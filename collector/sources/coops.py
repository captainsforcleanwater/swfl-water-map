"""NOAA CO-OPS tide stations: discovery through the Metadata API, data through the Data API."""

from __future__ import annotations

from datetime import datetime

from ..common import SAL, TEMP, Area, Reading, Station, log, parse_time

MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
DATAAPI = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

# Fallback only: used if the Metadata API is down. Discovery normally finds these.
FALLBACK = {
    "8725520": ("Fort Myers", 26.6478, -81.8711),
    "8723970": ("Vaca Key, Florida Bay", 24.711, -81.1065),
    "8724580": ("Key West", 24.5557, -81.8079),
}

# CO-OPS product name -> our variable
PRODUCTS = {"water_temperature": TEMP, "salinity": SAL}


def discover(s, area: Area) -> list[Station]:
    found: dict[str, Station] = {}
    try:
        for mdtype, var in (("watertemp", TEMP), ("cond", SAL)):
            r = s.get(MDAPI, params={"type": mdtype}, timeout=60)
            r.raise_for_status()
            for st in r.json().get("stations", []):
                lat, lon = float(st["lat"]), float(st["lng"])
                if not area.in_bbox(lat, lon):
                    continue
                sid = str(st["id"])
                stn = found.get(sid) or Station(
                    uid=f"coops:{sid}", provider="NOAA CO-OPS", system="coops", station_id=sid,
                    name=st["name"], lat=lat, lon=lon,
                    url=f"https://tidesandcurrents.noaa.gov/stationhome.html?id={sid}",
                    notes="Sensor depth not published in the API; treated as near-surface.")
                if var not in stn.variables:
                    stn.variables.append(var)
                found[sid] = stn
    except Exception as e:  # noqa: BLE001
        log.warning("CO-OPS discovery failed, using fallback list: %s", e)
        for sid, (name, lat, lon) in FALLBACK.items():
            found[sid] = Station(uid=f"coops:{sid}", provider="NOAA CO-OPS", system="coops",
                                 station_id=sid, name=name, lat=lat, lon=lon, variables=[TEMP],
                                 notes="From fallback list (discovery failed).")
    for stn in found.values():
        stn.depths = ["surface"]
    return list(found.values())


def fetch(s, stations: list[Station], start: datetime, end: datetime) -> tuple[list[Reading], dict]:
    out: list[Reading] = []
    errors: dict[str, str] = {}
    for stn in stations:
        products = ["water_temperature"]
        if SAL in stn.variables:
            products.append("salinity")
        for prod in products:
            params = {
                "product": prod, "station": stn.station_id,
                "begin_date": start.strftime("%Y%m%d %H:%M"), "end_date": end.strftime("%Y%m%d %H:%M"),
                "time_zone": "gmt", "units": "metric", "format": "json",
                "application": "swfl-water-map",
            }
            try:
                r = s.get(DATAAPI, params=params, timeout=60)
                r.raise_for_status()
                j = r.json()
            except Exception as e:  # noqa: BLE001
                errors[f"{stn.uid}/{prod}"] = str(e)[:200]
                continue
            if "error" in j:
                msg = j["error"].get("message", "")
                if "No data was found" not in msg:
                    errors[f"{stn.uid}/{prod}"] = msg[:200]
                continue
            var = PRODUCTS[prod]
            for row in j.get("data", []):
                v = row.get("v") if prod != "salinity" else row.get("s")
                if v in (None, ""):
                    continue
                out.append(Reading(time=parse_time(row["t"]), station_uid=stn.uid, variable=var,
                                   value=float(v), qc=row.get("f", "")))
    return out, errors
