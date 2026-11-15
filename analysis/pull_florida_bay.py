"""Pull 18 months of history for the northeast Florida Bay offset check.

    python analysis/pull_florida_bay.py DATA_DIR

Then run analysis/florida_bay_offsets.py DATA_DIR OUT_DIR. Sources: SECOORA ERDDAP (Everglades National
Park stations, FIU Key Largo, USDA NRCS SCAN 2051 Everglades ARS weather), NOAA CO-OPS Vaca Key wind,
USGS NWIS instantaneous values for Trout Creek (251253080320100).
"""
import sys, time
from pathlib import Path
import requests
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
START, END = "2025-05-01T00:00:00Z", "2026-11-14T23:59:00Z"
s = requests.Session(); s.headers["User-Agent"] = "swfl-water-map-analysis/1.0"
def get(url, **kw):
    for i in range(4):
        try:
            r = s.get(url, timeout=300, **kw)
            if r.status_code == 200:
                return r
            print("HTTP", r.status_code, url[:120], r.text[:200], flush=True)
            if r.status_code in (400, 404):
                return None
        except Exception as e:
            print("err", e, flush=True)
        time.sleep(5 * (i + 1))
    return None
ENP = ["hcef1", "lsnf1", "tcvf1", "jbyf1", "mnbf1", "lbsf1", "mdkf1", "bwsf1", "dkkf1", "thrf1", "lmdf1", "trrf1", "bnkf1"]
for sid in ENP:
    p = OUT / f"enp_{sid}.csv"
    if p.exists(): continue
    # Manatee Bay (mnbf1) has no water level variable; the request without it is retried below.
    q = ("time,sea_water_temperature,sea_water_temperature_qc_agg,sea_water_practical_salinity,"
         "water_surface_above_navd88,lwe_thickness_of_precipitation_amount_cm_time__sum_over_pt1h"
         f"&time>={START}&time<={END}")
    r = get(f"https://erddap.secoora.org/erddap/tabledap/gov-nps-ever-{sid}.csv?" + q.replace(">", "%3E").replace("<", "%3C"))
    if r is None:
        q2 = q.replace(",water_surface_above_navd88", "")
        r = get(f"https://erddap.secoora.org/erddap/tabledap/gov-nps-ever-{sid}.csv?" + q2.replace(">", "%3E").replace("<", "%3C"))
    if r is not None:
        p.write_text(r.text); print("ok", sid, len(r.text.splitlines()), flush=True)
r = get("https://erddap.secoora.org/erddap/tabledap/key-largo-fl.csv?time,sea_water_temperature,sea_water_practical_salinity"
        f"&time%3E={START}&time%3C={END}")
if r is not None: (OUT / "fiu_key_largo.csv").write_text(r.text); print("ok fiu", flush=True)
r = get("https://erddap.secoora.org/erddap/tabledap/gov_usda_nrcs_scan_2051.csv?time,solar_irradiance_cm_time__mean_over_pt1h,"
        f"wind_speed,air_temperature,lwe_precipitation_rate_cm_time__sum_over_pt1h&time%3E={START}&time%3C={END}")
if r is not None: (OUT / "scan_2051.csv").write_text(r.text); print("ok scan", flush=True)
# CO-OPS Vaca Key wind, hourly, one request per month
rows = []
import datetime as dt
d = dt.date(2025, 5, 1)
while d <= dt.date(2026, 11, 14):
    e = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    r = get("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter", params=dict(
        product="wind", station="8723970", begin_date=d.strftime("%Y%m%d"), end_date=e.strftime("%Y%m%d"),
        interval="h", units="metric", time_zone="gmt", format="csv"))
    if r is not None:
        lines = r.text.strip().splitlines()
        rows += lines[1:] if rows else lines
    d = e + dt.timedelta(days=1)
(OUT / "coops_vaca_wind.csv").write_text("\n".join(rows) + "\n"); print("ok wind", len(rows), flush=True)
# USGS Trout Creek temperature (bottom), legacy IV service, then OGC API as fallback
r = get("https://waterservices.usgs.gov/nwis/iv/", params=dict(format="rdb", sites="251253080320100",
        parameterCd="00010", startDT="2025-05-01", endDT="2026-11-14"))
if r is not None and "#" in r.text[:5]:
    (OUT / "usgs_trout_creek_rdb.txt").write_text(r.text); print("ok usgs rdb", len(r.text.splitlines()), flush=True)
else:
    print("usgs legacy failed", flush=True)
