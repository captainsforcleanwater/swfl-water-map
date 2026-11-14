"""Blend one hour: temperature (satellite shape, sensor truth) and salinity (sensors only).

Both use optimal interpolation (OI) with a correlation that falls off with water-path distance d,
rho(d) = (1 + d/L) exp(-d/L) (the "SOAR" function common in ocean analyses), so a reading spreads
along channels and around islands, never across land. OI also handles clusters: ten stations in
one corner of Florida Bay do not outvote one buoy.

Temperature
  background B: GOES-19, each pixel carried forward from its last clear hour (goes.py), on the
  blend grid. Water the satellite never sees (rivers, creeks, back bays) starts from the value of
  the nearest satellite cell by water path and fades, over extension_blend_km of water path, into
  a regional mean of the sensors (same weighting as the salinity regional field). Without this, a
  river far from any clear pixel would inherit one stale coastal pixel.
  background error sb: grows with the age of the satellite value and with that water-path
  extension distance, capped at max_background_error.
  analysis T = B + corrections, where the corrections are sensor-minus-B at each station,
  spread by OI.
Salinity
  background: Gulf salinity (median of the offshore buoys this hour) in open water, fading with
  water-path distance from open water into a smooth regional field: a Gaussian-weighted mean of
  all stations by water path, where stations bunched within declutter_km share their weight.
  So the inner shelf stays near Gulf salinity, and a back bay or river with no sensor of its own
  leans on the nearest sensors up the estuary rather than on the Gulf. OI then adds local detail.

Each cell also gets the OI's expected error, turned into a 0-100 confidence:
  confidence = 100 * (1 - error / confidence_error_scale), floored at 0.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .waterpaths import WaterGraph


@dataclass
class Obs:
    ids: list[str]
    nodes: np.ndarray       # (n,) graph node of each station
    values: np.ndarray      # (n,)
    rows: np.ndarray        # (n,) row of each station in the distance matrix


@dataclass
class Analysis:
    value: np.ndarray       # (m,) at target nodes
    error: np.ndarray       # (m,) expected 1-sigma error
    confidence: np.ndarray  # (m,) 0-100
    obs_background: np.ndarray   # (n,) background at each station


def soar(d, length_km):
    x = np.asarray(d, "f8") / length_km
    with np.errstate(invalid="ignore", over="ignore"):
        r = (1 + x) * np.exp(-x)
    return np.where(np.isfinite(x), r, 0.0)


def oi(target_bg, target_sb, obs_bg, obs_sb, y, d_oo, d_ot, length_km, obs_err):
    """Optimal interpolation. d_oo: (n, n) and d_ot: (n, m) water-path distances in km."""
    n = len(y)
    if n == 0:
        return target_bg.copy(), target_sb.copy()
    rho_oo = soar(d_oo, length_km)
    C = obs_sb[:, None] * obs_sb[None, :] * rho_oo + np.eye(n) * np.asarray(obs_err) ** 2
    K = np.linalg.inv(C)
    w = K @ (y - obs_bg)
    G = obs_sb[:, None] * soar(d_ot, length_km)
    value = target_bg + target_sb * (w @ G)
    q = np.einsum("im,im->m", K @ G, G)
    err = target_sb * np.sqrt(np.clip(1 - q, 1e-4, None))
    return value, err


def regional_mean(y, d_ot, d_oo, length_km, declutter_km):
    """Gaussian-weighted mean of station values by water path, with declustering weights."""
    a = 1.0 / np.exp(-(d_oo / declutter_km) ** 2).sum(1)
    E = (d_ot / length_km) ** 2
    finite = np.isfinite(E)
    emin = np.where(finite.any(0), np.where(finite, E, np.inf).min(0), 0)
    k = np.where(finite, np.exp(-(E - emin)), 0) * a[:, None]
    s = k.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(s > 0, (k * y[:, None]).sum(0) / s, np.nan)


def confidence(err, scale):
    return np.clip(np.rint(100 * (1 - err / scale)), 0, 100).astype(np.uint8)


def temperature_background(graph: WaterGraph, sat_cells: np.ndarray, age_cells: np.ndarray, cfg: dict
                           ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Satellite background value, its error and the water-path km to the nearest satellite cell,
    at every graph node.

    sat_cells, age_cells: (ny, nx) on the compute grid (NaN where no satellite value)."""
    prim = graph.primary
    flat_v = sat_cells.ravel()
    flat_a = age_cells.ravel()
    covered = (prim >= 0) & np.isfinite(flat_v)
    src_nodes = prim[covered]
    B = np.full(graph.n_nodes, np.nan)
    A = np.full(graph.n_nodes, np.nan)
    if len(src_nodes) == 0:
        return B, np.full(graph.n_nodes, cfg["max_background_error"]), np.full(graph.n_nodes, np.inf)
    B[src_nodes] = flat_v[covered]
    A[src_nodes] = flat_a[covered]
    src, ext = graph.nearest_source(src_nodes)
    ok = src >= 0
    Bf = np.full(graph.n_nodes, np.nan)
    Af = np.full(graph.n_nodes, np.nan)
    Bf[ok] = B[src[ok]]
    Af[ok] = A[src[ok]]
    ext = np.where(ok, ext, np.inf)
    sb = np.sqrt(cfg["sat_error"] ** 2 + (cfg["age_error_per_hour"] * Af) ** 2
                 + (cfg["extension_error_per_km"] * ext) ** 2)
    sb = np.where(np.isfinite(sb), np.minimum(sb, cfg["max_background_error"]), cfg["max_background_error"])
    return Bf, sb, ext


