"""Leave-one-out check: blend without each sensor and compare with what it read.

    python -m processing.validate --start 2026-11-11T17 --end 2026-11-14T17 --out docs/validation

For every hour and every station used by the grids, the station is removed, the blend is redone
from the others (same method and settings as processing.run), and the blended value at the
station is compared with its reading. Also reported, for comparison:
- temperature from the satellite background alone (GOES-19 carried forward, no sensors)
- salinity from the background alone (Gulf value and regional mean, no local detail)

Writes leave_one_out.md (summary) and, with --csv, leave_one_out.csv (one row per station and hour).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from collector.common import SAL, TEMP, iso, parse_time, utcnow
from . import blend, clean, goes
from .config import proc_config
from .grid import Grid
from .run import floor_hour, hours_between, persistence_at
from .waterpaths import WaterGraph

log = logging.getLogger("processing.validate")


@dataclass
class HourInputs:
    time: datetime
    t_ids: list
    t_vals: np.ndarray
    t_bg: np.ndarray        # satellite background at each station node
    t_age: np.ndarray       # hours since the satellite value
    t_ext: np.ndarray       # km of water path from the nearest satellite cell
    s_ids: list
    s_vals: np.ndarray


def prepare(start: datetime, end: datetime, now: datetime):
    cfg = proc_config()
    bc = cfg["blend"]
    grid = Grid.from_config()
    graph = WaterGraph.load(grid)
    cres = clean.run(start - timedelta(hours=1), now, now, graph=graph)
    nodes = cres.stats["station_nodes"]
    ids = sorted(nodes)
    row_of = {s: i for i, s in enumerate(ids)}
    D = graph.distances([nodes[i] for i in ids])
    Dss = D[:, [nodes[i] for i in ids]]
    arc = goes.load(start - timedelta(days=bc["goes_persistence_days"]), end)
    h = cres.grid
    hours = []
    for H in hours_between(start, end):
        hh = h[h["time"] == pd.Timestamp(H)]
        t = hh[hh["variable"] == TEMP].sort_values("station")
        s = hh[hh["variable"] == SAL].sort_values("station")
        if t.empty and s.empty:
            continue
        tn = np.array([nodes[i] for i in t["station"]], int)
        if arc is not None and len(tn):
            val, age, _ = persistence_at(arc, H, bc["goes_persistence_days"] * 24)
            sat, sat_age = goes.to_grid(val, age, arc, grid.lat()[::-1], grid.lon())
            sat, sat_age = sat[::-1].ravel(), sat_age[::-1].ravel()
            prim = graph.primary
            covered = (prim >= 0) & np.isfinite(sat)
            src_nodes = prim[covered]
            Bn = np.full(graph.n_nodes, np.nan)
            An = np.full(graph.n_nodes, np.nan)
            Bn[src_nodes], An[src_nodes] = sat[covered], sat_age[covered]
            src, ext = graph.nearest_source(src_nodes)
            ok = src[tn] >= 0
            bg = np.where(ok, Bn[np.where(ok, src[tn], 0)], np.nan)
            ag = np.where(ok, An[np.where(ok, src[tn], 0)], np.nan)
            ex = np.where(ok, ext[tn], np.inf)
        else:
            bg = ag = np.full(len(tn), np.nan)
            ex = np.full(len(tn), np.inf)
        hours.append(HourInputs(H, t["station"].tolist(), t["value"].to_numpy(), bg, ag, ex,
                                s["station"].tolist(), s["value"].to_numpy()))
    meta = {s.id: s for s in cres.stations}
    return hours, Dss, row_of, nodes, graph, meta


def temp_sb(age, ext, tc):
    sb = np.sqrt(tc["sat_error"] ** 2 + (tc["age_error_per_hour"] * age) ** 2
                 + (tc["extension_error_per_km"] * ext) ** 2)
    return np.where(np.isfinite(sb), np.minimum(sb, tc["max_background_error"]), tc["max_background_error"])


def loo_temperature(hours, Dss, row_of, tc) -> pd.DataFrame:
    rows = []
    for hi in hours:
        n = len(hi.t_ids)
        if n < 2 or np.isnan(hi.t_bg).all():
            continue
        r = np.array([row_of[i] for i in hi.t_ids])
        d = Dss[np.ix_(r, r)]
        sb = temp_sb(hi.t_age, hi.t_ext, tc)
        for k in range(n):
            o = np.arange(n) != k
            vals = hi.t_vals[o]
            ok_ = np.nonzero(o)[0]
            reg_o = blend.regional_mean(vals, d[np.ix_(o, o)], d[np.ix_(o, o)], tc["regional_length_km"], tc["declutter_km"])
            reg_k = blend.regional_mean(vals, d[np.ix_(o, [k])], d[np.ix_(o, o)], tc["regional_length_km"], tc["declutter_km"])
            bg_o = blend.blend_with_regional(hi.t_bg[ok_], hi.t_ext[ok_], reg_o, tc["extension_blend_km"])
            bg_k = blend.blend_with_regional(hi.t_bg[[k]], hi.t_ext[[k]], reg_k, tc["extension_blend_km"])
            est, err = blend.oi(bg_k, sb[[k]], bg_o, sb[o], vals, d[np.ix_(o, o)],
                                d[np.ix_(o, [k])], tc["length_km"], tc["obs_error"])
            rows.append((hi.time, hi.t_ids[k], hi.t_vals[k], float(est[0]), float(err[0]), hi.t_bg[k],
                         hi.t_age[k], hi.t_ext[k]))
    return pd.DataFrame(rows, columns=["time", "station", "obs", "est", "est_error", "satellite", "sat_age_h", "ext_km"])


def loo_salinity(hours, Dss, row_of, nodes, graph, sc) -> pd.DataFrame:
    rows = []
    shore = graph.shore_km
    sea = graph.sea_km
    for hi in hours:
        n = len(hi.s_ids)
        if n < 2:
            continue
        r = np.array([row_of[i] for i in hi.s_ids])
        nd = np.array([nodes[i] for i in hi.s_ids])
        d = Dss[np.ix_(r, r)]
        for k in range(n):
            o = np.arange(n) != k
            vals = hi.s_vals[o]
            off = shore[nd[o]] >= sc["offshore_km"]
            gulf = float(np.median(vals[off])) if off.any() else sc["gulf_default_psu"]

            def bg_at(cols):
                reg = blend.regional_mean(vals, d[np.ix_(o, cols)], d[np.ix_(o, o)], sc["regional_length_km"],
                                          sc["declutter_km"])
                reg = np.where(np.isfinite(reg), reg, gulf)
                w = np.exp(-np.asarray(sea[nd[cols]], "f8") / sc["sea_blend_km"])
                return w * gulf + (1 - w) * reg

            bg_o = bg_at(np.nonzero(o)[0])
            bg_k = bg_at(np.array([k]))
            sb = sc["background_error"]
            est, err = blend.oi(bg_k, np.array([sb]), bg_o, np.full(o.sum(), sb), vals, d[np.ix_(o, o)],
                                d[np.ix_(o, [k])], sc["length_km"], sc["obs_error"])
            rows.append((hi.time, hi.s_ids[k], hi.s_vals[k], max(float(est[0]), 0.0), float(err[0]), float(bg_k[0])))
    return pd.DataFrame(rows, columns=["time", "station", "obs", "est", "est_error", "background"])


def summarize(df: pd.DataFrame, base_col: str) -> dict:
    e = df["est"] - df["obs"]
    b = (df[base_col] - df["obs"]).dropna()
    return {"n": int(len(e)), "median_abs": float(np.median(np.abs(e))), "mean_abs": float(np.mean(np.abs(e))),
            "rmse": float(np.sqrt(np.mean(e ** 2))), "p90_abs": float(np.percentile(np.abs(e), 90)),
            "max_abs": float(np.max(np.abs(e))), "bias": float(np.mean(e)),
            "baseline_median_abs": float(np.median(np.abs(b))) if len(b) else None,
            "baseline_rmse": float(np.sqrt(np.mean(b ** 2))) if len(b) else None}


def report(tdf, sdf, meta, out: Path, period: str, csv: bool = False) -> str:
    out.mkdir(parents=True, exist_ok=True)
    if csv:
        both = pd.concat([tdf.assign(variable="water_temp_c"), sdf.assign(variable="salinity_psu")], ignore_index=True)
        both["time"] = both["time"].map(iso)
        both.round(3).to_csv(out / "leave_one_out.csv", index=False)
    ts, ss = summarize(tdf, "satellite"), summarize(sdf, "background")
    lines = ["# Leave-one-out check", "",
             f"Period: {period}. Generated {iso(utcnow())} by `python -m processing.validate`.", "",
             "Each station is left out in turn, the hour is blended from the others with the settings in",
             "`config/processing.json`, and the blended value at the station is compared with its reading.",
             "Errors are blended minus measured. Temperature in degrees C (F in brackets), salinity in PSU.", "",
             "| | Temperature | Salinity |", "|---|---:|---:|",
             f"| Station-hours | {ts['n']} | {ss['n']} |",
             f"| Typical error (median absolute) | {ts['median_abs']:.2f} ({ts['median_abs'] * 1.8:.2f} F) | {ss['median_abs']:.2f} |",
             f"| Mean absolute error | {ts['mean_abs']:.2f} ({ts['mean_abs'] * 1.8:.2f} F) | {ss['mean_abs']:.2f} |",
             f"| RMS error | {ts['rmse']:.2f} ({ts['rmse'] * 1.8:.2f} F) | {ss['rmse']:.2f} |",
             f"| 90% of errors below | {ts['p90_abs']:.2f} ({ts['p90_abs'] * 1.8:.2f} F) | {ss['p90_abs']:.2f} |",
             f"| Worst single hour | {ts['max_abs']:.2f} ({ts['max_abs'] * 1.8:.2f} F) | {ss['max_abs']:.2f} |",
             f"| Bias | {ts['bias']:+.2f} | {ss['bias']:+.2f} |",
             f"| Without sensors: typical error | {ts['baseline_median_abs']:.2f} (satellite only) | {ss['baseline_median_abs']:.2f} (background only) |",
             f"| Without sensors: RMS error | {ts['baseline_rmse']:.2f} | {ss['baseline_rmse']:.2f} |", ""]
    for name, df in (("Temperature", tdf), ("Salinity", sdf)):
        g = df.assign(abs_err=(df["est"] - df["obs"]).abs(), err=df["est"] - df["obs"]).groupby("station").agg(
            hours=("obs", "size"), median_abs=("abs_err", "median"), max_abs=("abs_err", "max"),
            bias=("err", "mean"), measured=("obs", "mean")).sort_values("median_abs", ascending=False)
        unit = "C" if name == "Temperature" else "PSU"
        lines += [f"## {name}: stations with the largest errors", "",
                  f"| Station | Name | Lat | Lon | Hours | Typical error ({unit}) | Worst ({unit}) | Bias ({unit}) | Mean reading |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for sid, r in g.head(12).iterrows():
            m = meta.get(sid)
            lines.append(f"| `{sid}` | {m.name if m else ''} | {m.lat:.3f} | {m.lon:.3f} | {r.hours} | "
                         f"{r.median_abs:.2f} | {r.max_abs:.2f} | {r.bias:+.2f} | {r.measured:.2f} |")
        lines.append("")
    text = "\n".join(lines) + "\n"
    (out / "leave_one_out.md").write_text(text)
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--now", default=None)
    ap.add_argument("--out", type=Path, default=Path("docs/validation"))
    ap.add_argument("--csv", action="store_true", help="also write one row per station and hour")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    start, end = floor_hour(parse_time(a.start)), floor_hour(parse_time(a.end))
    now = parse_time(a.now) if a.now else utcnow()
    hours, Dss, row_of, nodes, graph, meta = prepare(start, end, now)
    bc = proc_config()["blend"]
    tdf = loo_temperature(hours, Dss, row_of, bc["temperature"])
    sdf = loo_salinity(hours, Dss, row_of, nodes, graph, bc["salinity"])
    print(report(tdf, sdf, meta, a.out, f"{iso(start)} to {iso(end)}", a.csv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
