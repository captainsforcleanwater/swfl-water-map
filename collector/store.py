"""Write sensor readings to daily CSV files, merging with what is already there."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from pathlib import Path

from .common import DATA, Reading, iso

COLUMNS = ["time_utc", "station_uid", "variable", "value", "depth_m", "depth_tag", "sensor", "qc"]
KEY = ("time_utc", "station_uid", "variable", "depth_tag", "sensor", "depth_m")


def path_for(day: str, system: str) -> Path:
    y, m, _ = day.split("-")
    return DATA / "sensors" / y / m / f"{day}_{system}.csv"


def _row(r: Reading) -> dict:
    return {
        "time_utc": iso(r.time),
        "station_uid": r.station_uid,
        "variable": r.variable,
        "value": f"{r.value:.3f}".rstrip("0").rstrip("."),
        "depth_m": "" if r.depth_m is None else f"{r.depth_m:g}",
        "depth_tag": r.depth_tag,
        "sensor": r.sensor,
        "qc": r.qc,
    }


def write(readings: list[Reading], system_of: dict[str, str]) -> dict:
    """Merge readings into data/sensors/YYYY/MM/YYYY-MM-DD_<system>.csv. Returns files touched."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in readings:
        groups[(r.time.strftime("%Y-%m-%d"), system_of[r.station_uid])].append(_row(r))
    changed = {}
    for (day, system), rows in groups.items():
        p = path_for(day, system)
        merged: dict[tuple, dict] = {}
        if p.exists():
            with open(p, newline="") as f:
                for row in csv.DictReader(f):
                    merged[tuple(row[k] for k in KEY)] = row
        before = len(merged)
        for row in rows:   # newer pulls replace older ones (sources revise provisional data)
            merged[tuple(row[k] for k in KEY)] = row
        ordered = sorted(merged.values(), key=lambda r: (r["station_uid"], r["variable"], r["depth_tag"],
                                                          r["sensor"], r["time_utc"]))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(ordered)
        text = buf.getvalue()
        if p.exists() and p.read_text() == text:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        changed[str(p.relative_to(DATA.parent))] = {"rows": len(merged), "new": len(merged) - before}
    return changed


def has_any_data() -> bool:
    d = DATA / "sensors"
    return d.exists() and any(d.rglob("*.csv"))