def blend_with_regional(bg, ext, reg, blend_km):
    """Satellite background faded into the sensors' regional mean with distance from satellite cells."""
    w = np.exp(-np.asarray(ext, "f8") / blend_km)
    out = np.where(np.isfinite(bg), w * np.nan_to_num(bg) + (1 - w) * reg, reg)
    return np.where(np.isfinite(out), out, np.where(np.isfinite(bg), bg, np.nan))


def analyse_temperature(obs: Obs, D: np.ndarray, targets: np.ndarray, B: np.ndarray, sb: np.ndarray,
                        ext: np.ndarray, cfg: dict) -> Analysis:
    """D: (stations, nodes) distance matrix; obs.rows index into it."""
    d_oo = D[obs.rows][:, obs.nodes]
    d_ot = D[obs.rows][:, targets]
    sb_t, sb_o = sb[targets].copy(), sb[obs.nodes].copy()
    if len(obs.values):
        reg_t = regional_mean(obs.values, d_ot, d_oo, cfg["regional_length_km"], cfg["declutter_km"])
        reg_o = regional_mean(obs.values, d_oo, d_oo, cfg["regional_length_km"], cfg["declutter_km"])
    else:
        reg_t, reg_o = np.full(len(targets), np.nan), np.full(0, np.nan)
    bg_t = blend_with_regional(B[targets], ext[targets], reg_t, cfg["extension_blend_km"])
    bg_o = blend_with_regional(B[obs.nodes], ext[obs.nodes], reg_o, cfg["extension_blend_km"])
    # No satellite value and no sensor reaches a node: nothing to show there.
    val, err = oi(bg_t, sb_t, bg_o, sb_o, obs.values, d_oo, d_ot, cfg["length_km"], cfg["obs_error"])
    return Analysis(val, err, confidence(np.where(np.isfinite(val), err, np.inf), cfg["confidence_error_scale"]), bg_o)


def salinity_background(obs: Obs, D: np.ndarray, nodes: np.ndarray, graph: WaterGraph, cfg: dict
                        ) -> tuple[np.ndarray, float]:
    """Background salinity at `nodes`, and the Gulf value used."""
    off = graph.shore_km[obs.nodes] >= cfg["offshore_km"]
    gulf = float(np.median(obs.values[off])) if off.any() else cfg["gulf_default_psu"]
    d_on = D[obs.rows][:, nodes]
    d_oo = D[obs.rows][:, obs.nodes]
    reg = regional_mean(obs.values, d_on, d_oo, cfg["regional_length_km"], cfg["declutter_km"])
    reg = np.where(np.isfinite(reg), reg, gulf)
    w = np.exp(-np.asarray(graph.sea_km[nodes], "f8") / cfg["sea_blend_km"])
    return w * gulf + (1 - w) * reg, gulf


def analyse_salinity(obs: Obs, D: np.ndarray, targets: np.ndarray, graph: WaterGraph, cfg: dict) -> Analysis:
    m = len(targets)
    if len(obs.values) == 0:
        nan = np.full(m, np.nan)
        return Analysis(nan, nan, np.zeros(m, np.uint8), np.full(0, np.nan))
    d_oo = D[obs.rows][:, obs.nodes]
    d_ot = D[obs.rows][:, targets]
    bg_t, _ = salinity_background(obs, D, targets, graph, cfg)
    bg_o, _ = salinity_background(obs, D, obs.nodes, graph, cfg)
    sb = cfg["background_error"]
    val, err = oi(bg_t, np.full(m, sb), bg_o, np.full(len(bg_o), sb), obs.values, d_oo, d_ot,
                  cfg["length_km"], cfg["obs_error"])
    val = np.clip(val, 0, None)
    return Analysis(val, err, confidence(err, cfg["confidence_error_scale"]), bg_o)
