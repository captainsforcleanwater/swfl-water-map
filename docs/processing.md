# Clean and blend

Pipeline steps 2 and 3: quality control on the raw sensor readings, then hourly temperature and
salinity grids. Code is in `processing/`, settings in `config/processing.json`. The hourly workflow
runs `python -m processing.run` right after the collector, in its own step with its own time limit,
so a failure there never stops the raw data from being committed.

## Cleaning

Raw files in `data/sensors/` are read, never changed. The clean layer sits beside them:

| File | What |
|---|---|
| `data/qc/YYYY/MM/YYYY-MM-DD_flags.csv` | Every raw reading flagged suspect (3) or bad (4), with the tests it failed. Readings not listed are good (1). |
| `data/clean/YYYY/MM/YYYY-MM-DD.csv` | Hourly cleaned values: one row per physical station, variable, depth layer and hour. Mean of the good readings within 30 minutes of the hour. |
| `data/clean/stations.csv` | Physical stations: member feeds, co-located stations not merged (and why), setting tag, water depth, depth layers, which layer feeds the temperature grid, whether each grid uses it (and why not), newest reading, current or stale. |

Each run re-checks the last 3 days (reading 2 more days for context), because late data and the
tests that look at neighbouring hours can change a flag after the fact.

Tests (codes as in the IOOS QARTOD manuals; thresholds in `config/processing.json`):

| Test | Flag | Rule |
|---|---|---|
| provider | bad | the source marked the reading unusable (USGS equipment, ice, maintenance codes) |
| time | bad | timestamp more than an hour in the future |
| range | bad / suspect | outside -2 to 40 C, 0 to 70 PSU (bad); outside 5 to 35 C, above 55 PSU (suspect) |
| spike | suspect | further than 1.5 C or 4 PSU from the median of readings within 90 minutes either side, and more than 6 times the local spread. A real front is a step, not a spike, and passes. |
| flatline | suspect, then bad | identical readings for 12 h (suspect) or 24 h (bad) for temperature, 24 h / 48 h for salinity, doubled below the surface. Salinity sitting at the fresh-water floor, or flat while conductance keeps changing, is rounding and is not flagged. |
| neighbor | suspect | temperature more than 2.5 C from the median of stations within 15 km by water, held over a rolling 48-hour window |
| stale | station status | newest reading older than 6 h plus the provider's normal delay (10 h for Everglades National Park) plus two reporting intervals |
| hold | suspect | set by hand in `config/station_tags.json` (`qc_hold`) while a sensor is under review |

Highway Creek's temperature is on hold: its offset from nearby stations grew to about +4 C in
November 2026, the same day and night. Long Sound and Trout Cove run about 2 C warm this week and
are not flagged at the 2.5 C threshold. Eighteen months of history for all three are examined in
[florida_bay_offsets.md](florida_bay_offsets.md).

Other rules:

- **Salinity from conductance.** Salinity is computed with PSS-78 (UNESCO 1983, with the Hill et
  al. 1986 low-salinity extension) at 25 C where a station has specific conductance but no salinity
  at a depth, and where USGS reports salinity only in whole PSU (the reported value stays in the raw
  files but is not used). PSS-78 matches the UNESCO check value and agrees with USGS-reported
  salinity to within their rounding. On the Nov 11 to 14 data it fills in the Peace River at Harbour
  Heights, and replaces whole-PSU salinity at Turner River, East Side Creek and four bottom sensors.
- **One series per physical station.** Stations flagged `possible_duplicate_of` in the inventory,
  and stations within 300 m of each other measuring the same thing, are merged only when their
  readings agree (median difference over at least 12 shared hours within 0.3 C and 1 PSU, comparing
  the values each would give the grids). Otherwise both stay. Checked 2026-11-14:
  - C13 (GCOOS plus two SECOORA datasets), C22 (GCOOS and SECOORA) and C27 (two SECOORA datasets)
    agree to within 0.02 C and 0.07 PSU over Oct 1 to Nov 3. The SECOORA feeds all stopped on
    Nov 3, so for now nothing is merged and only the GCOOS feeds report; C27 has no working feed.
  - Trout Cove (NPS TCVF1, surface) and Trout Creek (USGS 251253080320100, bottom), 240 m apart,
    differ by 1.2 C over this week and are kept as two stations.
  Each hour of a merged station uses the highest-priority feed with a good reading; the `source`
  column says when that was not the station's own feed. `data/clean/stations.csv` lists pairs that
  were not merged and why.
