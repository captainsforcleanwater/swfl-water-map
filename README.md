# swfl-water-map

Live water temperature and salinity for Florida's Gulf coast, Englewood to the Keys. Project notes and
decisions are in [CLAUDE.md](CLAUDE.md). The current list of sensors is in [STATIONS.md](STATIONS.md).

## Hourly collector

`.github/workflows/collect.yml` runs `python -m collector.run` every hour at :17 UTC (and on demand from
the Actions tab). Each run:

1. Rediscovers stations once a day by bounding box (CO-OPS Metadata API, USGS Water Data API, GCOOS,
   SECOORA and IOOS ERDDAP, NDBC active stations list). Stations must fall inside the area polygon in
   `config/area.json`, which leaves out the Atlantic side of the Keys, plus any `edge_anchors` listed there.
2. Checks the last 5 days of GOES-19 hourly SST and archives any hour not yet saved. This runs first because
   the server keeps only 5 days, so it is the one source that cannot be caught up later.
3. Pulls the last 24 hours from every active station and merges into daily files, replacing older copies
   of the same reading. GitHub's cron can skip runs, so the overlap keeps gaps from forming. The first run
   pulls 72 hours.
4. Cleans and blends (`python -m processing.run`, below), in its own step with its own time limit.
5. Commits new data, the clean layer, the inventory, and the status files.

One source failing does not stop the others. The collect step has a 20 minute limit so the commit step
always runs. If a saved GOES-19 day file cannot be downloaded, that day is skipped and retried next run;
it is rebuilt from the server only when the file is truly missing from the release. Errors are listed in `data/status/last_run.json` and in the
run summary on the Actions page.

## Where the data goes

| What | Where | Format |
|---|---|---|
| Sensor readings | `data/sensors/YYYY/MM/YYYY-MM-DD_<source>.csv` | `time_utc, station_uid, variable, value, depth_m, depth_tag, sensor, qc` |
| Station inventory | `STATIONS.md`, `data/stations.csv`, `data/stations.json` | |
| GOES-19 hourly SST | GitHub Releases tagged `goes19-sst-hourly-YYYY-MM`, one file per UTC day | NetCDF4, SST in degrees C, packed to 0.01 C |
| GOES-19 archive index | `data/goes19/index.json` | which hours are saved, with download URLs |
| QC flags | `data/qc/YYYY/MM/YYYY-MM-DD_flags.csv` | raw readings flagged suspect (3) or bad (4), with the tests failed |
| Hourly cleaned values | `data/clean/YYYY/MM/YYYY-MM-DD.csv` | `time_utc, station, variable, layer, value, n, depth_m, source` |
| Physical stations | `data/clean/stations.csv` | member feeds, setting, grid use, status |
| Blended hourly grids | GitHub Releases tagged `blend-hourly-YYYY-MM`, one file per UTC day | gzip, format in `processing/gridfile.py` |
| Blended grid index | `data/blend/index.json` | which hours are saved, input signatures, download URLs |
| Water mask | `data/grid/water_fine.png`, `data/grid/grid.json` | 100 m, from USGS NHD and the NOAA Coastal Relief Model |
| Map coastline | `data/grid/display_100m.bin.gz`, `data/grid/land_100m.geojson` | 100 m water pieces and land polygons; the map draws from these, not the 500 m cells |
| Station depths | `data/grid/station_depth.json` | NOAA Coastal Relief Model depth near each station |

Variables: `water_temp_c` (degrees C), `salinity_psu` (PSU), `spec_cond_us_cm` (specific conductance at
25 C, microsiemens per cm). All times are UTC. The map converts temperature to degrees F for display.
Readings in `data/sensors/` are raw and never changed; QC flags and cleaned values live in `data/qc/` and
`data/clean/`.

## Clean and blend

`python -m processing.run` runs after the collector in the same workflow. It flags bad readings (range,
spike, stuck sensor, disagreement with nearby stations, stale stations), computes salinity from conductance
where a station has none (or where USGS reports only whole PSU), merges duplicate feeds only when their
readings agree, and writes hourly cleaned values. Then it
blends hourly grids at about 500 m: GOES-19 sets the temperature pattern and the sensors correct it; salinity
comes from the sensors alone. Both are interpolated by distance through water, so readings never cross land,
and each has a confidence grid. Hours in the last 24 whose inputs changed are blended again. Method, file
format and leave-one-out results: [docs/processing.md](docs/processing.md). Sample images: `docs/samples/`.

## Running locally

```bash
pip install -r requirements.txt               # exact versions, same as the workflow
python -m collector.run --goes-store local    # GOES-19 files go to build/goes19 instead of a release
python -m collector.run --discover --lookback-hours 6 --goes-store off
python -m processing.run --store local        # blended day files go to build/blend instead of a release
```

Optional: set `USGS_API_KEY` (free from api.data.gov) as a repository secret for higher USGS rate limits.
