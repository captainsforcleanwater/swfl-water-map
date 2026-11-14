"""Hourly collector entry point.

    python -m collector.run                 # normal hourly run
    python -m collector.run --discover      # force station rediscovery
    python -m collector.run --lookback-hours 48
    python -m collector.run --goes-store local   # keep GOES-19 files in build/goes19 (testing)

Each source runs on its own; one failing source does not stop the others.
A summary goes to data/status/last_run.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import timedelta

from . import goes19, inventory, store
from .common import DATA, Area, Station, hours_ago, iso, load_config, log, parse_time, session, utcnow
from .sources import coops, erddap, ndbc, usgs

SENSOR_SOURCES = {"coops": coops, "usgs": usgs, "ndbc": ndbc}


def ndbc_is_mirror(st: Station, erddap_seen: list[dict]) -> bool:
    """NDBC republishes many stations we already pull from their owners."""
    owner = st.extra.get("owner", "")
    if st.extra.get("pgm") == "NOS/CO-OPS" or "Everglades National Park" in owner or "COMPS" in owner:
        return True
    sid = st.station_id.lower()
    for d in erddap_seen:
        text = f"{d['id']} {d['title']}".lower()
        if sid in text.replace("(", " ").replace(")", " ").replace("_", " ").replace("-", " ").split():
            return True
    return False


def discover(s, area: Area, cfg: dict, status: dict) -> tuple[list[Station], dict]:
    stations: list[Station] = []
    errors = {}
    t0 = time.time()
    try:
        stations += coops.discover(s, area)
    except Exception as e:  # noqa: BLE001
        errors["coops"] = str(e)[:300]
    try:
        stations += usgs.discover(s, area, cfg["active_within_days"], cfg["inventory_max_age_days"])
    except Exception as e:  # noqa: BLE001
        errors["usgs"] = str(e)[:300]
    erd, erd_err, erd_seen = erddap.discover(s, area, extra_ids=cfg.get("edge_anchors", []),
                                             max_age_days=cfg["inventory_max_age_days"])
    stations += erd
    errors.update({f"erddap-{k}": v for k, v in erd_err.items()})
    mirrors = [{"system": f"erddap-{d['server']}", "id": d["id"], "title": d["title"]}
               for d in erd_seen if d["id"].startswith(erddap.MIRROR_PREFIXES)]
    try:
        for st in ndbc.discover(s, area):
            if ndbc_is_mirror(st, erd_seen):
                mirrors.append({"system": "ndbc", "id": st.station_id, "title": st.name})
            else:
                stations.append(st)
    except Exception as e:  # noqa: BLE001
        errors["ndbc"] = str(e)[:300]
    status["discovery"] = {"seconds": round(time.time() - t0, 1), "errors": errors, "stations": len(stations)}
    meta = {"discovered": iso(utcnow()), "skipped_mirrors": mirrors, "discovery_errors": errors}
    return stations, meta


def classify(stations: list[Station], area: Area, cfg: dict) -> None:
    active_cut = iso(utcnow() - timedelta(days=cfg["active_within_days"]))
    for st in stations:
        st.in_area = area.in_polygon(st.lat, st.lon)
        st.edge_anchor = (not st.in_area) and st.uid in area.edge_anchors
        st.collect = st.in_area or st.edge_anchor
        st.active = bool(st.last_obs) and st.last_obs >= active_cut
    inventory.flag_duplicates(stations)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="force station rediscovery")
    ap.add_argument("--lookback-hours", type=float, default=None)
    ap.add_argument("--goes-store", choices=["release", "local", "off"], default=None,
                    help="where GOES-19 daily files go (default: release in GitHub Actions, local otherwise)")
    ap.add_argument("--only", nargs="*", help="limit to these sources (coops usgs ndbc erddap goes19)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config()
    area = Area(cfg)
    s = session()
    started = utcnow()
    status: dict = {"started": iso(started), "sources": {}}
    only = set(args.only or [])

    # ---- stations
    stations, meta = inventory.load()
    age_ok = meta.get("discovered") and (utcnow() - parse_time(meta["discovered"])).total_seconds() < cfg["discovery_max_age_hours"] * 3600
    rediscovered = False
    if args.discover or not stations or not age_ok:
        new_stations, new_meta = discover(s, area, cfg, status)
        # Keep stats we already learned (cadence, newest reading) for stations still present.
        old = {st.uid: st for st in stations}
        for st in new_stations:
            if st.uid in old:
                o = old[st.uid]
                st.cadence_min = st.cadence_min or o.cadence_min
                st.last_obs = max(st.last_obs or "", o.last_obs or "")
        # A source whose discovery failed keeps its previous stations instead of vanishing.
        failed = set(new_meta["discovery_errors"])
        keep = [o for o in stations if o.system in failed and o.uid not in {n.uid for n in new_stations}]
        stations = new_stations + keep
        meta.update(new_meta)
        rediscovered = True
    meta["active_within_days"] = cfg["active_within_days"]
    classify(stations, area, cfg)

    # ---- GOES-19 first: its 5-day server window makes it the one source we cannot catch up on later.
    goes_mode = args.goes_store or ("release" if os.environ.get("GITHUB_ACTIONS") == "true" else "local")
    if goes_mode != "off" and (not only or "goes19" in only):
        t0 = time.time()
        try:
            st_obj = (goes19.ReleaseStore(os.environ["GITHUB_REPOSITORY"]) if goes_mode == "release"
                      else goes19.LocalStore())
            g = goes19.archive(s, area, cfg["goes19_lookback_days"], st_obj)
        except Exception as e:  # noqa: BLE001
            log.exception("GOES-19 archive failed")
            g = {"fatal": str(e)[:300]}
        g["seconds"] = round(time.time() - t0, 1)
        g["store"] = goes_mode
        status["sources"]["goes19"] = g
        log.info("goes19: %s", {k: v for k, v in g.items() if k != "errors"})

    # ---- sensors
    lookback = args.lookback_hours or (cfg["sensor_lookback_hours"] if store.has_any_data()
                                       else cfg["first_run_lookback_hours"])
    end = utcnow()
    start = hours_ago(lookback)
    status["sensor_window"] = [iso(start), iso(end)]
    active_cut = iso(utcnow() - timedelta(days=30))
    all_readings = []
    groups: dict[str, list[Station]] = {}
    for st in stations:
        if not st.collect:
            continue
        key = "erddap" if st.system.startswith("erddap-") else st.system
        # Skip ERDDAP datasets that have been silent for a month; they are rechecked at discovery.
        if key == "erddap" and st.last_obs and st.last_obs < active_cut:
            continue
        groups.setdefault(key, []).append(st)
    mods = dict(SENSOR_SOURCES, erddap=erddap)
    for key, mod in mods.items():
        if only and key not in only:
            continue
        sts = groups.get(key, [])
        t0 = time.time()
        try:
            readings, errors = mod.fetch(s, sts, start, end)
        except Exception as e:  # noqa: BLE001
            readings, errors = [], {"fatal": str(e)[:300]}
            log.exception("source %s failed", key)
        all_readings += readings
        status["sources"][key] = {
            "stations_queried": len(sts),
            "stations_reporting": len({r.station_uid for r in readings}),
            "readings": len(readings), "seconds": round(time.time() - t0, 1),
            "errors": errors,
        }
        log.info("%s: %d readings from %d/%d stations, %d errors", key, len(readings),
                 status["sources"][key]["stations_reporting"], len(sts), len(errors))

    system_of = {st.uid: st.system for st in stations}
    status["files_changed"] = store.write(all_readings, system_of)

    # stations that reported now count as active
    reporting = {r.station_uid for r in all_readings}
    inventory.update_cadence(stations, all_readings)
    inventory.update_depths(stations, all_readings)
    for st in stations:
        if st.uid in reporting:
            st.active = True
    if rediscovered or reporting:
        inventory.save(stations, meta)

    status["finished"] = iso(utcnow())
    status["total_readings"] = len(all_readings)
    (DATA / "status").mkdir(parents=True, exist_ok=True)
    (DATA / "status" / "last_run.json").write_text(json.dumps(status, indent=1, sort_keys=True) + "\n")
    write_summary(status)

    sensor_ok = any(v.get("readings", 0) for k, v in status["sources"].items() if k != "goes19")
    goes_ok = "fatal" not in status["sources"].get("goes19", {})
    return 0 if (sensor_ok or only) and goes_ok else 1


def write_summary(status: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Collector run", "", f"Window: {status.get('sensor_window')}", "",
             "| Source | Stations reporting | Readings | Errors |", "|---|---:|---:|---:|"]
    for k, v in status["sources"].items():
        if k == "goes19":
            lines.append(f"| goes19 | | {v.get('added', 0)} hours added | {len(v.get('errors', {})) + ('fatal' in v)} |")
        else:
            lines.append(f"| {k} | {v['stations_reporting']}/{v['stations_queried']} | {v['readings']} | {len(v['errors'])} |")
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