- **Depth layers.** Each series gets a layer: `surface` (tagged surface, or a mid sensor no deeper
  than 2 m), `bottom`, `mid`, or `profile` (thermistor strings such as the Faka Union boat basin's,
  including one sensor below the sediment). Salinity grids use `surface` only. Temperature grids use
  `surface`, or, where a station has no surface sensor and the water is no deeper than 3 m, its `mid`
  or `bottom` sensor. Depth is the deepest NOAA Coastal Relief Model cell within 300 m (a creek
  narrower than the model counts as shallow), kept in `data/grid/station_depth.json`. This adds
  seven USGS gauges in the Ten Thousand Islands and Florida Bay creeks, plus Broad River's mid-depth
  sensor, to the temperature grid.
- **Inland stations.** The inventory now carries a `setting` tag: `coastal` or `inland`. USGS
  wetland, canal, ditch and lake sites are inland by rule; `config/station_tags.json` sets the rest
  (so far only SCCF Alva, above the S-79 lock). Inland stations stay in the clean layer but feed
  neither grid. Coastal stations that sit more than 2.5 km from mapped tidal water (Faka Union boat
  basin, Bottle Creek) are also left out of the grids, and `data/clean/stations.csv` says so.

## Library versions

`requirements.txt` pins every package the workflow installs, including indirect ones, to exact
versions, generated from `requirements.in` with `uv pip compile`. Tested on Python 3.12 (the
workflow's version) with those versions.

## Grid and water paths

- **Output grid:** the area bbox in 0.0045 x 0.005 degree cells (about 500 m), 578 rows x 660
  columns, north to south. Cells outside the area polygon (Atlantic side of the Keys) hold no data.
- **Water mask:** 100 m, from the USGS National Hydrography Dataset (Sea/Ocean, Bay/Inlet,
  Stream/River, Foreshore, Lake/Pond). NHD's offshore polygons have rectangular gaps, so anything
  deeper than 1 m in the NOAA Coastal Relief Model also counts as water. The mask is cut at the S-79
  Franklin Lock, and water not connected to the Gulf is dropped. Stored as a 52 KB image in
  `data/grid/water_fine.png`; rebuild with `python -m processing.shoreline` (needs `pyshp`) when the
  grid or the list of barriers changes. Both sources are on hosts this session can reach.
- **Water-path distance:** inside each 500 m cell the 100 m water cells are split into connected
  pieces, each piece is a graph node, and touching pieces are linked. A cell cut by a barrier island
  becomes two nodes, so narrow rivers stay open at 500 m and thin islands stay closed. Example:
  Tarpon Bay to the Gulf off Sanibel's beach is 5.9 km in a straight line and 12.3 km by water.
- **Cache:** distances from each station's node to every node are cached in `build/cache/distance`,
  keyed by the mask hash and the node; the workflow keeps them with `actions/cache`. The graph builds
  in about 8 s and a new station's distances take about 0.1 s, so a cold cache costs little.

## Blending

Both variables use optimal interpolation (OI) with a correlation that falls off with water-path
distance d as (1 + d/L) exp(-d/L). OI also stops clusters (37 Everglades stations) from outvoting
lone buoys, and gives an expected error for every cell.

**Temperature.** The background is GOES-19: each pixel carries its most recent clear value forward
(up to 7 days), after dropping pixels next to cloud and pixels far from their neighbours. The
background error grows with the value's age (calibrated against the sensors:
sqrt(0.7^2 + (0.025 x age_h)^2) C) and with water-path distance from the nearest satellite cell.
Water the satellite never sees (rivers, creeks, back bays) fades from the nearest satellite value
into a regional mean of the sensors over 10 km of water path, so a river does not inherit one stale
coastal pixel. Sensor-minus-background corrections are then spread with L = 30 km.

**Salinity.** Sensors only. The background is the Gulf salinity (median of the offshore buoys this
hour, else 36) in open water, fading over 10 km of water path into a regional mean of the sensors.
OI adds local detail with L = 12 km.

**Confidence.** 100 x (1 - expected error / scale), floored at 0, with scale 2 C for temperature and
6 PSU for salinity. Temperature confidence drops as satellite values age and away from sensors;
salinity confidence is high near sensors and low across the open Gulf away from the buoys.
Salinity confidence measures sensor coverage, not a calibrated error. In the leave-one-out check,
errors at the offshore buoys are small even at low confidence because the Gulf is uniform, and
errors at fresh estuary sites are large even at middling confidence. The map should hide salinity
below a threshold (proposed: 40, about 10 km by water from a sensor) rather than fade it.

**Re-blending.** Each run works out a signature for every hour in the last 24: the hourly sensor
values and the list of GOES-19 frames available for it. Any hour whose signature changed is
blended again, so Everglades data (6 to 8 hours late) and GOES-19 frames (4 to 5 hours late) are
folded in as they arrive. Hours missing from the last 5 days are blended too. Run with
`--start <time> --force`, or the workflow's `reblend_from` input, to redo a range.

