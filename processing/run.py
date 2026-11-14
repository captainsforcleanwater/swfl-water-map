"""Clean and blend entry point. Runs after the collector in the hourly workflow.

    python -m processing.run                       # normal hourly run (release store in Actions)
    python -m processing.run --store local         # day files go to build/blend instead of a release
    python -m processing.run --start 2026-11-09T11 --force   # re-blend a range regardless of inputs

Each run:
1. Re-checks the raw readings of the last few days and rewrites data/qc and data/clean for them.
2. Works out, for every hour in the last reblend_hours, a signature of its inputs (the hourly
   sensor values and the GOES-19 frames available for it). Hours whose signature changed are
   re-blended: Everglades data lands 6 to 8 hours late and GOES-19 frames 4 to 5 hours late, so
   recent hours get redone as their inputs arrive. Hours missing from the last backfill_days
   are blended too.
3. Writes one file per UTC day (gridfile.py format) to a monthly GitHub Release
   blend-hourly-YYYY-MM and records hours and signatures in data/blend/index.json.

A summary goes to data/status/last_blend.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from collector.common import DATA, ROOT, SAL, TEMP, iso, parse_time, utcnow
from collector.goes19 import FAILED, OK, LocalStore, ReleaseStore
from . import blend, clean, goes, gridfile
from .config import BLEND_DIR, area_config, proc_config
from .grid import Grid, polygon_mask
from .waterpaths import WaterGraph

log = logging.getLogger("processing.run")

BLEND_VERSION = 1   # bump when the method changes so every hour in the window is redone
TEMP_SCALE = 0.05   # stored to 0.05 C (0.09 F); sensors are good to about 0.1 C
SAL_SCALE = 0.05
NODATA16 = -32768
NODATA8 = 255


# ---------------------------------------------------------------- stores

class BlendReleaseStore(ReleaseStore):
    def __init__(self, repo: str):
        super().__init__(repo)
        self.index_path = BLEND_DIR / "index.json"

    def _ensure_release(self, tag: str):
        if tag in self._known_tags:
            return
        if self._gh("release", "view", tag, check=False).returncode != 0:
            month = "-".join(tag.rsplit("-", 2)[-2:])
            self._gh("release", "create", tag, "--title", f"Blended hourly grids {month}",
                     "--notes", "Hourly blended water temperature and salinity grids for the SWFL study "
                     "area, one file per UTC day (format: processing/gridfile.py). Added automatically "
                     "by the hourly workflow.", "--latest=false")
        self._known_tags.add(tag)


class BlendLocalStore(LocalStore):
    def __init__(self):
        super().__init__(ROOT / "build" / "blend")


def tag_for(day: str) -> str:
    return f"{proc_config()['output']['release_prefix']}-{day[:7]}"


def asset_for(day: str) -> str:
    return f"{proc_config()['output']['asset_prefix']}_{day}.bin.gz"


# ---------------------------------------------------------------- helpers

def floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def hours_between(a: datetime, b: datetime) -> list[datetime]:
    out, t = [], floor_hour(a)
    while t <= b:
        out.append(t)
        t += timedelta(hours=1)
    return out


def config_hash() -> str:
    c = proc_config()
    blob = json.dumps({"blend": c["blend"], "grid": c["grid"], "qc": c["qc"], "v": BLEND_VERSION},
                      sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:8]


def signature(obs_t, obs_s, frame_times, chash) -> str:
    blob = json.dumps({
        "t": [[i, round(float(v), 3)] for i, v in zip(obs_t[0], obs_t[1])],
        "s": [[i, round(float(v), 3)] for i, v in zip(obs_s[0], obs_s[1])],
        "g": [int(t.timestamp()) for t in frame_times], "c": chash}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def persistence_at(arc: goes.GoesArchive, t: datetime, max_age_h: float):
    """Latest clear value per pixel at or before t, and its age in hours."""
    val = np.full(arc.frames.shape[1:], np.nan, "f4")
    age = np.full(arc.frames.shape[1:], np.nan, "f4")
    used = []
    for i in range(len(arc.times) - 1, -1, -1):
        ft = arc.times[i]
        if ft > t:
            continue
        a = (t - ft).total_seconds() / 3600
        if a > max_age_h:
            break
        used.append(ft)
        f = arc.frames[i]
        m = np.isnan(val) & np.isfinite(f)
        val[m] = f[m]
        age[m] = a
    return val, age, sorted(used)


class Targets:
    """Output cells that get values: water cells inside the area polygon."""

    def __init__(self, grid: Grid, graph: WaterGraph):
        rs, cs = grid.out_slice()
        prim = graph.primary.reshape(grid.ny, grid.nx)[rs, cs]
        poly = polygon_mask(grid, area_config()["polygon"])[rs, cs]
        self.mask = (prim >= 0) & poly
        self.nodes = prim[self.mask]
        self.shape = (grid.out_ny, grid.out_nx)
        self.water_pct = np.where(self.mask, np.rint(100 * graph.water_frac[rs, cs]), NODATA8).astype(np.uint8)

    def to_grid(self, v: np.ndarray, scale: float | None, dtype) -> np.ndarray:
        if scale is None:   # uint8 confidence
            out = np.full(self.shape, NODATA8, np.uint8)
            out[self.mask] = v
            return out
        out = np.full(self.shape, NODATA16, np.int16)
        q = np.rint(np.asarray(v, "f8") / scale)
        q = np.where(np.isfinite(q), np.clip(q, -32767, 32767), NODATA16)
        out[self.mask] = q.astype(np.int16)
        return out


# ---------------------------------------------------------------- one hour

def blend_hour(H, obs_t, obs_s, arc, graph, grid, targets, D, row_of, node_of, cfg):
    bc = cfg["blend"]
    info = {"time": iso(H)}
    # temperature background
    if arc is not None:
        val, age, used = persistence_at(arc, H, bc["goes_persistence_days"] * 24)
        sat, sat_age = goes.to_grid(val, age, arc, grid.lat()[::-1], grid.lon())
        sat, sat_age = sat[::-1], sat_age[::-1]   # GOES rows ascend in latitude; ours descend
        B, sb, ext = blend.temperature_background(graph, sat, sat_age, bc["temperature"])
        info["goes_frames"] = len(used)
        info["goes_newest"] = iso(used[-1]) if used else None
        ages = sat_age[np.isfinite(sat_age)]
        info["goes_median_age_h"] = round(float(np.median(ages)), 1) if len(ages) else None
    else:
        B = np.full(graph.n_nodes, np.nan)
        sb = np.full(graph.n_nodes, bc["temperature"]["max_background_error"])
        ext = np.full(graph.n_nodes, np.inf)
        info["goes_frames"] = 0

    def mk(o):
        ids, vals = o
        return blend.Obs(list(ids), np.array([node_of[i] for i in ids], int), np.asarray(vals, "f8"),
                         np.array([row_of[i] for i in ids], int))

    ot, os_ = mk(obs_t), mk(obs_s)
    at = blend.analyse_temperature(ot, D, targets.nodes, B, sb, ext, bc["temperature"])
    as_ = blend.analyse_salinity(os_, D, targets.nodes, graph, bc["salinity"])
    info["stations"] = {
        "temp": [[i, round(float(v), 2), None if not np.isfinite(b) else round(float(b), 2)]
                 for i, v, b in zip(ot.ids, ot.values, at.obs_background)],
        "sal": [[i, round(float(v), 2)] for i, v in zip(os_.ids, os_.values)],
    }
    layers = {
        "temp_c": targets.to_grid(at.value, TEMP_SCALE, np.int16),
        "temp_conf": targets.to_grid(at.confidence, None, np.uint8),
        "sal_psu": targets.to_grid(as_.value, SAL_SCALE, np.int16),
        "sal_conf": targets.to_grid(as_.confidence, None, np.uint8),
    }
    if len(os_.values) == 0:
        layers["sal_psu"][:] = NODATA16
        layers["sal_conf"][targets.mask] = 0
    return layers, info


LAYER_ATTRS = {
    "temp_c": {"units": "degC", "scale": TEMP_SCALE, "add_offset": 0.0, "encoding": "delta_tx",
               "long_name": "Blended near-surface water temperature (display converts to degF)"},
    "temp_conf": {"units": "percent", "scale": 1, "add_offset": 0, "encoding": "delta_t8",
                  "long_name": "Temperature confidence, 0-100"},
    "sal_psu": {"units": "PSU", "scale": SAL_SCALE, "add_offset": 0.0, "encoding": "delta_tx",
                "long_name": "Blended near-surface salinity (sensors only)"},
    "sal_conf": {"units": "percent", "scale": 1, "add_offset": 0, "encoding": "delta_t8",
                 "long_name": "Salinity confidence, 0-100"},
}
VALUE_LAYERS = ("temp_c", "sal_psu")


def write_day(path: Path, day: str, frames: dict[datetime, tuple[dict, dict]], grid: Grid, targets: Targets) -> int:
    times = sorted(frames)
    cfg = proc_config()["blend"]
    hours = []
    for t in times:
        layers, info = frames[t]
        info = dict(info)
        info["has"] = {k: bool((layers[k][targets.mask] != NODATA16).any()) for k in VALUE_LAYERS}
        hours.append(info)
    header = {
        "title": f"SWFL blended hourly water temperature and salinity, {day} UTC",
        "date": day, "times": [iso(t) for t in times], "grid": grid.describe(),
        "valid_mask": "water_pct", "hours": hours,
        "confidence": {
            "temp_c": f"100 * (1 - expected_error_degC / {cfg['temperature']['confidence_error_scale']})",
            "sal_psu": f"100 * (1 - expected_error_psu / {cfg['salinity']['confidence_error_scale']})"},
        "sources": "Sensors: NOAA CO-OPS, USGS, NDBC, GCOOS and SECOORA ERDDAP (SCCF RECON, USF COMPS, "
                   "Everglades National Park, FGCU, FIU). Satellite: NOAA CoastWatch GOES-19 hourly SST. "
                   "Water mask: USGS NHD and NOAA Coastal Relief Model.",
        "created": iso(utcnow()), "blend_version": BLEND_VERSION,
    }
    layers = {}
    for name, attrs in LAYER_ATTRS.items():
        a = np.stack([frames[t][0][name] for t in times])
        if name in VALUE_LAYERS:
            a = np.where(a == NODATA16, 0, a).astype(np.int16)   # masked by water_pct and has[] on read
        layers[name] = (a, attrs)
    layers["water_pct"] = (targets.water_pct, {
        "units": "percent", "scale": 1, "add_offset": 0, "nodata": NODATA8, "encoding": "none",
        "long_name": "Share of the cell that is water (static). Cells marked nodata hold no values in any layer."})
    return gridfile.write(path, header, layers)


def read_day(path: Path) -> dict[datetime, tuple[dict, dict]]:
    """Frames of a day file with nodata restored (NODATA16 / NODATA8), as blend_hour returns them."""
    head, arr = gridfile.read(path)
    invalid = arr["water_pct"] == NODATA8
    out = {}
    for i, (t, info) in enumerate(zip(head["times"], head["hours"])):
        layers = {}
        for k in LAYER_ATTRS:
            a = arr[k][i].copy()
            if k in VALUE_LAYERS:
                a[invalid | (not info.get("has", {}).get(k, True))] = NODATA16
            else:
                a[invalid] = NODATA8
            layers[k] = a
        out[parse_time(t)] = (layers, {kk: vv for kk, vv in info.items() if kk != "has"})
    return out


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", choices=["release", "local"], default=None)
    ap.add_argument("--start", help="first hour to consider (UTC, ISO); default backfill_days ago")
    ap.add_argument("--end", help="last hour to blend (UTC, ISO); default the latest complete hour")
    ap.add_argument("--now", help="pretend the run happens at this time (testing)")
    ap.add_argument("--force", action="store_true", help="re-blend every hour in range")
    ap.add_argument("--max-minutes", type=float, default=12.0, help="stop starting new hours after this")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    t_start = time.time()
    cfg = proc_config()
    bc = cfg["blend"]
    now = parse_time(args.now) if args.now else utcnow()
    h_last = floor_hour(now - timedelta(minutes=cfg["hourly"]["half_window_minutes"]))
    if args.end:
        h_last = min(h_last, floor_hour(parse_time(args.end)))
    reblend_from = h_last - timedelta(hours=bc["reblend_hours"] - 1)
    first = floor_hour(parse_time(args.start)) if args.start else h_last - timedelta(days=bc["backfill_days"])
    candidates = hours_between(first, h_last)
    status: dict = {"started": iso(utcnow()), "now": iso(now), "window": [iso(first), iso(h_last)]}

    mode = args.store or ("release" if os.environ.get("GITHUB_ACTIONS") == "true" else "local")
    store = BlendReleaseStore(os.environ["GITHUB_REPOSITORY"]) if mode == "release" else BlendLocalStore()
    index_path: Path = store.index_path
    index = json.loads(index_path.read_text()) if index_path.exists() else {"days": {}}
    days_idx = index.setdefault("days", {})

    grid = Grid.from_config()
    graph = WaterGraph.load(grid)
    targets = Targets(grid, graph)

    # ---- clean
    rewrite_from = (now - timedelta(days=cfg["qc"]["rewrite_days"])).date()
    write_days = sorted({d for d in clean.raw_days(rewrite_from, now.date())}
                        | {h.date() for h in candidates if h >= reblend_from})
    t0 = time.time()
    cres = clean.run(first - timedelta(hours=1), now, now, graph=graph, write_days=write_days)
    st = dict(cres.stats)
    nodes = st.pop("station_nodes", {})
    status["clean"] = st | {"seconds": round(time.time() - t0, 1)}
    log.info("clean: %s", {k: v for k, v in st.items() if k not in ("files_changed", "by_test")})

    # ---- inputs per hour
    h = cres.hourly
    h = h[(h["layer"] == "surface") & h["station"].isin(nodes)]
    obs = {}
    for (t, var), g in h.groupby(["time", "variable"]):
        g = g.sort_values("station")
        obs[(t.to_pydatetime(), var)] = (g["station"].tolist(), g["value"].to_numpy())
    ids = sorted(nodes)
    row_of = {sid: i for i, sid in enumerate(ids)}
    D = graph.distances([nodes[i] for i in ids]) if ids else np.zeros((0, graph.n_nodes), "f4")
    arc = goes.load(first - timedelta(days=bc["goes_persistence_days"]), h_last)
    chash = config_hash()

    empty = ([], np.array([]))
    todo = []
    for H in candidates:
        ot, os_ = obs.get((H, TEMP), empty), obs.get((H, SAL), empty)
        frames = arc.frame_times_between(H - timedelta(days=bc["goes_persistence_days"]), H) if arc else []
        if not len(ot[0]) and not len(os_[0]) and not frames:
            continue
        sig = signature(ot, os_, frames, chash)
        day, hh = H.strftime("%Y-%m-%d"), H.strftime("%H")
        old = days_idx.get(day, {}).get("hours", {}).get(hh, {}).get("sig")
        if args.force or old is None or (H >= reblend_from and old != sig):
            todo.append((H, sig, ot, os_))
    status["hours_considered"] = len(candidates)
    status["hours_to_blend"] = len(todo)
    log.info("%d hours to blend out of %d considered", len(todo), len(candidates))

    # ---- blend, newest day first, one file per day
    by_day: dict[str, list] = {}
    for item in todo:
        by_day.setdefault(item[0].strftime("%Y-%m-%d"), []).append(item)
    blended, errors, out_of_time = 0, {}, False
    work = Path(tempfile.mkdtemp(prefix="blend-"))
    for day in sorted(by_day, reverse=True):
        if out_of_time:
            break
        asset, tag = asset_for(day), tag_for(day)
        path = work / asset
        frames: dict = {}
        if day in days_idx:
            got = store.get(asset, path, tag)
            if got == OK:
                try:
                    frames = read_day(path)
                except Exception as e:  # noqa: BLE001
                    got = FAILED
                    log.warning("day file %s unreadable: %s", asset, e)
            if got == FAILED:
                errors[day] = "saved day file could not be downloaded or read; skipped this run"
                continue
        hours_info = days_idx.get(day, {}).get("hours", {}) if frames else {}
        done = 0
        for H, sig, ot, os_ in sorted(by_day[day], key=lambda x: x[0], reverse=True):
            if (time.time() - t_start) / 60 > args.max_minutes:
                out_of_time = True
                break
            try:
                layers, info = blend_hour(H, ot, os_, arc, graph, grid, targets, D, row_of, nodes, cfg)
            except Exception as e:  # noqa: BLE001
                log.exception("blend failed for %s", iso(H))
                errors[iso(H)] = str(e)[:200]
                continue
            info["sig"] = sig
            frames[H] = (layers, info)
            hours_info[H.strftime("%H")] = {"sig": sig, "n_temp": len(ot[0]), "n_sal": len(os_[0]),
                                            "goes_newest": info.get("goes_newest"), "blended": iso(utcnow())}
            done += 1
        if not done:
            continue
        size = write_day(path, day, frames, grid, targets)
        store.put(asset, path, tag)
        days_idx[day] = {"asset": asset, "release_tag": tag, "url": store.location(tag, asset),
                         "bytes": size, "updated": iso(utcnow()),
                         "hours": {k: hours_info[k] for k in sorted(hours_info)}}
        blended += done
        index.update({"format": f"{gridfile.FORMAT} v{gridfile.VERSION}", "grid": grid.describe(),
                      "layers": list(LAYER_ATTRS) + ["water_pct"]})
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
        log.info("wrote %s: %d hours (%d new), %.1f MB", asset, len(frames), done, size / 1e6)
    status.update({"hours_blended": blended, "errors": errors, "out_of_time": out_of_time,
                   "store": mode, "seconds": round(time.time() - t_start, 1), "finished": iso(utcnow())})
    (DATA / "status").mkdir(parents=True, exist_ok=True)
    (DATA / "status" / "last_blend.json").write_text(json.dumps(status, indent=1, sort_keys=True, default=str) + "\n")
    write_summary(status)
    return 0 if not errors else 1


def write_summary(status: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    c = status.get("clean", {})
    lines = ["## Clean and blend", "",
             f"Readings checked: {c.get('raw_readings', 0)} (suspect {c.get('suspect', 0)}, bad {c.get('bad', 0)})",
             f"Hours blended: {status.get('hours_blended', 0)} of {status.get('hours_to_blend', 0)} needing it",
             f"Errors: {len(status.get('errors', {}))}" + (" (stopped early: time limit)" if status.get("out_of_time") else "")]
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
