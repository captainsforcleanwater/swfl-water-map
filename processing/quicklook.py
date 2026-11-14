"""Plain quick-look images of blended hours, for checking results (not the map style).

    python -m processing.quicklook build/blend/swfl_blend_hourly_2026-11-14.bin.gz --hour 15 --out docs/samples

Needs matplotlib (pip install matplotlib); the hourly workflow does not.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from .config import CLEAN_DIR
from .run import NODATA16, NODATA8, read_day


def render(path: Path, hour: int, out_dir: Path, region: tuple | None = None, tag: str = "") -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from . import gridfile

    head, _ = gridfile.read(path)
    frames = read_day(path)
    t = sorted(frames)[[x.hour for x in sorted(frames)].index(hour)]
    layers, info = frames[t]
    g = head["grid"]
    extent = [g["lon_left_edge"], g["lon_left_edge"] + g["cols"] * g["dlon"],
              g["lat_top_edge"] - g["rows"] * g["dlat"], g["lat_top_edge"]]
    stations = {s["station"]: s for s in _stations()}
    tc = head["layers"]["temp_c"]["scale"]
    scs = head["layers"]["sal_psu"]["scale"]
    temp = np.where(layers["temp_c"] == NODATA16, np.nan, layers["temp_c"] * tc) * 9 / 5 + 32
    sal = np.where(layers["sal_psu"] == NODATA16, np.nan, layers["sal_psu"] * scs)
    tconf = np.where(layers["temp_conf"] == NODATA8, np.nan, layers["temp_conf"])
    sconf = np.where(layers["sal_conf"] == NODATA8, np.nan, layers["sal_conf"])

    fig, axes = plt.subplots(2, 2, figsize=(13, 11.5))
    panels = [
        (axes[0, 0], temp, "Temperature (F)", "turbo", None),
        (axes[0, 1], tconf, "Temperature confidence (0-100)", "Greys_r", (0, 100)),
        (axes[1, 0], sal, "Salinity (PSU)", "viridis", (0, 40)),
        (axes[1, 1], sconf, "Salinity confidence (0-100)", "Greys_r", (0, 100)),
    ]
    for ax, arr, title, cmap, lim in panels:
        ax.set_facecolor("#e8e4d8")
        vmin, vmax = lim if lim else (np.nanpercentile(arr, 1), np.nanpercentile(arr, 99))
        im = ax.imshow(arr, extent=extent, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
        if "Temperature (F)" in title:
            levels = np.arange(np.floor(vmin), np.ceil(vmax) + 1, 1.0)
            lat = np.linspace(extent[3], extent[2], arr.shape[0])
            lon = np.linspace(extent[0], extent[1], arr.shape[1])
            ax.contour(lon, lat, arr, levels=levels, colors="k", linewidths=0.3)
        key = "temp" if "Temperature" in title else "sal"
        pts = info["stations"][key]
        xs = [stations[p[0]]["lon"] for p in pts if p[0] in stations]
        ys = [stations[p[0]]["lat"] for p in pts if p[0] in stations]
        ax.scatter(xs, ys, s=9, facecolors="none", edgecolors="k", linewidths=0.6)
        r = region or (extent[0], extent[1], extent[2], extent[3])
        ax.set_xlim(r[0], r[1])
        ax.set_ylim(r[2], r[3])
        ax.set_title(title, fontsize=10)
        fig.colorbar(im, ax=ax, shrink=0.8)
    fig.suptitle(f"{info['time']}  |  {len(info['stations']['temp'])} temperature and "
                 f"{len(info['stations']['sal'])} salinity stations  |  newest GOES-19 frame "
                 f"{info.get('goes_newest')}, median pixel age {info.get('goes_median_age_h')} h", fontsize=10)
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"blend_{t:%Y-%m-%dT%H}Z{tag}.png"
    fig.savefig(out, dpi=80)
    plt.close(fig)
    return out


def _stations() -> list[dict]:
    import csv
    p = CLEAN_DIR / "stations.csv"
    if not p.exists():
        return []
    with open(p) as f:
        return [dict(r, lat=float(r["lat"]), lon=float(r["lon"])) for r in csv.DictReader(f)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--hour", type=int, required=True)
    ap.add_argument("--out", type=Path, default=Path("docs/samples"))
    ap.add_argument("--region", type=float, nargs=4, metavar=("W", "E", "S", "N"))
    ap.add_argument("--tag", default="")
    a = ap.parse_args(argv)
    print(render(a.file, a.hour, a.out, tuple(a.region) if a.region else None, a.tag))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