## Leave-one-out check

`python -m processing.validate --start ... --end ...` removes each station in turn, blends from the
others and compares with what it read. Results for Nov 11 17Z to Nov 14 18Z are in
[validation/leave_one_out.md](validation/leave_one_out.md). The settings were chosen on these same
three days, so treat the numbers as a little optimistic.

## Output files

### Coastline for the map

A 500 m cell counts as water if any of it is water, which keeps narrow rivers open but would erase
Captiva, Cayo Costa and Gasparilla if the map drew the cells. The map draws from the 100 m mask
instead. `python -m processing.display` (run by `processing.shoreline`) writes:

- `data/grid/display_100m.bin.gz` (108 KB, same file format as the hourly grids): layer `piece`,
  100 m over the output area, 5 x 5 pixels per 500 m cell. 0 = land or no data; 1 = the main water
  piece of its 500 m cell; k >= 2 = secondary piece k - 2, such as the far side of a barrier island
  inside the same cell. There are 1,569 secondary pieces.
- `data/grid/land_100m.geojson` (1.0 MB): land polygons traced from the same mask, simplified by
  30 m, for vector drawing.

The hourly files carry values for the secondary pieces in `*_extra` layers, so each 100 m water
pixel gets the value of its own piece: the cell's value for piece 1, element k - 2 of the `_extra`
layer for piece k. `processing/quicklook.py` draws this way.

### Hourly files

One file per UTC day, `swfl_blend_hourly_YYYY-MM-DD.bin.gz`, attached to a monthly GitHub Release
`blend-hourly-YYYY-MM`, like the GOES-19 archive. `data/blend/index.json` lists the hours in each
file with their input signatures and download URLs. Nothing large goes into git.

The format is a small header plus raw arrays, gzip-compressed, so a browser can read it with
built-in APIs (`DecompressionStream` and typed arrays) and no library. The layout and a 12-line
JavaScript decoder are in the docstring of `processing/gridfile.py`; the decoder was checked against
the Python reader on real files. Layers, all values rather than colours:

| Layer | Type | Meaning |
|---|---|---|
| `temp_c` | int16 x 0.05 | temperature, C (0.05 C = 0.09 F; sensors are good to about 0.1 C) |
| `temp_conf` | uint8 | temperature confidence, 0 to 100 |
| `sal_psu` | int16 x 0.05 | salinity, PSU |
| `sal_conf` | uint8 | salinity confidence, 0 to 100 |
| `water_pct` | uint8, static | share of the cell that is water; 255 marks cells with no data |
| `temp_c_extra`, `temp_conf_extra`, `sal_psu_extra`, `sal_conf_extra` | as above, hours x 1,569 | values for the secondary water pieces in `display_100m.bin.gz`; use only when `pieces_key` matches |

The header also lists, for each hour, the stations used with their values, the newest GOES-19 frame
and the median pixel age. Arrays are delta coded along hours and columns before gzip.

**Size and run time at 500 m:** 2.4 to 2.6 MB per full day (about 0.9 GB a year, 1.9 GB for the
proposed two years of hourly history). A blended hour takes about 1.2 s; a normal run re-blends up
to 24 hours in under a minute; the first run's 5-day backfill takes about 3 minutes. At 1 km both
would shrink about fourfold, but the tidal Caloosahatchee above Fort Myers (300 to 500 m wide) and
Estero Bay's back bays would shrink to a cell or less. At 250 m files would be about 4 times larger
(around 4 GB a year). These two are estimates scaled from the 500 m run, not measured.

**Serving to a browser.** A page on GitHub Pages cannot read these release files with `fetch()`.
Checked 2026-11-14: the download URL answers with a redirect that carries no
`Access-Control-Allow-Origin` header, and neither does the storage host it redirects to. The API's
asset endpoint does send the header but redirects to the same storage host. Files in the git
repository are fine (raw.githubusercontent.com sends `Access-Control-Allow-Origin: *`), so the
clean CSVs can be read directly. How to serve the grids is a map-step decision; options are copying
recent days to GitHub Pages, a small CORS proxy, or object storage with CORS (where the history plan
already puts long-term archives).

## Running locally

```bash
pip install -r requirements-dev.txt      # exact versions; regenerate from requirements*.in with uv pip compile
python -m processing.run --store local                       # day files go to build/blend
python -m processing.run --store local --start 2026-11-09T11 --force
python -m processing.quicklook build/blend/swfl_blend_hourly_2026-11-14.bin.gz --hour 15
python -m processing.validate --start 2026-11-11T17 --end 2026-11-14T17
python -m pytest tests
```
