"""USGS gages through the USGS Water Data OGC API (api.waterdata.usgs.gov, v1).

The legacy waterservices.usgs.gov service still answers, but USGS is retiring it,
so this uses the new API. Set USGS_API_KEY (free from api.data.gov) for higher
rate limits; it works without one at hourly volume.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from ..common import SAL, SPCOND, TEMP, Area, Reading, Station, depth_tag_from_text, iso, log, parse_time, utcnow

BASE = "https://api.waterdata.usgs.gov/ogcapi/v1/collections"

# parameter code -> (our variable, unit conversion)
PARAMS = {
    "00010": TEMP,    # water temperature, degC
    "00480": SAL,     # salinity, ppt
    "00095": SPCOND,  # specific conductance at 25 C, uS/cm
}

# Site types that are not open surface water. Wells, atmosphere, subsurface and land use sites are skipped.
SKIP_SITE_PREFIXES = ("GW", "SB", "AT", "AG", "AS", "AW", "FA", "LA")


def _headers():
    key = os.environ.get("USGS_API_KEY", "").strip()
    return {"X-Api-Key": key} if key else {}


def _paged(s, url, params, max_pages=50):
    params = dict(params, f="json")
    pages = 0
    while url and pages < max_pages:
        r = s.get(url, params=params, headers=_headers(), timeout=120)
        r.raise_for_status()
        j = r.json()
        yield from j.get("features", [])
        url = next((l["href"] for l in j.get("links", []) if l.get("rel") == "next"), None)
        params = None  # the next link carries the query
        pages += 1


def _end(p: dict) -> str:
    """Newest data time of a series. v1 calls it "end" (UTC); v0 called it "end_utc"."""
    return p.get("end_utc") or p.get("end") or ""


def discover(s, area: Area, active_days: int, max_age_days: int = 365) -> list[Station]:
    series = list(_paged(s, f"{BASE}/time-series-metadata/items", {
        "bbox": area.bbox_str, "parameter_code": ",".join(PARAMS),
        "computation_identifier": "Instantaneous", "limit": 10000,
    }))
    cutoff = utcnow() - timedelta(days=max_age_days)   # list anything with data in the last year
    by_site: dict[str, list[dict]] = {}
    geom: dict[str, tuple[float, float]] = {}
    for f in series:
        p = f["properties"]
        end = _end(p)
        if not end or parse_time(end) < cutoff:
            continue
        mid = p["monitoring_location_id"]
        by_site.setdefault(mid, []).append(p)
        if f.get("geometry"):
            lon, lat = f["geometry"]["coordinates"][:2]
            geom[mid] = (lat, lon)

    # names and site types, in batches
    meta: dict[str, dict] = {}
    ids = list(by_site)
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        for f in _paged(s, f"{BASE}/monitoring-locations/items", {
                "id": ",".join(batch), "limit": 100,
                "properties": "monitoring_location_name,site_type_code,site_type"}):
            meta[f["id"]] = f["properties"]
            if f.get("geometry"):
                lon, lat = f["geometry"]["coordinates"][:2]
                geom[f["id"]] = (lat, lon)

    active_cutoff = utcnow() - timedelta(days=active_days)
    stations = []
    for mid, plist in by_site.items():
        m = meta.get(mid, {})
        stype = m.get("site_type_code") or ""
        if stype.startswith(SKIP_SITE_PREFIXES):
            continue
        if mid not in geom:
            continue
        lat, lon = geom[mid]
        num = mid.split("-", 1)[1]
        ts_map = {}
        variables, depths = [], []
        last = ""
        seen_keys = set()
        for p in plist:
            var = PARAMS[p["parameter_code"]]
            tag = depth_tag_from_text(p.get("sublocation_identifier"))
            end = _end(p)
            sub = p.get("sublocation_identifier") or ""
            if parse_time(end) >= active_cutoff:
                if (var, sub) in seen_keys:
                    sub = f"{sub} ts{p['id'][:6]}".strip()
                seen_keys.add((var, sub))
                ts_map[p["id"]] = {"variable": var, "depth_tag": tag, "sublocation": sub,
                                   "unit": p.get("unit_of_measure") or ""}
            if var not in variables:
                variables.append(var)
            if tag not in depths:
                depths.append(tag)
            last = max(last, end)
        stations.append(Station(
            uid=f"usgs:{num}", provider="USGS", system="usgs", station_id=num,
            name=(m.get("monitoring_location_name") or num).title(), lat=lat, lon=lon,
            variables=variables, depths=depths, last_obs=iso(parse_time(last)) if last else "",
            url=f"https://waterdata.usgs.gov/monitoring-location/{num}/",
            notes=f"USGS site type {stype or 'unknown'} ({m.get('site_type', '')})",
            extra={"time_series": ts_map, "site_type": stype}))
    return stations


def fetch(s, stations: list[Station], start: datetime, end: datetime) -> tuple[list[Reading], dict]:
    ts_lookup = {}
    for stn in stations:
        for tsid, info in stn.extra.get("time_series", {}).items():
            ts_lookup[tsid] = (stn.uid, info)
    if not ts_lookup:
        return [], {}
    out: list[Reading] = []
    errors: dict[str, str] = {}
    # One bbox query per parameter covers every site, which keeps the request count low.
    lat = [st.lat for st in stations]
    lon = [st.lon for st in stations]
    bbox = f"{min(lon) - 0.01},{min(lat) - 0.01},{max(lon) + 0.01},{max(lat) + 0.01}"
    for pcode in PARAMS:
        try:
            for f in _paged(s, f"{BASE}/continuous/items", {
                    "bbox": bbox, "parameter_code": pcode, "limit": 10000,
                    "datetime": f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                    "properties": "time_series_id,time,value,approval_status,qualifier"}):
                p = f["properties"]
                hit = ts_lookup.get(p.get("time_series_id"))
                if not hit or p.get("value") in (None, ""):
                    continue
                uid, info = hit
                try:
                    v = float(p["value"])
                except ValueError:
                    continue
                if info["variable"] == TEMP and info["unit"].lower() in ("degf", "deg f"):
                    v = (v - 32) * 5 / 9
                q = p.get("approval_status") or ""
                if p.get("qualifier"):
                    q += f" {p['qualifier']}"
                out.append(Reading(time=parse_time(p["time"]), station_uid=uid, variable=info["variable"],
                                   value=v, depth_tag=info["depth_tag"], sensor=info["sublocation"],
                                   qc=q.strip()))
        except Exception as e:  # noqa: BLE001
            errors[f"usgs/{pcode}"] = str(e)[:300]
            log.warning("USGS fetch %s failed: %s", pcode, e)
    return out, errors
