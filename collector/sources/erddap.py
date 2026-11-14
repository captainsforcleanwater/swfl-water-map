"""ERDDAP aggregators (GCOOS, SECOORA, IOOS sensor map): discovery by bounding box and standard name.

This brings in SCCF RECON and USF COMPS (GCOOS), Everglades National Park and
other partner stations (SECOORA). Datasets that only mirror sources we pull
directly (USGS, CO-OPS) are skipped, as are the IOOS sensor map's copies of
GCOOS and SECOORA datasets.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timedelta
from urllib.parse import quote

from ..common import SAL, TEMP, Area, Reading, Station, depth_tag_from_m, iso, log, parse_time, utcnow

SERVERS = {
    # key: base URL. Order matters: a dataset ID seen on an earlier server is not taken again.
    "gcoos": "https://erddap.gcoos.org/erddap",
    "secoora": "https://erddap.secoora.org/erddap",
    "ioos": "https://erddap.sensors.ioos.us/erddap",
}

STANDARD_NAMES = {
    "sea_water_temperature": TEMP,
    "sea_surface_temperature": TEMP,
    "sea_water_practical_salinity": SAL,
    "sea_water_salinity": SAL,
}

FIXED_TYPES = ("TimeSeries", "TimeSeriesProfile", "Point")

# Dataset ID prefixes that mirror a source pulled directly, or another aggregator.
MIRROR_PREFIXES = ("gov_usgs_nwis_", "noaa_nos_co_ops_", "ism-")


def _q(params: str) -> str:
    """Percent-encode an ERDDAP query. GCOOS rejects raw < > characters."""
    return quote(params, safe="=&,()")


def _search(s, base: str, area: Area, standard_name: str) -> list[dict]:
    params = {
        "page": 1, "itemsPerPage": 5000, "protocol": "tabledap", "standard_name": standard_name,
        "minLat": area.lat_min, "maxLat": area.lat_max, "minLon": area.lon_min, "maxLon": area.lon_max,
    }
    r = s.get(f"{base}/search/advanced.csv", params=params, timeout=120)
    if r.status_code == 404:   # ERDDAP's way of saying "no matches"
        return []
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def _info(s, base: str, dsid: str) -> dict:
    r = s.get(f"{base}/info/{dsid}/index.json", timeout=60)
    r.raise_for_status()
    t = r.json()["table"]
    glob, vars_ = {}, {}
    for row_type, var, attr, _dtype, value in t["rows"]:
        if row_type == "variable":
            vars_.setdefault(var, {})
        elif row_type == "attribute":
            if var == "NC_GLOBAL":
                glob[attr] = value
            else:
                vars_.setdefault(var, {})[attr] = value
    return {"global": glob, "vars": vars_}


def _provider(g: dict, title: str) -> str:
    t = (title + " " + g.get("platform_name", "")).upper()
    if "SCCF" in t:
        return "SCCF RECON"
    if "COMPS" in t or g.get("platform", "").upper().startswith("IOOS-STATION-USF-COMPS"):
        return "USF COMPS"
    # contributor_name is often the funder (GCOOS), so the creator comes first.
    for k in ("creator_institution", "creator_name", "institution", "publisher_name"):
        v = g.get(k, "").strip()
        if v and v.upper() not in ("GCOOS", "FELIMON GAYANILO", "NONE"):
            return v.split(",")[0]
    return g.get("institution", "unknown")


def _station_from_info(server: str, base: str, dsid: str, title: str, info: dict) -> Station | None:
    g, vs = info["global"], info["vars"]
    # Fixed stations only. Gliders, ship tracks and cruise profiles move, so they need a different model.
    if g.get("cdm_data_type", "") not in FIXED_TYPES:
        return None
    measure = {}
    for v, a in vs.items():
        sn = a.get("standard_name", "")
        if sn in STANDARD_NAMES and "qc" not in v.lower():
            measure[v] = {"variable": STANDARD_NAMES[sn], "standard_name": sn, "units": a.get("units", "")}
    if not measure:
        return None
    depth_var = None
    for cand in ("depth", "z", "altitude"):
        if cand in vs:
            depth_var = cand
            break
    try:
        lat = float(g.get("geospatial_lat_min") or g.get("Southernmost_Northing"))
        lon = float(g.get("geospatial_lon_min") or g.get("Westernmost_Easting"))
    except (TypeError, ValueError):
        return None
    if lon > 180:
        lon -= 360
    name = g.get("platform_name") or title
    last = g.get("time_coverage_end", "")
    try:
        last = iso(parse_time(last)) if last else ""
    except ValueError:
        last = ""
    variables = sorted({m["variable"] for m in measure.values()})
    vmin, vmax = g.get("geospatial_vertical_min"), g.get("geospatial_vertical_max")
    return Station(
        uid=f"erddap-{server}:{dsid}", provider=_provider(g, title), system=f"erddap-{server}",
        station_id=dsid, name=name.strip(), lat=lat, lon=lon, variables=variables,
        depths=[], last_obs=last,
        url=f"{base}/tabledap/{dsid}.html",
        notes=f"Dataset vertical range {vmin} to {vmax} m" if vmin not in (None, "") else "",
        extra={"server": server, "measure": measure, "depth_var": depth_var,
               "depth_positive": vs.get(depth_var, {}).get("positive", "") if depth_var else "",
               "depth_sn": vs.get(depth_var, {}).get("standard_name", "") if depth_var else ""})


def discover(s, area: Area, extra_ids: list[str] | None = None,
             max_age_days: int = 365) -> tuple[list[Station], dict, list[dict]]:
    """Returns stations, per-server errors, and every dataset seen (including skipped mirrors)."""
    stations: list[Station] = []
    errors: dict[str, str] = {}
    seen_ids: set[str] = set()
    all_seen: list[dict] = []
    wanted_extra = {}
    for uid in extra_ids or []:
        sys_, _, dsid = uid.partition(":")
        if sys_.startswith("erddap-"):
            wanted_extra.setdefault(sys_.split("-", 1)[1], []).append(dsid)

    age_cut = (utcnow() - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_count = 0
    for server, base in SERVERS.items():
        found: dict[str, str] = {}
        try:
            for sn in STANDARD_NAMES:
                for row in _search(s, base, area, sn):
                    found[row["Dataset ID"]] = row.get("Title", "")
        except Exception as e:  # noqa: BLE001
            errors[server] = f"search failed: {str(e)[:200]}"
            log.warning("ERDDAP %s search failed: %s", server, e)
        for dsid in wanted_extra.get(server, []):
            found.setdefault(dsid, "")
        for dsid, title in sorted(found.items()):
            all_seen.append({"server": server, "id": dsid, "title": title})
            if dsid.startswith(MIRROR_PREFIXES) or dsid in seen_ids:
                continue
            seen_ids.add(dsid)
            try:
                info = _info(s, base, dsid)
            except Exception as e:  # noqa: BLE001
                errors[f"{server}:{dsid}"] = f"info failed: {str(e)[:200]}"
                continue
            stn = _station_from_info(server, base, dsid, title, info)
            if not stn:
                continue
            if stn.last_obs and stn.last_obs < age_cut and stn.uid not in (extra_ids or []):
                old_count += 1
                continue
            stations.append(stn)
    if old_count:
        log.info("ERDDAP: skipped %d datasets with no data in the last %d days", old_count, max_age_days)
    return stations, errors, all_seen


def _to_float(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _is_fahrenheit(units: str) -> bool:
    u = units.lower().replace(" ", "_")
    return u in ("degf", "deg_f", "degree_fahrenheit", "degrees_fahrenheit", "fahrenheit", "f")


def _fahrenheit_to_c(x: float) -> float:
    return (x - 32) * 5 / 9


def fetch(s, stations: list[Station], start: datetime, end: datetime) -> tuple[list[Reading], dict]:
    out: list[Reading] = []
    errors: dict[str, str] = {}
    for stn in stations:
        base = SERVERS[stn.extra["server"]]
        measure = stn.extra["measure"]
        dvar = stn.extra.get("depth_var")
        cols = ["time"] + ([dvar] if dvar else []) + list(measure)
        query = ",".join(cols) + f"&time>={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&time<={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        url = f"{base}/tabledap/{stn.station_id}.csv?{_q(query)}"
        try:
            r = s.get(url, timeout=120)
        except Exception as e:  # noqa: BLE001
            errors[stn.uid] = str(e)[:200]
            continue
        if r.status_code == 404 or (r.status_code == 500 and "nRows = 0" in r.text):
            continue  # no data in window
        if not r.ok:
            errors[stn.uid] = f"HTTP {r.status_code}: {r.text[:150]}"
            continue
        rows = list(csv.reader(io.StringIO(r.text)))[2:]   # skip header and units rows
        # Collect values as {(time, var): {depth: value}}
        grid: dict[tuple, dict] = {}
        flip = stn.extra.get("depth_sn") == "altitude" or stn.extra.get("depth_positive") == "up"
        for row in rows:
            if not row:
                continue
            t = parse_time(row[0])
            k = 1
            depth = None
            if dvar:
                depth = _to_float(row[1])
                if depth is not None and flip:
                    depth = -depth + 0.0   # + 0.0 turns -0.0 into 0.0
                k = 2
            for j, v in enumerate(measure):
                val = _to_float(row[k + j])
                if val is None:
                    continue
                grid.setdefault((t, v), {})[depth] = val
        all_depths = sorted({d for vals in grid.values() for d in vals if d is not None})
        for (t, v), by_depth in grid.items():
            m = measure[v]
            conv = _fahrenheit_to_c if m["variable"] == TEMP and _is_fahrenheit(m["units"]) else (lambda x: x)
            values = set(by_depth.values())
            is_sst = m["standard_name"] == "sea_surface_temperature"
            if is_sst or len(values) == 1:
                # One sensor, possibly repeated across another instrument's depth bins
                # (GCOOS COMPS buoys do this). Keep one reading.
                shallowest = min(by_depth, key=lambda x: -1 if x is None else x)
                val = by_depth[shallowest]
                if len(by_depth) == 1 and shallowest is not None:
                    d, tag = shallowest, depth_tag_from_m(shallowest, [shallowest])
                else:
                    d, tag = None, "surface"
                out.append(Reading(t, stn.uid, m["variable"], conv(val), depth_m=d, depth_tag=tag, sensor=v))
            else:
                for d, val in by_depth.items():
                    out.append(Reading(t, stn.uid, m["variable"], conv(val), depth_m=d,
                                       depth_tag=depth_tag_from_m(d, all_depths), sensor=v))
    return out, errors
