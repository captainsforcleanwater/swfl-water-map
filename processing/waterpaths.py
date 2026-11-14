"""Water-path distances on the blend grid.

The blend grid has about 500 m cells, which is too coarse to tell a 150 m river from a 300 m
barrier island. So the graph is built from the fine (about 100 m) mask:

1. Inside each coarse cell, fine water cells are grouped into connected pieces. Usually there is
   one piece. A coarse cell split by a barrier island or a mangrove point has two or more, and
   each becomes its own node, so water on either side of the island never shares a node.
2. Two nodes are linked when a fine water cell of one touches a fine water cell of the other
   (8 neighbours). The link length is the distance between the pieces' centres.
3. Each coarse cell takes its value from its largest piece (its primary node).

Distances from each station to every node come from Dijkstra's algorithm on this graph. They only
change when a station moves or the mask changes, so they are cached under build/cache/distance,
keyed by the mask hash and the station's node.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import ndimage, sparse
from scipy.sparse.csgraph import dijkstra

from .config import CACHE
from .grid import KM_PER_DEG_LAT, Grid
from .shoreline import load_mask, mask_hash

log = logging.getLogger("processing.waterpaths")

DIST_UNIT_KM = 0.05           # cached distances are uint16 in 50 m steps
UNREACHABLE = np.uint16(65535)
GRAPH_VERSION = 2
OFFSHORE_KM = 10.0           # water at least this far from any land counts as open Gulf


@dataclass
class WaterGraph:
    grid: Grid
    n_nodes: int
    node_lat: np.ndarray        # (n,) centre of each water piece
    node_lon: np.ndarray
    node_cell: np.ndarray       # (n,) flat index of the coarse cell holding the piece
    node_size: np.ndarray       # (n,) fine cells in the piece
    primary: np.ndarray         # (ny*nx,) primary node of each coarse cell, -1 for land
    water_frac: np.ndarray      # (ny, nx) share of fine cells that are water
    adj: sparse.csr_matrix      # symmetric, km
    fine_labels: np.ndarray     # (fny, fnx) node index + 1 for each fine cell, 0 = land
    shore_km: np.ndarray        # (n,) straight-line distance from the piece's centre to the nearest land
    sea_km: np.ndarray          # (n,) water-path distance to open Gulf (water >= OFFSHORE_KM from land)
    key: str

    # ---------------------------------------------------------------- construction
    @classmethod
    def load(cls, grid: Grid | None = None, cache: Path | None = None) -> "WaterGraph":
        grid = grid or Grid.from_config()
        key = f"{mask_hash()}-v{GRAPH_VERSION}"
        path = (cache or CACHE) / "graph" / f"graph_{key}.npz"
        if path.exists():
            try:
                z = np.load(path)
                adj = sparse.csr_matrix((z["adj_data"], z["adj_indices"], z["adj_indptr"]),
                                        shape=(int(z["n"]), int(z["n"])))
                return cls(grid, int(z["n"]), z["node_lat"], z["node_lon"], z["node_cell"],
                           z["node_size"], z["primary"], z["water_frac"], adj, z["fine_labels"],
                           z["shore_km"], z["sea_km"], key)
            except Exception as e:  # noqa: BLE001
                log.warning("graph cache unreadable (%s); rebuilding", e)
        g = cls.build(grid, load_mask(grid), key)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, n=g.n_nodes, node_lat=g.node_lat, node_lon=g.node_lon,
                            node_cell=g.node_cell, node_size=g.node_size, primary=g.primary,
                            water_frac=g.water_frac, adj_data=g.adj.data, adj_indices=g.adj.indices,
                            adj_indptr=g.adj.indptr, fine_labels=g.fine_labels, shore_km=g.shore_km,
                            sea_km=g.sea_km)
        return g

    @classmethod
    def build(cls, grid: Grid, mask: np.ndarray, key: str) -> "WaterGraph":
        f, ny, nx = grid.fine, grid.ny, grid.nx
        # Label water pieces inside each coarse cell: put a row and column of land between
        # cells so 8-connected labelling cannot cross a cell edge.
        e = np.zeros((ny, f + 1, nx, f + 1), bool)
        e[:, :f, :, :f] = mask.reshape(ny, f, nx, f)
        lab, n = ndimage.label(e.reshape(ny * (f + 1), nx * (f + 1)), structure=np.ones((3, 3), bool))
        fine = lab.reshape(ny, f + 1, nx, f + 1)[:, :f, :, :f].reshape(ny * f, nx * f).astype(np.int32)
        del lab, e
        # Labels come out in raster order of the expanded array; keep them as they are.
        flat = fine.ravel()
        rows = np.repeat(np.arange(ny * f), nx * f)
        cols = np.tile(np.arange(nx * f), ny * f)
        size = np.bincount(flat, minlength=n + 1)[1:].astype(np.int32)
        r_mean = np.bincount(flat, weights=rows, minlength=n + 1)[1:] / size
        c_mean = np.bincount(flat, weights=cols, minlength=n + 1)[1:] / size
        node_lat = grid.top - (r_mean + 0.5) * grid.fdlat
        node_lon = grid.left + (c_mean + 0.5) * grid.fdlon
        node_cell = (np.floor(r_mean / f).astype(np.int64) * nx + np.floor(c_mean / f).astype(np.int64))
        del rows, cols, flat

        # Links between pieces that touch across a coarse cell edge.
        pairs = []
        H, W = fine.shape
        for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            if dx >= 0:
                a, b = fine[0:H - dy, 0:W - dx], fine[dy:H, dx:W]
            else:
                a, b = fine[0:H - dy, -dx:W], fine[dy:H, 0:W + dx]
            m = (a > 0) & (b > 0) & (a != b)
            if m.any():
                pairs.append(np.stack([a[m], b[m]], 1))
        p = np.unique(np.sort(np.concatenate(pairs), 1), axis=0) - 1
        lat_mid = 0.5 * (node_lat[p[:, 0]] + node_lat[p[:, 1]])
        dy_km = (node_lat[p[:, 0]] - node_lat[p[:, 1]]) * KM_PER_DEG_LAT
        dx_km = (node_lon[p[:, 0]] - node_lon[p[:, 1]]) * KM_PER_DEG_LAT * np.cos(np.radians(lat_mid))
        w = np.maximum(np.hypot(dy_km, dx_km), 0.01)
        adj = sparse.coo_matrix((np.concatenate([w, w]), (np.concatenate([p[:, 0], p[:, 1]]),
                                np.concatenate([p[:, 1], p[:, 0]]))), shape=(n, n)).tocsr()

        # Primary node of each coarse cell: its largest piece.
        order = np.lexsort((size, node_cell))         # by cell, then size ascending
        last = np.r_[node_cell[order][1:] != node_cell[order][:-1], True]
        primary = np.full(ny * nx, -1, np.int64)
        primary[node_cell[order][last]] = order[last]
        water_frac = mask.reshape(ny, f, nx, f).mean((1, 3)).astype("f4")
        # Distance to land at each piece's centre, and water-path distance to open Gulf.
        edt = ndimage.distance_transform_edt(mask, sampling=(grid.fdlat * KM_PER_DEG_LAT,
                                                             grid.fdlon * KM_PER_DEG_LAT * np.cos(np.radians(26))))
        rr = np.clip(np.rint(r_mean).astype(int), 0, grid.fny - 1)
        cc = np.clip(np.rint(c_mean).astype(int), 0, grid.fnx - 1)
        shore = edt[rr, cc].astype("f4")
        del edt
        off = np.nonzero(shore >= OFFSHORE_KM)[0]
        if len(off):
            sea = dijkstra(adj, directed=False, indices=off, min_only=True).astype("f4")
        else:
            sea = np.full(n, np.inf, "f4")
        log.info("water graph: %d nodes, %d links, %d coarse water cells", n, len(p), int((primary >= 0).sum()))
        return cls(grid, n, node_lat, node_lon, node_cell, size, primary, water_frac, adj, fine,
                   shore, sea, key)

    # ---------------------------------------------------------------- stations
    def snap(self, lat: float, lon: float, max_km: float) -> tuple[int, float] | None:
        """Node for a station location: its own fine cell if water, else the nearest water cell
        within max_km. Returns (node, snap distance km) or None."""
        g = self.grid
        r, c = g.fine_cell_of(lat, lon)
        if not (0 <= r < g.fny and 0 <= c < g.fnx):
            return None
        if self.fine_labels[r, c] > 0:
            return int(self.fine_labels[r, c]) - 1, 0.0
        rad = int(np.ceil(max_km / (g.fdlat * KM_PER_DEG_LAT))) + 1
        r0, r1 = max(r - rad, 0), min(r + rad + 1, g.fny)
        c0, c1 = max(c - rad, 0), min(c + rad + 1, g.fnx)
        win = self.fine_labels[r0:r1, c0:c1]
        rr, cc = np.nonzero(win)
        if not len(rr):
            return None
        dy = (rr + r0 - r) * g.fdlat * KM_PER_DEG_LAT
        dx = (cc + c0 - c) * g.fdlon * KM_PER_DEG_LAT * np.cos(np.radians(lat))
        d = np.hypot(dy, dx)
        k = int(np.argmin(d))
        if d[k] > max_km:
            return None
        return int(win[rr[k], cc[k]]) - 1, float(d[k])

    def distances(self, nodes: list[int], cache: Path | None = None) -> np.ndarray:
        """Water-path distance (km) from each node in `nodes` to every node. Rows follow `nodes`.
        np.inf where water does not connect."""
        cdir = (cache or CACHE) / "distance" / self.key
        cdir.mkdir(parents=True, exist_ok=True)
        uniq = sorted(set(nodes))
        have: dict[int, np.ndarray] = {}
        todo = []
        for nd in uniq:
            p = cdir / f"node_{nd}.npy"
            if p.exists():
                try:
                    have[nd] = np.load(p)
                    continue
                except Exception:  # noqa: BLE001
                    pass
            todo.append(nd)
        for i in range(0, len(todo), 8):
            batch = todo[i:i + 8]
            d = dijkstra(self.adj, directed=False, indices=batch)
            for nd, row in zip(batch, d):
                q = np.where(np.isfinite(row), np.minimum(np.rint(row / DIST_UNIT_KM), 65534), 65535)
                q = q.astype(np.uint16)
                np.save(cdir / f"node_{nd}.npy", q)
                have[nd] = q
        if todo:
            log.info("computed water-path distances for %d new station nodes (%d cached)",
                     len(todo), len(uniq) - len(todo))
        out = np.empty((len(nodes), self.n_nodes), "f4")
        for i, nd in enumerate(nodes):
            q = have[nd]
            out[i] = np.where(q == UNREACHABLE, np.inf, q.astype("f4") * DIST_UNIT_KM)
        return out

    def nearest_source(self, sources: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """For every node, the nearest node in `sources` by water path and the distance (km).
        Nodes that cannot reach any source get -1 and inf."""
        if len(sources) == 0:
            return np.full(self.n_nodes, -1), np.full(self.n_nodes, np.inf)
        d, _, src = dijkstra(self.adj, directed=False, indices=sources, min_only=True,
                             return_predecessors=True)
        return src, d
