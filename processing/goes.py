"""GOES-19 hourly SST from the archive, carried forward pixel by pixel through cloudy hours.

Frames come from the daily NetCDF files listed in data/goes19/index.json (downloaded once into
build/cache/goes19). For a blend hour H, each pixel takes its most recent clear value at or
before H, up to goes_persistence_days old, and remembers how old that value is.

Before use, each frame drops pixels next to cloud (cloud edges read cold) and pixels more than
OUTLIER_C from the median of their 5 x 5 neighbourhood.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import requests
from scipy import ndimage

from collector.common import DATA, USER_AGENT
from collector.goes19 import read_day
from .config import CACHE

log = logging.getLogger("processing.goes")

INDEX = DATA / "goes19" / "index.json"
OUTLIER_C = 1.5


@dataclass
class GoesArchive:
    lat: np.ndarray            # ascending, as stored
    lon: np.ndarray
    times: list[datetime]
    frames: np.ndarray         # (nt, nlat, nlon) float32, NaN where cloudy or land

    def frame_times_between(self, t0: datetime, t1: datetime) -> list[datetime]:
        return [t for t in self.times if t0 <= t <= t1]


def _fetch(day: str, info: dict, cache: Path) -> Path | None:
    p = cache / info["asset"]
    if p.exists() and p.stat().st_size == info.get("bytes"):
        return p
    try:
        r = requests.get(info["url"], timeout=120, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        cache.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        return p
    except Exception as e:  # noqa: BLE001
        log.warning("GOES-19 file for %s not downloaded: %s", day, e)
        return p if p.exists() else None


def clean_frame(f: np.ndarray, ever_valid: np.ndarray) -> np.ndarray:
    f = f.copy()
    valid = np.isfinite(f)
    cloud = ever_valid & ~valid
    near_cloud = ndimage.binary_dilation(cloud, structure=np.ones((3, 3), bool))
    f[near_cloud] = np.nan
    valid = np.isfinite(f)
    if valid.sum() > 25:
        med = _nanmedian5(f)
        f[np.abs(f - med) > OUTLIER_C] = np.nan
    return f


def _nanmedian5(f: np.ndarray) -> np.ndarray:
    """Median of finite values in each 5 x 5 window."""
    pad = np.pad(f, 2, constant_values=np.nan)
    stack = np.stack([pad[i:i + f.shape[0], j:j + f.shape[1]] for i in range(5) for j in range(5)])
    with np.errstate(all="ignore"):
        out = np.full(f.shape, np.nan, "f4")
        ok = np.isfinite(stack).any(0)
        out[ok] = np.nanmedian(stack[:, ok], axis=0)
    return out


def load(t0: datetime, t1: datetime, cache: Path | None = None) -> GoesArchive | None:
    """Frames between t0 and t1 (inclusive) from the archive."""
    if not INDEX.exists():
        return None
    index = json.loads(INDEX.read_text())
    cache = cache or CACHE / "goes19"
    lat = lon = None
    frames: dict[datetime, np.ndarray] = {}
    d = t0.date()
    while d <= t1.date():
        info = index.get("days", {}).get(d.isoformat())
        if info:
            p = _fetch(d.isoformat(), info, cache)
            if p is not None:
                try:
                    la, lo, fr = read_day(p)
                    if lat is None:
                        lat, lon = la, lo
                    if la.shape == lat.shape and lo.shape == lon.shape:
                        frames.update({t: v for t, v in fr.items() if t0 <= t <= t1})
                except Exception as e:  # noqa: BLE001
                    log.warning("GOES-19 file %s unreadable: %s", p, e)
        d += timedelta(days=1)
    if not frames:
        return None
    times = sorted(frames)
    arr = np.stack([frames[t] for t in times]).astype("f4")
    ever = np.isfinite(arr).any(0)
    arr = np.stack([clean_frame(a, ever) for a in arr])
    return GoesArchive(lat, lon, times, arr)


class Persistence:
    """Walks forward through the frames, keeping each pixel's latest clear value and its time."""

    def __init__(self, arc: GoesArchive, max_age_h: float):
        self.arc = arc
        self.max_age_h = max_age_h
        self.value = np.full(arc.frames.shape[1:], np.nan, "f4")
        self.when = np.full(arc.frames.shape[1:], -np.inf)   # epoch seconds
        self.i = 0

    def at(self, t: datetime) -> tuple[np.ndarray, np.ndarray, datetime | None]:
        """(value, age in hours, newest frame time used) for hour t. Call with t ascending."""
        ts = t.timestamp()
        while self.i < len(self.arc.times) and self.arc.times[self.i].timestamp() <= ts:
            f = self.arc.frames[self.i]
            ok = np.isfinite(f)
            self.value[ok] = f[ok]
            self.when[ok] = self.arc.times[self.i].timestamp()
            self.i += 1
        age = (ts - self.when) / 3600
        val = np.where(age <= self.max_age_h, self.value, np.nan)
        newest = self.arc.times[self.i - 1] if self.i else None
        return val, np.where(np.isfinite(val), age, np.nan), newest


def to_grid(val: np.ndarray, age: np.ndarray, arc: GoesArchive, lat: np.ndarray, lon: np.ndarray
            ) -> tuple[np.ndarray, np.ndarray]:
    """Bilinear interpolation of the GOES field onto cell centres (lat, lon 1-D), using only
    clear pixels. Cells with no clear pixel among their four neighbours get NaN."""
    dy = arc.lat[1] - arc.lat[0]
    dx = arc.lon[1] - arc.lon[0]
    fy = (lat - arc.lat[0]) / dy
    fx = (lon - arc.lon[0]) / dx
    y0 = np.clip(np.floor(fy).astype(int), 0, len(arc.lat) - 2)
    x0 = np.clip(np.floor(fx).astype(int), 0, len(arc.lon) - 2)
    wy = np.clip(fy - y0, 0, 1)[:, None]
    wx = np.clip(fx - x0, 0, 1)[None, :]
    num = np.zeros((len(lat), len(lon)))
    agen = np.zeros_like(num)
    den = np.zeros_like(num)
    for oy, ox, w in ((0, 0, (1 - wy) * (1 - wx)), (0, 1, (1 - wy) * wx), (1, 0, wy * (1 - wx)), (1, 1, wy * wx)):
        v = val[(y0 + oy)[:, None], (x0 + ox)[None, :]]
        a = age[(y0 + oy)[:, None], (x0 + ox)[None, :]]
        ok = np.isfinite(v)
        num += np.where(ok, w * v, 0)
        agen += np.where(ok, w * a, 0)
        den += np.where(ok, w, 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(den > 1e-6, num / den, np.nan)
        out_age = np.where(den > 1e-6, agen / den, np.nan)
    return out.astype("f4"), out_age.astype("f4")
