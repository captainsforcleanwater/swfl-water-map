"""Northeast Florida Bay warm-offset check.

    python analysis/florida_bay_offsets.py DATA_DIR OUT_DIR

Asks whether Highway Creek, Long Sound and Trout Cove read warm because of their sensors (an offset
that stays the same in all weather, day and night) or because shallow water really heats up (an
offset that grows on calm sunny afternoons and fades at night and on windy or cloudy days).

DATA_DIR holds the files pulled from:
- SECOORA ERDDAP gov-nps-ever-<id> (Everglades National Park, hourly-ish, with QARTOD flags)
- USGS 251253080320100 Trout Creek at mouth (bottom temperature, 15 min, NWIS instantaneous values)
- USDA NRCS SCAN 2051 Everglades ARS (hourly solar radiation, wind) on SECOORA ERDDAP
- NOAA CO-OPS 8723970 Vaca Key (hourly wind)

Each station is compared with the median of a fixed set of open-bay reference stations in
eastern Florida Bay, hour by hour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

TARGETS = {
    "hcef1": "Highway Creek (HCEF1)",
    "lsnf1": "Long Sound (LSNF1)",
    "tcvf1": "Trout Cove (TCVF1)",
    "jbyf1": "Joe Bay (JBYF1)",
    "usgs_trout": "Trout Creek, USGS (bottom)",
}
REFERENCE = ["mnbf1", "lbsf1", "bwsf1", "dkkf1", "mdkf1", "thrf1", "bnkf1"]
LOCAL = "America/New_York"


def load_enp(path: Path) -> pd.Series:
    df = pd.read_csv(path, skiprows=[1])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    ok = np.isfinite(df["sea_water_temperature"]) & (df["sea_water_temperature_qc_agg"].isin([1, 2]))
    return df.loc[ok].set_index("time")["sea_water_temperature"].resample("1h").mean()


def load_usgs(path: Path) -> pd.Series:
    df = pd.read_csv(path, sep="\t", comment="#", dtype=str).iloc[1:]
    col = [c for c in df.columns if c.endswith("_00010")][0]
    off = df["tz_cd"].map({"EDT": "-04:00", "EST": "-05:00"})
    t = pd.to_datetime(df["datetime"] + off, utc=True, format="%Y-%m-%d %H:%M%z")
    v = pd.to_numeric(df[col], errors="coerce")
    return pd.Series(v.values, index=t).dropna().resample("1h").mean()


def load_weather(d: Path) -> pd.DataFrame:
    s = pd.read_csv(d / "scan_2051.csv", skiprows=[1])
    s["time"] = pd.to_datetime(s["time"], utc=True)
    s = s.set_index("time").rename(columns={"solar_irradiance_cm_time__mean_over_pt1h": "solar_wm2",
                                            "wind_speed": "land_wind_ms", "air_temperature": "air_c",
                                            "lwe_precipitation_rate_cm_time__sum_over_pt1h": "rain_mm"})
    s = s[["solar_wm2", "land_wind_ms", "air_c", "rain_mm"]].apply(pd.to_numeric, errors="coerce").resample("1h").mean()
    w = pd.read_csv(d / "coops_vaca_wind.csv", skipinitialspace=True)
    w = w[w["Date Time"] != "Date Time"]
    w["time"] = pd.to_datetime(w["Date Time"], utc=True)
    w = w.set_index("time")[["Speed"]].apply(pd.to_numeric, errors="coerce").rename(columns={"Speed": "bay_wind_ms"})
    return s.join(w.resample("1h").mean(), how="outer")


def main(data_dir: str, out_dir: str) -> None:
    d, out = Path(data_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    series = {sid: load_enp(d / f"enp_{sid}.csv") for sid in set(REFERENCE) | (set(TARGETS) - {"usgs_trout"})}
    series["usgs_trout"] = load_usgs(d / "usgs_trout_creek_rdb.txt")
    T = pd.DataFrame(series).sort_index()
    ref = T[REFERENCE]
    ref_med = ref.median(axis=1).where(ref.notna().sum(axis=1) >= 4)
    W = load_weather(d)
    local = T.index.tz_convert(LOCAL)

    rows, daily_all, diurnal = [], {}, {}
    for sid, name in TARGETS.items():
        off = (T[sid] - ref_med).rename("offset")
        df = pd.DataFrame({"offset": off, "hour": local.hour, "date": local.date}).dropna()
        # day: 13-17 local (peak heating); night: 03-07 local (coolest, no sun)
        day = df[df["hour"].between(13, 16)].groupby("date")["offset"].mean()
        night = df[df["hour"].between(3, 6)].groupby("date")["offset"].mean()
        daily = pd.DataFrame({"mean": df.groupby("date")["offset"].mean(), "day": day, "night": night,
                              "n": df.groupby("date")["offset"].size()})
        daily = daily[daily["n"] >= 18]
        wl = W.copy()
        wl["date"] = W.index.tz_convert(LOCAL).date
        wd = wl.groupby("date").agg(solar=("solar_wm2", "mean"), bay_wind=("bay_wind_ms", "mean"),
                                    rain=("rain_mm", "sum"), air=("air_c", "mean"))
        daily = daily.join(wd, how="left")
        daily_all[sid] = daily
        diurnal[sid] = df.groupby("hour")["offset"].median()
        x = daily.dropna(subset=["solar", "bay_wind", "day", "night"])
        # Calm sunny days: top third of sunshine and bottom third of wind. Cloudy or windy: the rest's extremes.
        if len(x) > 30:
            sunny_calm = x[(x["solar"] >= x["solar"].quantile(2 / 3)) & (x["bay_wind"] <= x["bay_wind"].quantile(1 / 3))]
            dull_windy = x[(x["solar"] <= x["solar"].quantile(1 / 3)) | (x["bay_wind"] >= x["bay_wind"].quantile(2 / 3))]
            A = np.column_stack([np.ones(len(x)), (x["solar"] - x["solar"].mean()) / 100, x["bay_wind"] - x["bay_wind"].mean()])
            coef, *_ = np.linalg.lstsq(A, x["day"].to_numpy(), rcond=None)
            pred = A @ coef
            r2 = 1 - np.sum((x["day"] - pred) ** 2) / np.sum((x["day"] - x["day"].mean()) ** 2)
        else:
            sunny_calm = dull_windy = x.iloc[:0]
            coef, r2 = [np.nan] * 3, np.nan
        monthly = daily["mean"].groupby(pd.to_datetime(daily.index).to_period("M")).median()
        rows.append({
            "station": name, "days": len(daily),
            "mean_offset": daily["mean"].median(), "night_offset": daily["night"].median(),
            "afternoon_offset": daily["day"].median(),
            "sunny_calm_afternoon": sunny_calm["day"].median() if len(sunny_calm) else np.nan,
            "dull_windy_afternoon": dull_windy["day"].median() if len(dull_windy) else np.nan,
            "sunny_calm_night": sunny_calm["night"].median() if len(sunny_calm) else np.nan,
            "per_100wm2_sun": coef[1], "per_ms_wind": coef[2], "r2": r2,
            "monthly_min": monthly.min(), "monthly_max": monthly.max(),
            "first_month": str(monthly.index.min()), "last_month": str(monthly.index.max()),
        })
    summary = pd.DataFrame(rows)
    summary.round(2).to_csv(out / "summary.csv", index=False)
    monthly = pd.DataFrame({TARGETS[s]: daily_all[s]["mean"].groupby(pd.to_datetime(daily_all[s].index).to_period("M")).median()
                            for s in TARGETS})
    monthly.round(2).to_csv(out / "monthly_offset.csv")
    pd.DataFrame({TARGETS[s]: diurnal[s] for s in TARGETS}).round(2).to_csv(out / "diurnal_offset.csv")
    print(summary.round(2).to_string())
    print(monthly.round(2).to_string())
    print(pd.DataFrame({TARGETS[s]: diurnal[s] for s in TARGETS}).round(2).to_string())
    try:
        plot(daily_all, diurnal, out)
    except ImportError:
        pass


def plot(daily_all, diurnal, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(len(TARGETS), 3, figsize=(15, 3.1 * len(TARGETS)),
                             gridspec_kw={"width_ratios": [3, 1.2, 1.2]})
    for row, (sid, name) in zip(axes, TARGETS.items()):
        d = daily_all[sid]
        t = pd.to_datetime(d.index)
        row[0].plot(t, d["day"], ".", ms=2, color="tab:red", label="afternoon (13-17 local)")
        row[0].plot(t, d["night"], ".", ms=2, color="tab:blue", label="night (03-07 local)")
        row[0].axhline(0, color="k", lw=0.5)
        row[0].set_ylim(-4, 6)
        row[0].set_title(f"{name}: offset from open-bay median (C), daily", fontsize=9)
        row[0].legend(fontsize=7, loc="upper left")
        x = d.dropna(subset=["solar", "day"])
        sc = row[1].scatter(x["solar"], x["day"], c=x["bay_wind"], s=4, cmap="viridis_r", vmin=0, vmax=10)
        row[1].set_xlabel("daily mean sunshine (W/m2)", fontsize=8)
        row[1].set_title("afternoon offset vs sun (colour = wind m/s)", fontsize=8)
        row[1].set_ylim(-4, 6)
        fig.colorbar(sc, ax=row[1])
        row[2].plot(diurnal[sid].index, diurnal[sid].values, "-o", ms=3)
        row[2].axhline(0, color="k", lw=0.5)
        row[2].set_xlabel("hour (local)", fontsize=8)
        row[2].set_title("median offset by hour", fontsize=8)
        row[2].set_ylim(-2, 5)
    fig.tight_layout()
    fig.savefig(out / "ne_florida_bay_offsets.png", dpi=75)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
