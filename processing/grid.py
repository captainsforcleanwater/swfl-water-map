"""Grid definition shared by the mask builder, the blend and the output files.

Two grids, aligned so each coarse cell holds fine_factor x fine_factor fine cells:

- coarse (about 500 m): the blend runs here. The compute grid covers the area bbox from
  config/area.json plus pad cells, so edge anchors outside the bbox still connect by water.
  The output grid is the part of the compute grid inside the bbox.
- fine (about 100 m): the water mask. Water paths are traced on it so narrow rivers stay
  open and thin barrier islands stay closed.

Rows run north to south and columns west to east. Cell (i, j) of the compute grid has its
centre at lat = top - (i + 0.5) * dlat, lon = left + (j + 0.5) * dlon.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import area_config, proc_config

KM_PER_DEG_LAT = 111.32


@dataclass(frozen=True)
class Grid:
    top: float
    left: float
    dlat: float
    dlon: float
    ny: int
    nx: int
    fine: int
    out_row0: int
    out_col0: int
    out_ny: int
    out_nx: int

    @classmethod
    def from_config(cls) -> "Grid":
        b = area_config()["bbox"]
        g = proc_config()["grid"]
        dlat, dlon = g["cell_deg_lat"], g["cell_deg_lon"]
        out_ny = math.ceil(round((b["lat_max"] - b["lat_min"]) / dlat, 6))
        out_nx = math.ceil(round((b["lon_max"] - b["lon_min"]) / dlon, 6))
        p = g["pad_cells"]
        return cls(top=b["lat_max"] + p["north"] * dlat, left=b["lon_min"] - p["west"] * dlon,
                   dlat=dlat, dlon=dlon, ny=out_ny + p["north"] + p["south"],
                   nx=out_nx + p["west"] + p["east"], fine=g["fine_factor"],
                   out_row0=p["north"], out_col0=p["west"], out_ny=out_ny, out_nx=out_nx)

    # ------------------------------------------------------------ coarse grid
    def lat(self) -> np.ndarray:
        return self.top - (np.arange(self.ny) + 0.5) * self.dlat

    def lon(self) -> np.ndarray:
        return self.left + (np.arange(self.nx) + 0.5) * self.dlon

    def out_slice(self) -> tuple[slice, slice]:
        return (slice(self.out_row0, self.out_row0 + self.out_ny),
                slice(self.out_col0, self.out_col0 + self.out_nx))

    def cell_of(self, lat: float, lon: float) -> tuple[int, int]:
        return int((self.top - lat) // self.dlat), int((lon - self.left) // self.dlon)

    # ------------------------------------------------------------ fine grid
    @property
    def fny(self) -> int:
        return self.ny * self.fine

    @property
    def fnx(self) -> int:
        return self.nx * self.fine

    @property
    def fdlat(self) -> float:
        return self.dlat / self.fine

    @property
    def fdlon(self) -> float:
        return self.dlon / self.fine

    def fine_cell_of(self, lat: float, lon: float) -> tuple[int, int]:
        return int((self.top - lat) // self.fdlat), int((lon - self.left) // self.fdlon)

    def fine_lat(self) -> np.ndarray:
        return self.top - (np.arange(self.fny) + 0.5) * self.fdlat

    def fine_lon(self) -> np.ndarray:
        return self.left + (np.arange(self.fnx) + 0.5) * self.fdlon

    def km_per_deg_lon(self, lat: float | np.ndarray) -> float | np.ndarray:
        return KM_PER_DEG_LAT * np.cos(np.radians(lat))

    def describe(self) -> dict:
        """Grid definition as written into output file headers (output part only)."""
        top = self.top - self.out_row0 * self.dlat
        left = self.left + self.out_col0 * self.dlon
        return {
            "rows": self.out_ny, "cols": self.out_nx,
            "lat_top_edge": round(top, 6), "lon_left_edge": round(left, 6),
            "dlat": self.dlat, "dlon": self.dlon,
            "lat_first_row_center": round(top - self.dlat / 2, 6),
            "lon_first_col_center": round(left + self.dlon / 2, 6),
            "row_order": "north_to_south", "crs": "EPSG:4326",
        }


def polygon_mask(grid: Grid, polygon: list, lat: np.ndarray | None = None,
                 lon: np.ndarray | None = None) -> np.ndarray:
    """True for compute-grid cell centres inside the polygon [(lon, lat), ...]."""
    lat = grid.lat() if lat is None else lat
    lon = grid.lon() if lon is None else lon
    LON, LAT = np.meshgrid(lon, lat)
    inside = np.zeros(LON.shape, bool)
    pts = [tuple(p) for p in polygon]
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        cond = (yi > LAT) != (yj > LAT)
        with np.errstate(divide="ignore", invalid="ignore"):
            xc = (xj - xi) * (LAT - yi) / (yj - yi) + xi
        inside ^= cond & (LON < xc)
        j = i
    return inside
