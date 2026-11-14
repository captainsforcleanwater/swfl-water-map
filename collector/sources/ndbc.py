"""NDBC buoys and C-MAN stations: discovery from the active stations list, data from realtime2 text files.

Many NDBC entries in this area are mirrors of stations we already pull from their
owners (CO-OPS, Everglades National Park, USF COMPS). run.py drops those.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ..common import SAL, TEMP, Area, Reading, Station, depth_tag_from_m

ACTIVE = "https://www.ndbc.noaa.gov/activestations.xml"
RT = "https://www.ndbc.noaa.gov/data/realtime2/{id}.{ext}"


def discover(s, area: Area) -> list[Station]:
    r = s.get(ACTIVE, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for el in root:
        a = el.attrib
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except (KeyError, ValueError):
            continue
        if not area.in_bbox(lat, lon):
            continue
        sid = a["id"].upper()
        out.append(Station(
            uid=f"ndbc:{sid}", provider=a.get("owner", "NDBC"), system="ndbc", station_id=sid,
            name=a.get("name", sid).strip(), lat=lat, lon=lon,
            variables=[TEMP] + ([SAL] if a.get("waterquality") == "y" else []),
            depths=["surface"], url=f"https://www.ndbc.noaa.gov/station_page.php?station={sid.lower()}",
            notes=f"NDBC program: {a.get('pgm', '')}; type {a.get('type', '')}",
            extra={"owner": a.get("owner", ""), "pgm": a.get("pgm", "")}))
    return out


def _parse(text: str):
    lines = text.splitlines()
    if len(lines) < 3:
        return [], []
    cols = lines[0].lstrip("#").split()
    rows = [ln.split() for ln in lines[2:] if ln.strip()]
    return cols, rows


def _time(cols, row) -> datetime:
    i = {c: k for k, c in enumerate(cols)}
    return datetime(int(row[i["YY"]]), int(row[i["MM"]]), int(row[i["DD"]]),
                    int(row[i["hh"]]), int(row[i["mm"]]), tzinfo=timezone.utc)


def _num(x):
    if x in ("MM", "", None):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def fetch(s, stations: list[Station], start: datetime, end: datetime) -> tuple[list[Reading], dict]:
    out: list[Reading] = []
    errors: dict[str, str] = {}
    for stn in stations:
        sid = stn.station_id
        got_any = False
        for ext in ("txt", "ocean"):
            try:
                r = s.get(RT.format(id=sid, ext=ext), timeout=60)
            except Exception as e:  # noqa: BLE001
                errors[f"{stn.uid}/{ext}"] = str(e)[:200]
                continue
            if r.status_code == 404:
                continue
            if not r.ok:
                errors[f"{stn.uid}/{ext}"] = f"HTTP {r.status_code}"
                continue
            got_any = True
            cols, rows = _parse(r.text)
            idx = {c: k for k, c in enumerate(cols)}
            if ext == "txt" and "WTMP" in idx:
                for row in rows:
                    t = _time(cols, row)
                    if not (start <= t <= end):
                        continue
                    v = _num(row[idx["WTMP"]])
                    if v is not None:
                        out.append(Reading(t, stn.uid, TEMP, v, depth_tag="surface", sensor="WTMP"))
            elif ext == "ocean":
                depths = [_num(row[idx["DEPTH"]]) for row in rows if "DEPTH" in idx]
                depths = [d for d in depths if d is not None]
                for row in rows:
                    t = _time(cols, row)
                    if not (start <= t <= end):
                        continue
                    d = _num(row[idx["DEPTH"]]) if "DEPTH" in idx else None
                    tag = depth_tag_from_m(d, depths)
                    for col, var in (("OTMP", TEMP), ("SAL", SAL)):
                        if col in idx:
                            v = _num(row[idx[col]])
                            if v is not None:
                                out.append(Reading(t, stn.uid, var, v, depth_m=d, depth_tag=tag, sensor=col))
        if not got_any:
            errors.setdefault(stn.uid, "no realtime2 files")
    return out, errors
