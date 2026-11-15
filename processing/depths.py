"""Water depth at each station, from the NOAA Coastal Relief Model (volume 3, about 90 m cells).

Used to decide whether a bottom-only temperature gauge sits in shallow water, where the bottom
reading can stand in for the surface. For each station the deepest relief-model cell within
SEARCH_M is taken, because gauges sit at the edge of channels. If no cell within SEARCH_M is below
sea level, the station is in a creek narrower than the model, and is treated as shallow.

Results are kept in data/grid/station_depth.json (committed by the workflow), so the model is only
queried for new or moved stations. If the query fails, the station's depth stays unknown and its
bottom gauge is not used until a later run succeeds.
"""

from __future__ import annotations

import json
import logging

import numpy as np

from collector.common import distance_m, iso, utcnow
from .config import GRID_DIR
from .shoreline import CRM_URL

log = logging.getLogger("processing.depths")

PATH = GRID_DIR / "station_depth.json"
SEARCH_M = 300.0


def _load() -> dict:
    if PATH.exists():
        return json.loads(PATH.read_text()).get("stations", {})
    return {}


def _query(points: dict[str, tuple[float, float]]) -> dict[str, dict]:
    from netCDF4 import Dataset

    out = {}
    with Dataset(CRM_URL) as ds:
        x = np.asarray(ds["x"][:])
        y = np.asarray(ds["y"][:])
        dx, dy = x[1] - x[0], y[1] - y[0]
        for uid, (lat, lon) in points.items():
            if not (x[0] <= lon <= x[-1] and y[0] <= lat <= y[-1]):
                continue
            ry = int(np.ceil(SEARCH_M / (dy * 111320))) + 1
            rx = int(np.ceil(SEARCH_M / (dx * 111320 * np.cos(np.radians(lat))))) + 1
            j = int(round((lat - y[0]) / dy))
            i = int(round((lon - x[0]) / dx))
            z = np.ma.filled(ds["z"][max(j - ry, 0):j + ry + 1, max(i - rx, 0):i + rx + 1], np.nan)
            yy = y[max(j - ry, 0):j + ry + 1][:, None]
            xx = x[max(i - rx, 0):i + rx + 1][None, :]
            d = np.vectorize(lambda a, b: distance_m(lat, lon, a, b))(np.broadcast_to(yy, z.shape),
                                                                      np.broadcast_to(xx, z.shape))
            wet = (d <= SEARCH_M) & np.isfinite(z) & (z < 0)
            out[uid] = {"lat": round(lat, 5), "lon": round(lon, 5),
                        "depth_m": round(float(-z[wet].min()), 1) if wet.any() else None,
                        "note": "" if wet.any() else f"no relief-model water within {SEARCH_M:g} m: creek narrower than the model, treated as shallow",
                        "checked": iso(utcnow())}
    return out


def station_depths(stations: list[dict], fetch: bool = True) -> dict[str, dict]:
    """uid -> {"depth_m": metres or None, "note": ...} for the given raw station records."""
    known = _load()
    want = {}
    for s in stations:
        k = known.get(s["uid"])
        if not k or abs(k["lat"] - s["lat"]) > 1e-4 or abs(k["lon"] - s["lon"]) > 1e-4:
            want[s["uid"]] = (s["lat"], s["lon"])
    if want and fetch:
        try:
            known.update(_query(want))
            GRID_DIR.mkdir(parents=True, exist_ok=True)
            PATH.write_text(json.dumps({
                "_comment": "Deepest NOAA Coastal Relief Model cell within 300 m of each station (processing/depths.py). depth_m null means no model water nearby: a narrow creek, treated as shallow.",
                "source": CRM_URL, "stations": dict(sorted(known.items()))}, indent=1) + "\n")
        except Exception as e:  # noqa: BLE001
            log.warning("station depths not updated (%s); bottom gauges at %d stations wait", e, len(want))
    return known


def is_shallow(info: dict | None, max_depth_m: float) -> bool | None:
    """True if shallow, False if deeper than max_depth_m, None if unknown."""
    if not info:
        return None
    return info["depth_m"] is None or info["depth_m"] <= max_depth_m
