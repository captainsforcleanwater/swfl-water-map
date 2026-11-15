"""Build the fine (about 100 m) water mask from the USGS National Hydrography Dataset (NHD).

    python -m processing.shoreline            # download NHD (about 250 MB, cached in build/cache/nhd) and rebuild
    python -m processing.shoreline --nhd-dir DIR   # use already extracted shapefiles

Run it by hand only when the grid or the shoreline source changes. It writes:

- data/grid/water_fine.png: 1-bit image of the compute grid at fine resolution, white = water.
- data/grid/grid.json: grid definition, source and a hash of the mask (the distance cache keys on it).
- data/grid/display_100m.bin.gz and land_100m.geojson: the map's 100 m coastline (processing/display.py).

Water is NHD Sea/Ocean, Bay/Inlet, Stream/River and Foreshore areas plus Lake/Pond waterbodies.
Canals, swamps and reservoirs count as land. NHD is mapped at 1:24,000 or better, so the mask resolves
the tidal Caloosahatchee, Estero Bay and the Ten Thousand Islands at 100 m.

NHD's Sea/Ocean polygons have rectangular holes offshore (missing map tiles) and stop a few miles
out. To fill them, any cell deeper than CRM_WATER_DEPTH_M in the NOAA Coastal Relief Model
(volume 3, 3 arc-second, about 90 m) also counts as water. Where NHD says water and the relief model
says land (rivers narrower than its cells), NHD wins.

Water control structures that separate tidal water from fresh water (grid.barriers in
config/processing.json, for example the S-79 Franklin Lock on the Caloosahatchee) are cut out of the
mask. Water that does not connect to the open Gulf (inland lakes, rivers above the locks) is then
dropped, so the estuaries never borrow Lake Okeechobee's temperature or salinity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import zipfile
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw
from scipy import ndimage

from collector.common import USER_AGENT, iso, utcnow
from .config import CACHE, GRID_DIR, proc_config
from .grid import Grid

log = logging.getLogger("processing.shoreline")

NHD_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHD/HU4/Shape/NHD_H_{hu}_HU4_Shape.zip"
HU4 = ["0309", "0310"]          # Southern Florida; Peace-Tampa Bay (Charlotte Harbor)
AREA_WATER = {445: "SeaOcean", 312: "BayInlet", 460: "StreamRiver", 364: "Foreshore"}
WATERBODY_WATER = {390: "LakePond", 493: "Estuary"}
LAYERS = ["NHDArea", "NHDWaterbody"]
CRM_URL = "https://www.ngdc.noaa.gov/thredds/dodsC/crm/crm_vol3.nc"
CRM_WATER_DEPTH_M = -1.0

MASK_PATH = GRID_DIR / "water_fine.png"
GRID_JSON = GRID_DIR / "grid.json"


def download(hu: str, dest: Path) -> Path:
    """Download and extract the layers we need for one NHD subregion."""
    out = dest / hu
    if all((out / "Shape" / f"{lyr}.shp").exists() for lyr in LAYERS):
        return out
    dest.mkdir(parents=True, exist_ok=True)
    z = dest / f"NHD_H_{hu}_HU4_Shape.zip"
    if not z.exists():
        log.info("downloading NHD %s", hu)
        with requests.get(NHD_URL.format(hu=hu), stream=True, timeout=600,
                          headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            with open(z, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    with zipfile.ZipFile(z) as zf:
        for name in zf.namelist():
            if any(name.startswith(f"Shape/{lyr}.") for lyr in LAYERS):
                zf.extract(name, out)
    return out


class Rasterizer:
    def __init__(self, grid: Grid):
        self.g = grid
        self.mask = np.zeros((grid.fny, grid.fnx), bool)

    def _pix(self, pts: np.ndarray) -> np.ndarray:
        x = (pts[:, 0] - self.g.left) / self.g.fdlon - 0.5
        y = (self.g.top - pts[:, 1]) / self.g.fdlat - 0.5
        return np.column_stack([x, y])

    def add(self, shape) -> None:
        """OR one polygon (with holes) into the mask.

        Rings are combined with the even-odd rule (each ring flips the pixels inside it), so the
        result does not depend on ring orientation. Some NHD Sea/Ocean tiles have outer rings
        wound the wrong way, which an orientation test would treat as holes.
        """
        pts = np.asarray(shape.points, dtype="f8")
        if len(pts) < 3:
            return
        px = self._pix(pts)
        fx0, fy0 = np.floor(px.min(0)).astype(int) - 1
        fx1, fy1 = np.ceil(px.max(0)).astype(int) + 1
        fx0, fy0 = max(fx0, 0), max(fy0, 0)
        fx1, fy1 = min(fx1, self.g.fnx), min(fy1, self.g.fny)
        if fx1 <= fx0 or fy1 <= fy0:
            return
        feat = np.zeros((fy1 - fy0, fx1 - fx0), bool)
        parts = list(shape.parts) + [len(pts)]
        for a, b in zip(parts, parts[1:]):
            if b - a < 3:
                continue
            ring = px[a:b]
            x0, y0 = np.floor(ring.min(0)).astype(int) - 1
            x1, y1 = np.ceil(ring.max(0)).astype(int) + 1
            x0, y0 = max(x0, fx0), max(y0, fy0)
            x1, y1 = min(x1, fx1), min(y1, fy1)
            if x1 <= x0 or y1 <= y0:
                continue
            img = Image.new("1", (x1 - x0, y1 - y0), 0)
            ImageDraw.Draw(img).polygon([tuple(p) for p in ring - (x0, y0)], fill=1)
            feat[y0 - fy0:y1 - fy0, x0 - fx0:x1 - fx0] ^= np.asarray(img, dtype=bool)
        self.mask[fy0:fy1, fx0:fx1] |= feat


def crm_depth(grid: Grid) -> np.ndarray:
    """NOAA Coastal Relief Model elevation (m, positive up) sampled at fine-grid cell centres."""
    from netCDF4 import Dataset

    with Dataset(CRM_URL) as ds:
        x = np.asarray(ds["x"][:])
        y = np.asarray(ds["y"][:])
        flat, flon = grid.fine_lat(), grid.fine_lon()
        xi = np.clip(np.rint((flon - x[0]) / (x[1] - x[0])).astype(int), 0, len(x) - 1)
        yi = np.clip(np.rint((flat - y[0]) / (y[1] - y[0])).astype(int), 0, len(y) - 1)
        i0, i1 = xi.min(), xi.max() + 1
        j0, j1 = yi.min(), yi.max() + 1
        z = np.empty((j1 - j0, i1 - i0), "f4")
        for a in range(j0, j1, 500):   # OPeNDAP requests in strips
            b = min(a + 500, j1)
            z[a - j0:b - j0] = np.ma.filled(ds["z"][a:b, i0:i1], np.nan)
    return z[np.ix_(yi - j0, xi - i0)]


def cut_barriers(grid: Grid, mask: np.ndarray) -> list[str]:
    """Set fine cells within radius_m of each barrier point to land."""
    done = []
    for b in proc_config()["grid"].get("barriers", []):
        r0, c0 = grid.fine_cell_of(b["lat"], b["lon"])
        ry = int(np.ceil(b["radius_m"] / (grid.fdlat * 111320))) + 1
        rx = int(np.ceil(b["radius_m"] / (grid.fdlon * 111320 * np.cos(np.radians(b["lat"]))))) + 1
        rr, cc = np.mgrid[max(r0 - ry, 0):r0 + ry + 1, max(c0 - rx, 0):c0 + rx + 1]
        dy = (rr - r0) * grid.fdlat * 111320
        dx = (cc - c0) * grid.fdlon * 111320 * np.cos(np.radians(b["lat"]))
        m = np.hypot(dy, dx) <= b["radius_m"]
        rr, cc = rr[m], cc[m]
        ok = (rr < grid.fny) & (cc < grid.fnx)
        mask[rr[ok], cc[ok]] = False
        done.append(b["name"])
    return done


def build(grid: Grid, nhd_dirs: list[Path]) -> tuple[np.ndarray, dict]:
    import shapefile   # pyshp

    water = Rasterizer(grid)
    counts: dict[str, int] = {}
    for d in nhd_dirs:
        shp = d / "Shape"
        for lyr, keep in (("NHDArea", AREA_WATER), ("NHDWaterbody", WATERBODY_WATER)):
            r = shapefile.Reader(str(shp / lyr))
            fields = [f[0].lower() for f in r.fields[1:]]
            fi = fields.index("ftype")
            for sr in r.iterShapeRecords():
                ft = sr.record[fi]
                if ft in keep:
                    water.add(sr.shape)
                    counts[keep[ft]] = counts.get(keep[ft], 0) + 1
    log.info("reading NOAA Coastal Relief Model")
    z = crm_depth(grid)
    deep = np.nan_to_num(z, nan=1.0) <= CRM_WATER_DEPTH_M
    mask = water.mask | deep
    cut = cut_barriers(grid, mask)
    # Keep only water connected to the open Gulf (the component touching the west edge).
    lab, n = ndimage.label(mask, structure=np.ones((3, 3), bool))
    edge = np.unique(lab[:, 0])
    edge = edge[edge > 0]
    sea = np.isin(lab, edge)
    info = {"features": counts, "barriers_cut": cut,
            "cells_water_from_relief_model_only": int((sea & ~water.mask).sum()),
            "dropped_inland_water_cells": int(mask.sum() - sea.sum())}
    return sea, info


def save(grid: Grid, mask: np.ndarray, info: dict) -> None:
    GRID_DIR.mkdir(parents=True, exist_ok=True)
    Image.fromarray(mask).save(MASK_PATH, optimize=True)
    digest = hashlib.sha256(MASK_PATH.read_bytes()).hexdigest()[:16]
    meta = {
        "_comment": "Written by python -m processing.shoreline. The fine water mask is water_fine.png (white = water), on the compute grid below at fine resolution.",
        "built": iso(utcnow()),
        "source": "USGS National Hydrography Dataset (NHD), high resolution, subregions " + ", ".join(HU4)
                  + "; gaps filled from NOAA Coastal Relief Model vol. 3 where deeper than "
                  + f"{-CRM_WATER_DEPTH_M:g} m",
        "source_url": [NHD_URL.format(hu="<HU4>"), CRM_URL],
        "water_feature_types": sorted(set(AREA_WATER.values()) | set(WATERBODY_WATER.values())),
        "mask_sha256_16": digest,
        "compute_grid": {"top": grid.top, "left": grid.left, "dlat": grid.dlat, "dlon": grid.dlon,
                         "rows": grid.ny, "cols": grid.nx, "fine_factor": grid.fine},
        "output_grid": grid.describe(),
        "water_fraction_fine": round(float(mask.mean()), 4),
        **info,
    }
    GRID_JSON.write_text(json.dumps(meta, indent=1) + "\n")


def load_mask(grid: Grid) -> np.ndarray:
    m = np.asarray(Image.open(MASK_PATH), dtype=bool)
    if m.shape != (grid.fny, grid.fnx):
        raise RuntimeError(f"{MASK_PATH} is {m.shape}, grid wants {(grid.fny, grid.fnx)}; "
                           "rebuild it with python -m processing.shoreline")
    return m


def mask_hash() -> str:
    return json.loads(GRID_JSON.read_text())["mask_sha256_16"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nhd-dir", type=Path, default=None,
                    help="folder holding <HU4>/Shape/*.shp (default: download to build/cache/nhd)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    grid = Grid.from_config()
    base = args.nhd_dir or CACHE / "nhd"
    dirs = [download(hu, base) if args.nhd_dir is None else base / hu for hu in HU4]
    mask, info = build(grid, dirs)
    save(grid, mask, info)
    log.info("water mask %s: %d x %d, %.1f%% water, %s", MASK_PATH, mask.shape[1], mask.shape[0],
             100 * mask.mean(), info)
    # The map's 100 m coastline files depend on the mask, so rebuild them with it.
    from . import display
    return display.main()


if __name__ == "__main__":
    raise SystemExit(main())
