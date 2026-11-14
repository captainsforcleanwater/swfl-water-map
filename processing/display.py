"""Static files the map needs to draw the coastline at 100 m.

    python -m processing.display          # also run by python -m processing.shoreline

The blend grid is 500 m and a cell counts as water if any of it is water, which keeps narrow rivers
open for the water paths but makes Captiva, Cayo Costa and Gasparilla vanish and Sanibel and Pine
Island shrink if the map drew the 500 m cells. So the map draws its coastline from the 100 m mask:

- data/grid/display_100m.bin.gz (gridfile format): layer `piece`, 100 m over the output area, rows
  north to south, 5 x 5 pixels per 500 m cell. 0 = land or no data; 1 = the main water piece of
  its 500 m cell (colour it with the cell's value); k >= 2 = a secondary piece, such as the far side
  of a barrier island inside the same cell (colour it with element k - 2 of the hourly file's
  *_extra layers). The header's pieces_key must match the hourly file's pieces_key.
- data/grid/land_100m.geojson: land polygons traced from the same mask and lightly simplified, for
  drawing the shoreline as vectors.
"""

from __future__ import annotations

import json
import logging

import numpy as np

from collector.common import iso, utcnow
from . import gridfile
from .config import GRID_DIR, area_config
from .grid import Grid, polygon_mask
from .shoreline import load_mask
from .waterpaths import WaterGraph, display_pieces

log = logging.getLogger("processing.display")

PIECES_PATH = GRID_DIR / "display_100m.bin.gz"
LAND_PATH = GRID_DIR / "land_100m.geojson"
SIMPLIFY_DEG = 0.0003    # about 30 m, well under the 100 m pixel


def inside_cells(grid: Grid, graph: WaterGraph) -> np.ndarray:
    rs, cs = grid.out_slice()
    prim = graph.primary.reshape(grid.ny, grid.nx)[rs, cs]
    return (prim >= 0) & polygon_mask(grid, area_config()["polygon"])[rs, cs]


def write_pieces(grid: Grid, graph: WaterGraph) -> dict:
    piece, secondary = display_pieces(graph, inside_cells(grid, graph))
    d = grid.describe()
    f = grid.fine
    header = {
        "title": "SWFL display mask, 100 m",
        "description": "piece: 0 land or no data; 1 main water piece of the 500 m cell; k >= 2 secondary piece k-2, "
                       "valued by the hourly files' *_extra layers",
        "pieces_key": graph.key, "n_extra": int(len(secondary)), "created": iso(utcnow()),
        "grid": dict(d, rows=d["rows"] * f, cols=d["cols"] * f, dlat=grid.fdlat, dlon=grid.fdlon,
                     lat_first_row_center=round(d["lat_top_edge"] - grid.fdlat / 2, 7),
                     lon_first_col_center=round(d["lon_left_edge"] + grid.fdlon / 2, 7),
                     cells_per_blend_cell=f),
    }
    size = gridfile.write(PIECES_PATH, header, {"piece": (piece, {"encoding": "delta_tx", "scale": 1})})
    return {"bytes": size, "n_extra": int(len(secondary))}


def write_land(grid: Grid) -> dict:
    import contourpy
    from shapely.geometry import MultiPolygon, Polygon, mapping

    f = grid.fine
    rs, cs = grid.out_slice()
    water = load_mask(grid)[rs.start * f:rs.stop * f, cs.start * f:cs.stop * f]
    land = np.pad((~water).astype("f4"), 1, constant_values=0.0)   # close polygons at the edge
    lat = grid.top - (np.arange(rs.start * f - 1, rs.stop * f + 1) + 0.5) * grid.fdlat
    lon = grid.left + (np.arange(cs.start * f - 1, cs.stop * f + 1) + 0.5) * grid.fdlon
    cg = contourpy.contour_generator(x=lon, y=lat, z=land, fill_type="OuterOffset")
    points, offsets = cg.filled(0.5, 1.5)
    polys = []
    for pts, off in zip(points, offsets):
        rings = [pts[a:b] for a, b in zip(off[:-1], off[1:])]
        p = Polygon(rings[0], rings[1:]).simplify(SIMPLIFY_DEG, preserve_topology=True)
        if not p.is_empty and p.area > 0:
            polys.append(p)
    geom = mapping(MultiPolygon(polys))

    def rnd(c):
        return [[[round(x, 5), round(y, 5)] for x, y in ring] for ring in c]

    geom["coordinates"] = [rnd(poly) for poly in geom["coordinates"]]
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": geom,
        "properties": {"name": "land", "source": "data/grid/water_fine.png (USGS NHD and NOAA Coastal Relief Model, 100 m)",
                       "simplified_deg": SIMPLIFY_DEG}}]}
    LAND_PATH.write_text(json.dumps(fc, separators=(",", ":")) + "\n")
    return {"bytes": LAND_PATH.stat().st_size, "polygons": len(polys)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    grid = Grid.from_config()
    graph = WaterGraph.load(grid)
    log.info("display pieces: %s", write_pieces(grid, graph))
    log.info("land polygons: %s", write_land(grid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
