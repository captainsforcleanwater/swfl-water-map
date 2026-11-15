"""Clean step: QC flags and hourly cleaned values, written as a layer separate from the raw files.

Raw files in data/sensors are read, never changed. For each series (station, variable, depth tag,
sensor) the tests below set a flag on every reading (IOOS QARTOD codes: 1 good, 3 suspect, 4 bad):

- provider: the source marked the reading as unusable (USGS equipment, ice, maintenance codes)
- time: timestamp more than an hour in the future
- range: outside the fail limits (bad) or the suspect limits in config/processing.json
- spike: far from the median of readings within +-window_minutes, by more than the larger of a fixed
  threshold and a multiple of the local spread
- flatline: identical readings for too long (stuck sensor). Salinity at the fresh-water floor and
  salinity runs while conductance keeps changing are left alone: that is rounding, not a stuck sensor
- neighbor: temperature that stays far from the median of nearby stations (by water path) for days
- hold: set by hand in config/station_tags.json (qc_hold) while a sensor is under review

Salinity is computed from specific conductance with PSS-78 where a station has conductance but no
salinity at a depth, and also where USGS reports salinity only in whole PSU (the computed value is
finer; the reported one is kept in the raw files but not used). Above 42 PSU, the top of the range
PSS-78 is defined for, the value is extrapolated and the hourly source column says so; hypersaline
readings are kept (the range limits sit above Florida Bay's records).

Hourly values are the mean of good readings within half_window_minutes of the hour. Depth is kept
as a layer: surface (near-surface), bottom, mid, or profile (thermistor strings, not used).
Stations at the same spot (flagged possible_duplicate_of, or within merge.max_distance_m) are
merged into one physical station only when their readings agree (stations.py); otherwise both stay.

Grid inputs (CleanResult.grid): salinity from the surface layer only; temperature from the surface
layer, or, where a station has no surface sensor and sits in shallow water (depths.py), from its
mid or bottom sensor.

Outputs (only good readings make it into the hourly values):
- data/qc/YYYY/MM/YYYY-MM-DD_flags.csv: every raw reading flagged suspect or bad, with the tests failed
- data/clean/YYYY/MM/YYYY-MM-DD.csv: hourly cleaned values per physical station, variable and layer.
  source is blank for the station's own reading, else the feed it came from (for merged
  duplicates) and/or "PSS-78 from conductance"
- data/clean/stations.csv: physical stations, their members, setting tag, depth, grid use and status
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from collector.common import DATA, SAL, SPCOND, TEMP, iso
from collector.tags import overrides_file
from . import depths as depthmod
from .config import CLEAN_DIR, QC_DIR, proc_config
from .pss78 import PSS78_MAX, salinity_from_spec_cond
from .stations import PhysStation, candidate_pairs, load_raw_stations, physical_stations

log = logging.getLogger("processing.clean")

GOOD, SUSPECT, BAD = 1, 3, 4
TESTS = {   # bit: (name, flag it sets)
    1: ("provider", BAD), 2: ("time", BAD), 4: ("range", BAD), 8: ("range_suspect", SUSPECT),
    16: ("spike", SUSPECT), 32: ("flatline", SUSPECT), 64: ("flatline_long", BAD),
    128: ("neighbor", SUSPECT), 256: ("missing", BAD), 512: ("from_flagged_conductance", SUSPECT),
    1024: ("hold", SUSPECT),
}
BAD_BITS = sum(b for b, (_, f) in TESTS.items() if f == BAD)
# USGS qualifier codes that mean the value should not be used.
PROVIDER_BAD = re.compile(r"\b(Eqp|Ice|Mnt|Dis|Ssn|Bkw|Fld|Pr|Rat|Zfl|\*\*\*)\b")
PROFILE_SENSOR = re.compile(r"substrate|sbstrate|sediment|therm\s*\d", re.I)
SURFACE_SENSOR = re.compile(r"surface|\btop\b|upper", re.I)
LAYERS = ("surface", "mid", "bottom", "profile")

CLEAN_COLUMNS = ["time_utc", "station", "variable", "layer", "value", "n", "depth_m", "source"]
FLAG_COLUMNS = ["time_utc", "station_uid", "variable", "depth_tag", "sensor", "depth_m", "value", "flag", "tests"]


@dataclass
class CleanResult:
    readings: pd.DataFrame        # raw readings with flag, tests, series, layer
    hourly: pd.DataFrame          # time, station, variable, layer, value, n, depth_m, source, member
    grid: pd.DataFrame            # time, station, variable, value, layer: what the grids use
    stations: list[PhysStation]
    status: pd.DataFrame          # per physical station
    stats: dict


# ---------------------------------------------------------------- reading raw files

def raw_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def load_raw(days: list[date]) -> pd.DataFrame:
    frames = []
    for d in days:
        folder = DATA / "sensors" / f"{d:%Y}" / f"{d:%m}"
        for p in sorted(folder.glob(f"{d:%Y-%m-%d}_*.csv")):
            df = pd.read_csv(p, dtype=str, keep_default_na=False)
            df["system"] = p.stem.split("_", 1)[1]
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["time_utc", "station_uid", "variable", "value", "depth_m",
                                     "depth_tag", "sensor", "qc", "system", "time"])
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time_utc"], utc=True, format="ISO8601")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["depth_m"] = pd.to_numeric(df["depth_m"], errors="coerce")
    return df.drop_duplicates(["time_utc", "station_uid", "variable", "depth_tag", "sensor", "depth_m"], keep="last")


# ---------------------------------------------------------------- per-series tests

def epoch_s(times: pd.Series) -> np.ndarray:
    """Seconds since 1970 as float, whatever resolution pandas chose for the column."""
    return ((times - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=1)).to_numpy(dtype="f8")


def _layer(depth_tag: str, sensor: str, depth_m: float, cfg: dict) -> str:
    if PROFILE_SENSOR.search(sensor or ""):
        return "profile"
    if depth_tag == "bottom":
        return "bottom"
    if depth_tag == "mid":
        return "surface" if np.isfinite(depth_m) and depth_m <= cfg["near_surface_max_depth_m"] else "mid"
    return "surface"


def _rolling_median(t_s: np.ndarray, v: np.ndarray, half: float) -> tuple[np.ndarray, np.ndarray]:
    """Median and count of v within +-half seconds of each point (point included)."""
    lo = np.searchsorted(t_s, t_s - half, "left")
    hi = np.searchsorted(t_s, t_s + half, "right")
    med = np.empty(len(v))
    for i in range(len(v)):
        med[i] = np.median(v[lo[i]:hi[i]])
    return med, hi - lo


def spike_bits(t_s: np.ndarray, v: np.ndarray, var: str, cfg: dict) -> np.ndarray:
    sc = cfg["spike"]
    thr = sc.get(var)
    out = np.zeros(len(v), np.int64)
    ok = np.isfinite(v)
    if thr is None or ok.sum() < 3:
        return out
    tt, vv = t_s[ok], v[ok]
    half = sc["window_minutes"] * 60
    med, cnt = _rolling_median(tt, vv, half)
    dev = np.abs(vv - med)
    spread, _ = _rolling_median(tt, dev, half)
    limit = np.maximum(thr, sc["mad_factor"] * 1.4826 * spread)
    hit = (dev > limit) & (cnt >= 3)
    out[np.nonzero(ok)[0][hit]] = 16
    return out


def flatline_bits(t_s: np.ndarray, v: np.ndarray, var: str, cfg: dict,
                  companion: tuple[np.ndarray, np.ndarray] | None, layer: str = "surface") -> np.ndarray:
    fc = cfg["flatline"].get(var)
    out = np.zeros(len(v), np.int64)
    if fc is None or len(v) < 3 or layer == "profile":
        return out
    # Water below the surface (and the sediment) changes slowly, so allow longer runs there.
    scale = 1.0 if layer == "surface" else cfg["flatline"].get("below_surface_factor", 2.0)
    fc = dict(fc, suspect_hours=fc["suspect_hours"] * scale, fail_hours=fc["fail_hours"] * scale)
    same = np.r_[False, np.abs(np.diff(v)) < 1e-9]
    i = 0
    n = len(v)
    while i < n:
        if i + 1 < n and same[i + 1]:
            j = i + 1
            while j + 1 < n and same[j + 1]:
                j += 1
            hours = (t_s[j] - t_s[i]) / 3600
            if hours >= fc["suspect_hours"]:
                skip = False
                if var == SAL:
                    if v[i] < fc.get("fresh_floor_psu", 0):
                        skip = True
                    elif companion is not None:
                        ct, cv = companion
                        m = (ct >= t_s[i]) & (ct <= t_s[j]) & np.isfinite(cv)
                        if m.sum() >= 3 and np.ptp(cv[m]) > max(10.0, 0.005 * np.nanmean(cv[m])):
                            skip = True
                if not skip:
                    out[i:j + 1] |= 64 if hours >= fc["fail_hours"] else 32
            i = j + 1
        else:
            i += 1
    return out


def flags_from_bits(bits: np.ndarray) -> np.ndarray:
    return np.where(bits & BAD_BITS, BAD, np.where(bits > 0, SUSPECT, GOOD))


def test_names(bits: int) -> str:
    return ";".join(name for b, (name, _) in TESTS.items() if bits & b)


def run_series_tests(df: pd.DataFrame, now: datetime, cfg: dict) -> pd.DataFrame:
    """Add series id, layer and test bits (without the neighbor test) to raw readings."""
    df = df.sort_values(["station_uid", "variable", "depth_tag", "sensor", "time"]).reset_index(drop=True)
    df["series"] = df.groupby(["station_uid", "variable", "depth_tag", "sensor"], sort=False).ngroup()
    bits = np.zeros(len(df), np.int64)
    t_all = epoch_s(df["time"])
    v_all = df["value"].to_numpy(dtype="f8")
    depth_all = df["depth_m"].to_numpy(dtype="f8")
    bits[~np.isfinite(v_all)] |= 256
    bits[df["qc"].map(lambda q: bool(PROVIDER_BAD.search(q or ""))).to_numpy()] |= 1
    bits[t_all > now.timestamp() + 3600] |= 2
    for var, lim in cfg["gross_range"].items():
        m = (df["variable"] == var).to_numpy()
        lo, hi = lim["fail"]
        bits[m & ((v_all < lo) | (v_all > hi))] |= 4
        lo, hi = lim["suspect"]
        bits[m & ((v_all < lo) | (v_all > hi))] |= 8
    # Companion conductance per (station, depth_tag) for the salinity flatline check.
    comp = {}
    for (uid, tag), g in df[df["variable"] == SPCOND].groupby(["station_uid", "depth_tag"]):
        comp[(uid, tag)] = (epoch_s(g["time"]), g["value"].to_numpy("f8"))
    layers = np.empty(len(df), object)
    for sid, idx in df.groupby("series").indices.items():
        row = df.iloc[idx[0]]
        var = row["variable"]
        t, v = t_all[idx], v_all[idx]
        dm = depth_all[idx]
        layer = _layer(row["depth_tag"], row["sensor"], float(np.nanmedian(dm)) if np.isfinite(dm).any() else np.nan, cfg)
        layers[idx] = layer
        usable = (bits[idx] & (BAD_BITS | 8)) == 0
        b = np.zeros(len(idx), np.int64)
        sub = np.nonzero(usable)[0]
        if len(sub):
            b[sub] |= spike_bits(t[sub], v[sub], var, cfg)
            b[sub] |= flatline_bits(t[sub], v[sub], var, cfg, comp.get((row["station_uid"], row["depth_tag"])), layer)
        bits[idx] |= b
    df["bits"] = bits
    df["layer"] = layers
    return df


def whole_psu_series(df: pd.DataFrame) -> set[tuple[str, str]]:
    """(station, layer) where USGS reports salinity only in whole PSU."""
    out = set()
    sal = df[(df["variable"] == SAL) & df["station_uid"].str.startswith("usgs:")]
    for (uid, layer), g in sal.groupby(["station_uid", "layer"]):
        v = g["value"].to_numpy(dtype="f8")
        v = v[np.isfinite(v)]
        if len(v) >= 12 and np.mean(np.abs(v - np.rint(v)) < 1e-6) >= 0.98 and v.max() >= 1:
            out.add((uid, layer))
    return out


def add_derived_salinity(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, set]:
    """Salinity from specific conductance (PSS-78) where a station has conductance but no salinity
    in a layer, or where USGS salinity there is whole PSU only. Returns the readings (derived rows
    appended, superseded rows marked) and the (station, layer) pairs whose reported salinity was
    replaced."""
    df = df.copy()
    df["derived"] = ""
    df["superseded"] = False
    sal = df[df["variable"] == SAL]
    has_sal = set(zip(sal["station_uid"], sal["layer"]))
    sc = df[df["variable"] == SPCOND]
    sc_keys = set(zip(sc["station_uid"], sc["layer"]))
    whole = whole_psu_series(df) & sc_keys if cfg.get("usgs_whole_psu_from_conductance", True) else set()
    if whole:
        keys = list(zip(df["station_uid"], df["layer"]))
        df.loc[(df["variable"] == SAL).to_numpy() & np.array([k in whole for k in keys]), "superseded"] = True
    derive = {k for k in sc_keys if k not in has_sal or k in whole}
    sc = sc[[k in derive for k in zip(sc["station_uid"], sc["layer"])]]
    if sc.empty:
        return df, whole
    d = sc.copy()
    d["variable"] = SAL
    d["value"] = salinity_from_spec_cond(sc["value"].to_numpy())
    d["sensor"] = (sc["sensor"].where(sc["sensor"] != "", "conductance")) + " (PSS-78 from conductance)"
    d["derived"] = "pss78"
    # Inherit conductance flags: bad stays bad, suspect becomes "from flagged conductance".
    b = d["bits"].to_numpy()
    d["bits"] = np.where(b & BAD_BITS, b, np.where(b > 0, 512, 0))
    lo, hi = cfg["gross_range"][SAL]["fail"]
    v = d["value"].to_numpy()
    d.loc[(v < lo) | (v > hi), "bits"] |= 4
    d["series"] = df["series"].max() + 1 + d.groupby(["station_uid", "depth_tag", "sensor"]).ngroup()
    return pd.concat([df, d], ignore_index=True), whole


def hold_bits(df: pd.DataFrame) -> tuple[np.ndarray, dict]:
    """Bit 1024 on readings of stations held by hand in config/station_tags.json (qc_hold)."""
    holds = overrides_file().get("qc_hold", {})
    bits = np.zeros(len(df), np.int64)
    for uid, h in holds.items():
        m = (df["station_uid"] == uid).to_numpy().copy()
        if h.get("variables"):
            m &= df["variable"].isin(h["variables"]).to_numpy()
        bits[m] = 1024
    return bits, holds


# ---------------------------------------------------------------- hourly values

SENSOR_RANK = lambda s: 0 if SURFACE_SENSOR.search(s or "") else (1 if not s else 2)  # noqa: E731


def hourly_values(df: pd.DataFrame, stations: list[PhysStation], cfg_hourly: dict) -> pd.DataFrame:
    """One value per station (df["phys"]), variable, layer and hour, from good readings only.
    stations gives the member order of merged stations; unmerged stations have one member."""
    good = df[(flags_from_bits(df["bits"].to_numpy()) == GOOD) & (df["variable"] != SPCOND)
              & (df["layer"] != "profile") & ~df["superseded"]].copy()
    if good.empty:
        return pd.DataFrame(columns=["time", "station", "variable", "layer", "value", "n", "depth_m", "source", "member"])
    half = pd.Timedelta(minutes=cfg_hourly["half_window_minutes"])
    good["hour"] = (good["time"] + half).dt.floor("h")
    # Readings exactly half a window after the hour belong to the next hour's window only.
    agg = good.groupby(["phys", "station_uid", "variable", "layer", "sensor", "derived", "hour"]).agg(
        value=("value", "mean"), n=("value", "size"), depth_m=("depth_m", "mean")).reset_index()
    rank = {}
    for ps in stations:
        for i, m in enumerate(ps.members):
            rank[m] = i
    agg["member_rank"] = agg["station_uid"].map(rank).fillna(99)
    agg["sensor_rank"] = agg["sensor"].map(SENSOR_RANK)
    agg["derived_rank"] = (agg["derived"] != "").astype(int)
    agg = agg.sort_values(["phys", "variable", "layer", "hour", "derived_rank", "member_rank", "sensor_rank", "n"],
                          ascending=[True, True, True, True, True, True, True, False])
    best = agg.drop_duplicates(["phys", "variable", "layer", "hour"], keep="first").copy()
    # source: blank when the value is the station's own reading; otherwise which feed it came from
    # (a duplicate service) and whether it was computed from conductance.
    other = best["station_uid"] != best["phys"]
    best["source"] = np.where(other, best["station_uid"], "")
    best["source"] = np.where(best["derived"] == "pss78",
                              (best["source"] + " PSS-78 from conductance").str.strip(), best["source"])
    # PSS-78 is only defined to 42 PSU; above that the value is extrapolated (kept, and said so).
    ext = (best["derived"] == "pss78") & (best["value"] > PSS78_MAX)
    best["source"] = np.where(ext, best["source"] + f", extrapolated above {PSS78_MAX:g} PSU", best["source"])
    best["member"] = best["station_uid"]
    out = best.rename(columns={"phys": "station", "hour": "time"})[
        ["time", "station", "variable", "layer", "value", "n", "depth_m", "source", "member"]]
    return out.reset_index(drop=True)


def grid_values(hourly: pd.DataFrame, shallow: dict[str, bool | None]) -> pd.DataFrame:
    """The value each station gives the grids each hour: salinity from the surface layer; temperature
    from the surface layer, else from a mid or bottom sensor in shallow water (shallow[member])."""
    if hourly.empty:
        return pd.DataFrame(columns=["time", "station", "variable", "value", "layer", "member"])
    h = hourly
    sal = h[(h["variable"] == SAL) & (h["layer"] == "surface")]
    t = h[h["variable"] == TEMP]
    surf = t[t["layer"] == "surface"]
    below = t[t["layer"].isin(["mid", "bottom"]) & t["member"].map(lambda m: shallow.get(m) is True)]
    below = below.assign(rank=below["layer"].map({"mid": 0, "bottom": 1})).sort_values("rank")
    below = below.drop_duplicates(["station", "time"]).drop(columns="rank")
    have = set(zip(surf["station"], surf["time"]))
    below = below[[k not in have for k in zip(below["station"], below["time"])]]
    out = pd.concat([sal, surf, below], ignore_index=True)
    return out[["time", "station", "variable", "value", "layer", "member"]].sort_values(
        ["station", "variable", "time"]).reset_index(drop=True)


def agreement(grid: pd.DataFrame, pairs: list[tuple[str, str]], mcfg: dict) -> dict[tuple[str, str], dict]:
    """For each candidate pair, the median absolute difference of their grid values over shared
    hours, per variable, and whether they agree. agree is None when they share no hours."""
    out = {}
    piv = {var: grid[grid["variable"] == var].pivot_table(index="time", columns="station", values="value")
           for var in (TEMP, SAL)}
    for a, b in pairs:
        info = {"agree": None}
        verdicts = []
        for var, key in ((TEMP, "temp"), (SAL, "salinity")):
            p = piv[var]
            if a not in p or b not in p:
                continue
            both = p[[a, b]].dropna()
            if len(both) < mcfg["min_overlap_hours"]:
                continue
            md = float(np.median(np.abs(both[a] - both[b])))
            info[key] = {"median_abs_diff": round(md, 2), "hours": int(len(both))}
            verdicts.append(md <= mcfg[var])
        if verdicts:
            info["agree"] = all(verdicts)
        out[(a, b)] = info
    return out


# ---------------------------------------------------------------- neighbor test

def neighbor_test(df: pd.DataFrame, grid: pd.DataFrame, dist: dict[tuple[str, str], float], cfg: dict
                  ) -> tuple[np.ndarray, dict]:
    """Bits (128) for raw readings in hours where a station's grid temperature stays far from its
    neighbours. dist maps (station id, station id) to water-path km for coastal gridded stations."""
    nc = cfg["neighbor"]
    var = TEMP
    thr = nc.get(var)
    h = grid[grid["variable"] == var]
    ids = sorted({a for a, _ in dist})
    if thr is None or h.empty or len(ids) < nc["min_neighbors"] + 1:
        return np.zeros(len(df), np.int64), {}
    X = h.pivot_table(index="time", columns="station", values="value").reindex(columns=ids)
    X = X.sort_index()
    full = pd.date_range(X.index.min(), X.index.max(), freq="h")
    X = X.reindex(full)
    vals = X.to_numpy()
    nbrs = {i: [j for j in range(len(ids)) if j != i and dist.get((ids[i], ids[j]), np.inf) <= nc["max_km"]]
            for i in range(len(ids))}
    flagged = np.zeros(vals.shape, bool)
    summary = {}
    for _ in range(2):   # second pass leaves out what the first pass flagged
        v = np.where(flagged, np.nan, vals)
        resid = np.full(vals.shape, np.nan)
        for i, nb in nbrs.items():
            if len(nb) < nc["min_neighbors"]:
                continue
            sub = v[:, nb]
            cnt = np.isfinite(sub).sum(1)
            med = np.full(len(sub), np.nan)
            okr = cnt > 0
            med[okr] = np.nanmedian(sub[okr], axis=1)
            r = vals[:, i] - med
            r[cnt < nc["min_neighbors"]] = np.nan
            resid[:, i] = r
        R = pd.DataFrame(resid, index=X.index)
        roll = R.rolling(nc["window_hours"], center=True, min_periods=nc["min_hours"]).median().to_numpy()
        flagged = np.isfinite(vals) & (np.abs(roll) > thr)
    for i, sid in enumerate(ids):
        if flagged[:, i].any():
            summary[sid] = {"hours": int(flagged[:, i].sum()),
                            "median_offset_c": round(float(np.nanmedian(resid[flagged[:, i], i])), 2)}
    # Map flagged station-hours back to raw readings of that station's temperature series.
    bits = np.zeros(len(df), np.int64)
    half = pd.Timedelta(minutes=proc_config()["hourly"]["half_window_minutes"])
    hours = (df["time"] + half).dt.floor("h")
    fl = {(ids[i], X.index[t_idx]) for t_idx, i in zip(*np.nonzero(flagged))}
    if fl:
        layer_of = dict(zip(zip(h["station"], h["time"]), h["layer"]))
        m = (df["variable"] == var).to_numpy()
        idx = np.nonzero(m)[0]
        keys = zip(df["phys"].to_numpy()[idx], hours.to_numpy()[idx], df["layer"].to_numpy()[idx])
        hit = np.array([layer_of.get((p, pd.Timestamp(t))) == lay if (p, pd.Timestamp(t)) in fl else False
                        for p, t, lay in keys], bool)
        bits[idx[hit]] = 128
    return bits, summary


# ---------------------------------------------------------------- station status

def station_status(df: pd.DataFrame, stations: list[PhysStation], now: datetime, cfg: dict,
                   grid_use: dict[str, dict], depth: dict[str, dict], not_merged: dict[str, list[str]],
                   grid: pd.DataFrame) -> pd.DataFrame:
    sc = cfg["stale"]
    raw = {s["uid"]: s for s in load_raw_stations()}
    last = df[df["variable"] != SPCOND].groupby("phys")["time"].max()
    rows = []
    for ps in stations:
        t = last.get(ps.id)
        delay = sc["provider_delay_hours"].get(ps.provider, 0)
        cad = max([c for c in ps.cadence_min.values() if c] or [60]) / 60
        limit = sc["default_hours"] + delay + 2 * cad
        if t is None or pd.isna(t):
            status = "no data in window"
            age = ""
        else:
            age_h = (now - t.to_pydatetime()).total_seconds() / 3600
            status = "stale" if age_h > limit else "current"
            age = round(age_h, 1)
        sub = df[df["phys"] == ps.id]
        fl = flags_from_bits(sub["bits"].to_numpy())
        layers = {}
        for var in (TEMP, SAL):
            s = sub[(sub["variable"] == var) & (fl == GOOD)]
            layers[var] = ",".join(sorted(set(s["layer"]), key=LAYERS.index))
        gu = grid_use.get(ps.id, {})
        dp = depth.get(ps.id) or {}
        gt = grid[(grid["station"] == ps.id) & (grid["variable"] == TEMP)]
        rows.append({
            "station": ps.id, "name": ps.name, "provider": ps.provider,
            "lat": round(ps.lat, 5), "lon": round(ps.lon, 5),
            "members": ";".join(ps.members), "not_merged_with": ";".join(not_merged.get(ps.id, [])),
            "setting": ps.setting, "setting_reason": ps.setting_reason,
            "depth_m": "" if not dp else ("creek" if dp.get("depth_m") is None else dp["depth_m"]),
            "temp_layers": layers[TEMP], "salinity_layers": layers[SAL],
            "grid_temp_layer": ",".join(sorted(set(gt["layer"]), key=LAYERS.index)),
            "salinity_from_conductance": "yes" if (sub["derived"] == "pss78").any() else "no",
            "grid_temp": gu.get("temp", ""), "grid_salinity": gu.get("salinity", ""),
            "snap_km": gu.get("snap_km", ""),
            "last_reading_utc": iso(t.to_pydatetime()) if t is not None and not pd.isna(t) else "",
            "age_hours": age, "status": status,
            "readings": int(len(sub)), "suspect": int((fl == SUSPECT).sum()), "bad": int((fl == BAD).sum()),
            "url": raw.get(ps.id, {}).get("url", ""),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- writing

def _write_if_changed(path, text: str) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return True


def _fmt(x, nd=3) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return ""
    return f"{x:.{nd}f}".rstrip("0").rstrip(".")


def write_outputs(res: CleanResult, days: list[date]) -> dict:
    changed = {}
    rd = res.readings
    fl = flags_from_bits(rd["bits"].to_numpy())
    rd = rd.assign(flag=fl, day=rd["time"].dt.strftime("%Y-%m-%d"))
    hourly = res.hourly.assign(day=res.hourly["time"].dt.strftime("%Y-%m-%d"))
    for d in days:
        ds = d.isoformat()
        # flags
        sub = rd[(rd["day"] == ds) & (rd["flag"] != GOOD) & (rd["derived"] == "")]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(FLAG_COLUMNS)
        for r in sub.sort_values(["station_uid", "variable", "depth_tag", "sensor", "time"]).itertuples():
            w.writerow([r.time_utc, r.station_uid, r.variable, r.depth_tag, r.sensor, _fmt(r.depth_m, 2),
                        _fmt(r.value), r.flag, test_names(int(r.bits))])
        p = QC_DIR / f"{d:%Y}" / f"{d:%m}" / f"{ds}_flags.csv"
        if _write_if_changed(p, buf.getvalue()):
            changed[str(p.relative_to(DATA.parent))] = len(sub)
        # hourly clean values
        sub = hourly[hourly["day"] == ds].sort_values(["station", "variable", "layer", "time"])
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(CLEAN_COLUMNS)
        for r in sub.itertuples():
            w.writerow([iso(pd.Timestamp(r.time).to_pydatetime()), r.station, r.variable, r.layer,
                        _fmt(r.value, 3 if r.variable == SAL else 2), r.n, _fmt(r.depth_m, 2), r.source])
        p = CLEAN_DIR / f"{d:%Y}" / f"{d:%m}" / f"{ds}.csv"
        if not sub.empty and _write_if_changed(p, buf.getvalue()):
            changed[str(p.relative_to(DATA.parent))] = len(sub)
    p = CLEAN_DIR / "stations.csv"
    if _write_if_changed(p, res.status.to_csv(index=False, lineterminator="\n")):
        changed[str(p.relative_to(DATA.parent))] = len(res.status)
    return changed


# ---------------------------------------------------------------- entry point

def run(start: datetime, end: datetime, now: datetime, graph=None, write_days: list[date] | None = None,
        fetch_depths: bool = True) -> CleanResult:
    """QC readings between start and end (plus context), compute hourly values and grid inputs.

    graph: a WaterGraph, used for the neighbor test and to record which stations the grids can use.
    """
    pcfg = proc_config()
    cfg = pcfg["qc"]
    ctx = cfg["context_days"]
    days = raw_days((start - timedelta(days=ctx)).date(), end.date())
    df = load_raw(days)
    raw_st = [s for s in load_raw_stations() if s.get("collect")]
    df = df[df["station_uid"].isin({s["uid"] for s in raw_st})]
    stats: dict = {"raw_readings": int(len(df))}
    if df.empty:
        stations, _ = physical_stations(raw_st, set())
        empty = pd.DataFrame(columns=CLEAN_COLUMNS)
        return CleanResult(df, empty, empty, stations, pd.DataFrame(), stats)
    df = run_series_tests(df, now, cfg)
    df, whole = add_derived_salinity(df, cfg)
    hb, holds = hold_bits(df)
    df["bits"] = df["bits"].to_numpy() | hb
    stats["salinity_from_conductance_replacing_whole_psu"] = sorted(f"{u} ({lay})" for u, lay in whole)
    stats["holds"] = {u: h.get("reason", "") for u, h in holds.items()}

    # Depth at each station, for bottom gauges standing in for surface temperature.
    bt = cfg["bottom_temperature"]
    depth = depthmod.station_depths(raw_st, fetch=fetch_depths)
    shallow = {s["uid"]: (depthmod.is_shallow(depth.get(s["uid"]), bt["max_depth_m"]) if bt["use_in_shallow_water"]
                          else False) for s in raw_st}

    # Each station on its own first; merge co-located stations only where their readings agree.
    df["phys"] = df["station_uid"]
    solo = grid_values(hourly_values(df, [], pcfg["hourly"]), shallow)
    pairs = candidate_pairs(raw_st, cfg["merge"]["max_distance_m"])
    checks = agreement(solo, pairs, cfg["merge"])
    agree = {frozenset(p) for p, i in checks.items() if i["agree"]}
    stations, member_of = physical_stations(raw_st, agree)
    df["phys"] = df["station_uid"].map(member_of)
    not_merged: dict[str, list[str]] = {}
    for (a, b), i in checks.items():
        if frozenset((a, b)) in agree:
            continue
        why = "; ".join(f"{k} differs by {v['median_abs_diff']}" for k, v in i.items() if isinstance(v, dict)) \
            or "no shared hours to compare"
        not_merged.setdefault(member_of[a], []).append(f"{b} ({why})")
        not_merged.setdefault(member_of[b], []).append(f"{a} ({why})")
    stats["merge_checks"] = {f"{a} + {b}": i for (a, b), i in checks.items()}
    depth_phys = {ps.id: depth.get(ps.id) for ps in stations}

    # Grid use and water-path distances between coastal stations (for the neighbor test).
    grid_use, dist = {}, {}
    if graph is not None:
        snap_km = pcfg["grid"]["station_snap_km"]
        nodes = {}
        for ps in stations:
            gu = {}
            if ps.setting != "coastal":
                gu["temp"] = gu["salinity"] = f"no: {ps.setting}"
            else:
                sn = graph.snap(ps.lat, ps.lon, snap_km)
                if sn is None:
                    gu["temp"] = gu["salinity"] = f"no: not within {snap_km:g} km of mapped tidal water"
                else:
                    nodes[ps.id] = sn[0]
                    gu["snap_km"] = round(sn[1], 2)
            grid_use[ps.id] = gu
        if nodes:
            ids = sorted(nodes)
            D = graph.distances([nodes[i] for i in ids])
            for a_i, a in enumerate(ids):
                for b in ids:
                    dist[(a, b)] = float(D[a_i, nodes[b]])
        stats["station_nodes"] = nodes

    hourly = hourly_values(df, stations, pcfg["hourly"])
    grid = grid_values(hourly, shallow)
    nb_bits, nb_summary = neighbor_test(df, grid, dist, cfg)
    if nb_bits.any():
        df["bits"] = df["bits"].to_numpy() | nb_bits
        hourly = hourly_values(df, stations, pcfg["hourly"])
        grid = grid_values(hourly, shallow)
    stats["neighbor_flags"] = nb_summary
    if graph is not None:
        grid = grid[grid["station"].isin(stats["station_nodes"])].reset_index(drop=True)

    # Why each station is or is not in each grid.
    for ps in stations:
        gu = grid_use.setdefault(ps.id, {})
        for var, key in ((TEMP, "temp"), (SAL, "salinity")):
            if key in gu:
                continue
            g = grid[(grid["station"] == ps.id) & (grid["variable"] == var)]
            h = hourly[(hourly["station"] == ps.id) & (hourly["variable"] == var)]
            if ps.setting != "coastal":
                gu[key] = f"no: {ps.setting}"
            elif len(g):
                below = sorted(set(g["layer"]) - {"surface"})
                gu[key] = "yes" if not below else f"yes: {'/'.join(below)} sensor in shallow water"
            elif len(h):
                if var == TEMP and bt["use_in_shallow_water"]:
                    sh = shallow.get(ps.id)
                    gu[key] = ("no: no near-surface sensor, depth unknown" if sh is None else
                               f"no: no near-surface sensor, water deeper than {bt['max_depth_m']:g} m")
                else:
                    gu[key] = "no: no near-surface sensor"
            else:
                gu[key] = "no: no good readings in window"
    status = station_status(df, stations, now, cfg, grid_use, depth_phys, not_merged, grid)
    fl = flags_from_bits(df["bits"].to_numpy())
    stats.update({
        "good": int((fl == GOOD).sum()), "suspect": int((fl == SUSPECT).sum()), "bad": int((fl == BAD).sum()),
        "by_test": {name: int(((df["bits"].to_numpy() & b) > 0).sum()) for b, (name, _) in TESTS.items()},
        "hourly_values": int(len(hourly)),
        "salinity_extrapolated_above_42": int(hourly["source"].str.contains("extrapolated", na=False).sum()),
        "stale_stations": status.loc[status["status"] == "stale", "station"].tolist(),
    })
    res = CleanResult(df, hourly, grid, stations, status, stats)
    if write_days:
        stats["files_changed"] = write_outputs(res, write_days)
    return res
