"""Check that the cleaning keeps hypersaline Florida Bay readings: summer 2015 (seagrass die-off).

    python analysis/hypersaline_2015.py DATA_DIR [ENP_DIR]

Pulls 15-minute USGS salinity, specific conductance and temperature for Florida Bay gauges for
May to September 2015 into DATA_DIR (once), optionally adds Everglades National Park files from
ENP_DIR (SECOORA ERDDAP CSVs named enp_<id>.csv), and runs the clean step's per-reading tests and
PSS-78 conversion with the limits in config/processing.json. Reports how many readings above 42,
55 and 60 PSU come through as good, and what flagged the rest.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector.common import SAL, SPCOND, TEMP  # noqa: E402
from processing import clean  # noqa: E402
from processing.config import proc_config  # noqa: E402

USGS_SITES = ["251003080435500", "251355080312800", "251209080350100", "251253080320100", "251433080265000",
              "251127080382100", "251241080385300", "250802081035500", "251032080432200"]
PARAMS = {"00480": SAL, "00095": SPCOND, "00010": TEMP}


def pull_usgs(out: Path) -> Path:
    p = out / "usgs_iv_2015.csv"
    if p.exists():
        return p
    rows = []
    s = requests.Session()
    s.headers["User-Agent"] = "swfl-water-map-analysis/1.0"
    for site in USGS_SITES:
        r = s.get("https://waterservices.usgs.gov/nwis/iv/", timeout=600, params=dict(
            format="rdb", sites=site, parameterCd=",".join(PARAMS), startDT="2015-05-01", endDT="2015-09-30"))
        if r.status_code != 200:
            print("no data", site, r.status_code)
            continue
        lines = [ln for ln in r.text.splitlines() if not ln.startswith("#")]
        if len(lines) < 3:
            continue
        hdr = lines[0].split("\t")
        df = pd.DataFrame([ln.split("\t") for ln in lines[2:]], columns=hdr)
        off = df["tz_cd"].map({"EDT": "-04:00", "EST": "-05:00"})
        t = pd.to_datetime(df["datetime"] + off, utc=True, format="%Y-%m-%d %H:%M%z")
        for c in hdr:
            parts = c.split("_")
            if len(parts) == 2 and parts[1] in PARAMS:
                rows.append(pd.DataFrame({"time": t, "station_uid": f"usgs:{site}", "variable": PARAMS[parts[1]],
                                          "value": pd.to_numeric(df[c], errors="coerce"),
                                          "qc": df.get(c + "_cd", ""), "sensor": parts[0]}))
        print("pulled", site)
    d = pd.concat(rows, ignore_index=True).dropna(subset=["value"])
    d.to_csv(p, index=False)
    return p


def load_enp(enp_dir: Path) -> pd.DataFrame:
    frames = []
    for f in sorted(enp_dir.glob("enp_*.csv")):
        df = pd.read_csv(f, skiprows=[1])
        if "time" not in df or df.empty:
            continue
        df["time"] = pd.to_datetime(df["time"], utc=True)
        uid = f"erddap-secoora:gov-nps-ever-{f.stem.split('_', 1)[1]}"
        for col, var in (("sea_water_practical_salinity", SAL), ("sea_water_temperature", TEMP)):
            if col in df:
                frames.append(pd.DataFrame({"time": df["time"], "station_uid": uid, "variable": var,
                                            "value": pd.to_numeric(df[col], errors="coerce"), "qc": "", "sensor": col}))
    return pd.concat(frames, ignore_index=True).dropna(subset=["value"]) if frames else pd.DataFrame()


def main(data_dir: str, enp_dir: str | None = None) -> None:
    out = Path(data_dir)
    out.mkdir(parents=True, exist_ok=True)
    d = pd.read_csv(pull_usgs(out), dtype={"sensor": str, "qc": str})
    d["time"] = pd.to_datetime(d["time"], utc=True, format="ISO8601")
    if enp_dir:
        d = pd.concat([d, load_enp(Path(enp_dir))], ignore_index=True)
    d["qc"] = d["qc"].fillna("")
    d["sensor"] = d["sensor"].fillna("").astype(str)
    d["depth_tag"] = "surface"
    d["depth_m"] = np.nan
    d["time_utc"] = d["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg = proc_config()["qc"]
    df = clean.run_series_tests(d, datetime(2015, 10, 1, tzinfo=timezone.utc), cfg)
    df, whole = clean.add_derived_salinity(df, cfg)
    fl = clean.flags_from_bits(df["bits"].to_numpy())
    df["flag"] = fl
    sal = df[(df["variable"] == SAL) & ~df["superseded"]]
    print(f"\nLimits: salinity suspect above {cfg['gross_range'][SAL]['suspect'][1]}, bad above "
          f"{cfg['gross_range'][SAL]['fail'][1]}; conductance suspect above {cfg['gross_range'][SPCOND]['suspect'][1]:,.0f}, "
          f"bad above {cfg['gross_range'][SPCOND]['fail'][1]:,.0f}")
    print(f"Salinity readings May to Sep 2015: {len(sal)} (whole-PSU USGS salinity replaced from conductance at "
          f"{len(whole)} gauges)")
    for thr in (42, 50, 55, 60):
        x = sal[sal["value"] > thr]
        print(f"  above {thr} PSU: {len(x)} readings, {int((x['flag'] == 1).sum())} good, "
              f"{int((x['flag'] == 3).sum())} suspect, {int((x['flag'] == 4).sum())} bad")
    x = sal[(sal["value"] > 42) & (sal["flag"] != 1)]
    if len(x):
        print("  tests that flagged readings above 42 PSU:",
              x["bits"].map(lambda b: clean.test_names(int(b))).value_counts().to_dict())
    top = sal.sort_values("value").groupby("station_uid").tail(1).sort_values("value", ascending=False)
    print("\nHighest salinity per station, summer 2015, and its flag:")
    for r in top.head(12).itertuples():
        print(f"  {r.station_uid:42s} {r.value:6.2f} PSU  {r.time_utc}  flag {r.flag} {clean.test_names(int(r.bits))}"
              f"{'  (PSS-78 extrapolated above 42)' if r.derived == 'pss78' and r.value > 42 else ''}")
    sc = df[df["variable"] == SPCOND]
    print(f"\nConductance above 75,000 uS/cm: {int((sc['value'] > 75000).sum())} readings, "
          f"{int(((sc['value'] > 75000) & (sc['flag'] == 1)).sum())} good; max {sc['value'].max():,.0f}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
