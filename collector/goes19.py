"""Archive GOES-19 hourly SST for the study area.

The CoastWatch Caribbean/Gulf node keeps only about 5 days of hourly frames, so
every run checks the last few days and fetches any hour we have not archived.

Each UTC day becomes one compressed NetCDF file (time x lat x lon, SST in
hundredths of a degree C). In GitHub Actions the files are attached to a monthly
GitHub Release (tag goes19-sst-hourly-YYYY-MM) and data/goes19/index.json records
which hours are archived. Run locally, the files go to build/goes19 instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from urllib.parse import quote

import numpy as np
from netCDF4 import Dataset

from .common import DATA, ROOT, Area, iso, log, parse_time, utcnow

ERDDAP = "https://cwcgom.aoml.noaa.gov/erddap/griddap/goes19SSThourly"
FILL = -32768
SCALE = 0.01


# ---------------------------------------------------------------- storage backends

class LocalStore:
    """Keeps daily files and the index under build/goes19. For testing."""

    def __init__(self, root: Path | None = None):
        self.root = root or ROOT / "build" / "goes19"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"

    def get(self, asset: str, dest: Path, tag: str = "") -> bool:
        src = self.root / asset
        if src.exists():
            shutil.copy(src, dest)
            return True
        return False

    def put(self, asset: str, src: Path, tag: str) -> None:
        shutil.copy(src, self.root / asset)

    def location(self, tag: str, asset: str) -> str:
        return str(self.root / asset)


class ReleaseStore:
    """Attaches daily files to monthly GitHub Releases using the gh CLI."""

    def __init__(self, repo: str):
        self.repo = repo
        self.index_path = DATA / "goes19" / "index.json"
        self._known_tags: set[str] = set()

    def _gh(self, *args, check=True):
        return subprocess.run(["gh", *args, "--repo", self.repo], capture_output=True, text=True, check=check)

    def _ensure_release(self, tag: str):
        if tag in self._known_tags:
            return
        if self._gh("release", "view", tag, check=False).returncode != 0:
            month = tag.rsplit("-", 2)[-2:]
            self._gh("release", "create", tag, "--title", f"GOES-19 hourly SST {'-'.join(month)}",
                     "--notes", "Hourly GOES-19 sea surface temperature for the SWFL study area, one "
                     "NetCDF file per UTC day. Added automatically by the collector. Source: NOAA "
                     "CoastWatch Caribbean/Gulf node, dataset goes19SSThourly.", "--latest=false")
        self._known_tags.add(tag)

    def get(self, asset: str, dest: Path, tag: str) -> bool:
        r = self._gh("release", "download", tag, "--pattern", asset, "--dir", str(dest.parent),
                     "--clobber", check=False)
        got = dest.parent / asset
        if r.returncode == 0 and got.exists():
            if got != dest:
                got.rename(dest)
            return True
        return False

    def put(self, asset: str, src: Path, tag: str) -> None:
        self._ensure_release(tag)
        named = src.parent / asset
        if src != named:
            shutil.copy(src, named)
        self._gh("release", "upload", tag, str(named), "--clobber")

    def location(self, tag: str, asset: str) -> str:
        return f"https://github.com/{self.repo}/releases/download/{tag}/{asset}"


# ---------------------------------------------------------------- helpers

def tag_for(day: str) -> str:
    return f"goes19-sst-hourly-{day[:7]}"


def asset_for(day: str) -> str:
    return f"goes19_sst_hourly_{day}.nc"


def server_times(s) -> list[datetime]:
    r = s.get(f"{ERDDAP}.csv?time", timeout=60)
    r.raise_for_status()
    lines = r.text.strip().splitlines()[2:]
    return [parse_time(x) for x in lines if x.strip()]


def fetch_hour(s, t: datetime, area: Area):
    q = f"sst[({iso(t)})][({area.lat_min}):({area.lat_max})][({area.lon_min}):({area.lon_max})]"
    r = s.get(f"{ERDDAP}.nc?{quote(q, safe='()[]:')}", timeout=120)
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".nc") as tmp:
        tmp.write(r.content)
        tmp.flush()
        with Dataset(tmp.name) as ds:
            lat = np.array(ds["latitude"][:], dtype="f4")
            lon = np.array(ds["longitude"][:], dtype="f4")
            sst = np.ma.filled(ds["sst"][0, :, :].astype("f4"), np.nan)
    return lat, lon, sst


def read_day(path: Path):
    with Dataset(path) as ds:
        lat = np.array(ds["latitude"][:], dtype="f4")
        lon = np.array(ds["longitude"][:], dtype="f4")
        times = [datetime.fromtimestamp(float(x), tz=timezone.utc) for x in ds["time"][:]]
        sst = np.ma.filled(ds["sst"][:].astype("f4"), np.nan)   # auto scale applied
    frames = {t: sst[i] for i, t in enumerate(times)}
    return lat, lon, frames


def write_day(path: Path, lat, lon, frames: dict, day: str):
    times = sorted(frames)
    with Dataset(path, "w", format="NETCDF4") as ds:
        ds.title = f"GOES-19 hourly SST, SWFL study area, {day} UTC"
        ds.source = f"{ERDDAP} (NOAA CoastWatch Caribbean/Gulf node)"
        ds.history = f"{iso(utcnow())} packed by swfl-water-map collector"
        ds.Conventions = "CF-1.8"
        ds.createDimension("time", None)
        ds.createDimension("latitude", len(lat))
        ds.createDimension("longitude", len(lon))
        vt = ds.createVariable("time", "f8", ("time",))
        vt.units = "seconds since 1970-01-01T00:00:00Z"
        vt.standard_name = "time"
        vt[:] = [t.timestamp() for t in times]
        vy = ds.createVariable("latitude", "f4", ("latitude",))
        vy.units = "degrees_north"
        vy[:] = lat
        vx = ds.createVariable("longitude", "f4", ("longitude",))
        vx.units = "degrees_east"
        vx[:] = lon
        v = ds.createVariable("sst", "i2", ("time", "latitude", "longitude"), zlib=True, complevel=9,
                              shuffle=True, fill_value=FILL, chunksizes=(1, len(lat), len(lon)))
        v.scale_factor = SCALE
        v.add_offset = 0.0
        v.units = "degree_C"
        v.standard_name = "sea_surface_temperature"
        v.long_name = "GOES-19 hourly sea surface temperature"
        arr = np.stack([frames[t] for t in times])
        bad = ~np.isfinite(arr)
        v[:] = np.ma.array(np.where(bad, 0.0, arr), mask=bad)


# ---------------------------------------------------------------- main entry

def archive(s, area: Area, lookback_days: int, store) -> dict:
    index_path: Path = store.index_path
    index = json.loads(index_path.read_text()) if index_path.exists() else {"days": {}}
    days = index.setdefault("days", {})

    avail = server_times(s)
    cutoff = utcnow() - timedelta(days=lookback_days + 1)
    avail = [t for t in avail if t >= cutoff]
    have = {(d, h) for d, info in days.items() for h in info.get("hours", [])}
    missing = [t for t in avail if (t.strftime("%Y-%m-%d"), t.strftime("%H")) not in have]
    status = {"server_first": iso(min(avail)) if avail else None,
              "server_last": iso(max(avail)) if avail else None,
              "missing_before_run": len(missing), "added": 0, "errors": {}}
    if not missing:
        return status

    by_day: dict[str, list[datetime]] = {}
    for t in missing:
        by_day.setdefault(t.strftime("%Y-%m-%d"), []).append(t)

    work = Path(tempfile.mkdtemp(prefix="goes19-"))
    try:
        for day, hours in sorted(by_day.items()):
            asset, tag = asset_for(day), tag_for(day)
            path = work / asset
            frames, lat, lon = {}, None, None
            if day in days:
                ok = store.get(asset, path, tag)
                if ok:
                    lat, lon, frames = read_day(path)
                else:
                    # Index says we have hours but the file is gone: refetch whatever the server still has.
                    log.warning("GOES-19 archive file for %s missing; rebuilding from server", day)
                    hours = sorted({*hours, *[t for t in avail if t.strftime("%Y-%m-%d") == day]})
            added = 0
            for t in hours:
                try:
                    hlat, hlon, sst = fetch_hour(s, t, area)
                except Exception as e:  # noqa: BLE001
                    status["errors"][iso(t)] = str(e)[:200]
                    continue
                if lat is None:
                    lat, lon = hlat, hlon
                elif hlat.shape != lat.shape or hlon.shape != lon.shape or not np.allclose(hlat, lat):
                    status["errors"][iso(t)] = "grid changed; hour skipped"
                    continue
                frames[t] = sst
                added += 1
            if not added:
                continue
            write_day(path, lat, lon, frames, day)
            store.put(asset, path, tag)
            hrs = sorted({t.strftime("%H") for t in frames})
            days[day] = {
                "hours": hrs, "n_hours": len(hrs), "release_tag": tag, "asset": asset,
                "url": store.location(tag, asset), "bytes": path.stat().st_size,
                "updated": iso(utcnow()),
            }
            status["added"] += added
            # Save after each day so a later failure does not lose the record.
            index["dataset"] = ERDDAP
            index["grid"] = {"lat_min": float(lat.min()), "lat_max": float(lat.max()),
                             "lon_min": float(lon.min()), "lon_max": float(lon.max()),
                             "n_lat": int(len(lat)), "n_lon": int(len(lon))}
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return status
