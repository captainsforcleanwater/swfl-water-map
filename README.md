# swfl-water-map

Live water temperature and salinity for Florida's Gulf coast, Englewood to the Keys. Project notes and
decisions are in [CLAUDE.md](CLAUDE.md). The current list of sensors is in [STATIONS.md](STATIONS.md).

## Hourly collector

`.github/workflows/collect.yml` runs `python -m collector.run` every hour at :17 UTC (and on demand from
the Actions tab). Each run:

1. Rediscovers stations once a day by bounding box (CO-OPS Metadata API, USGS Water Data API, GCOOS,
   SECOORA and IOOS ERDDAP, NDBC active stations list). Stations must fall inside the area polygon in
   `config/area.json`, which leaves out the Atlantic side of the Keys, plus any `edge_anchors` listed there.
2. Pulls the last 24 hours from every active station and merges into daily files, replacing older copies
   of the same reading. GitHub's cron can skip runs, so the overlap keeps gaps from forming. The first run
   pulls 72 hours.
3. Checks the last 5 days of GOES-19 hourly SST and archives any hour not yet saved.
4. Commits new data, the inventory, and `data/status/last_run.json`.

One source failing does not stop the others. Errors are listed in `data/status/last_run.json` and in the
run summary on the Actions page.

## Where the data goes

| What | Where | Format |
|---|---|---|
| Sensor readings | `data/sensors/YYYY/MM/YYYY-MM-DD_<source>.csv` | `time_utc, station_uid, variable, value, depth_m, depth_tag, sensor, qc` |
| Station inventory | `STATIONS.md`, `data/stations.csv`, `data/stations.json` | |
| GOES-19 hourly SST | GitHub Releases tagged `goes19-sst-hourly-YYYY-MM`, one file per UTC day | NetCDF4, SST in degrees C, packed to 0.01 C |
| GOES-19 archive index | `data/goes19/index.json` | which hours are saved, with download URLs |

Variables: `water_temp_c` (degrees C), `salinity_psu` (PSU), `spec_cond_us_cm` (specific conductance at
25 C, microsiemens per cm). All times are UTC. The map converts temperature to degrees F for display.
Readings are raw: range checks, spike and stuck-sensor detection come in the next pipeline step.

## Running locally

```bash
pip install -r requirements.txt
python -m collector.run --goes-store local    # GOES-19 files go to build/goes19 instead of a release
python -m collector.run --discover --lookback-hours 6 --goes-store off
```

Optional: set `USGS_API_KEY` (free from api.data.gov) as a repository secret for higher USGS rate limits.
