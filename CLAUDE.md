# SWFL water temperature and salinity map

Project notes for Claude Code sessions working in this repository. Owner: Captains For Clean Water (CFCW). Items marked *proposed* are recommendations CFCW has not confirmed yet. Don't treat them as settled.

## Goal

A live map of water temperature and salinity along Florida's Gulf coast from Englewood to the Keys. It uses every public sensor in the area plus satellite data, with as many readings per day as the sources allow, and a history feature for scrubbing back in time and comparing dates. The core idea: **sensors give truth, satellite gives shape, and the two are blended every hour.**

## Decisions

| Topic | Decision |
|---|---|
| Area | Gulf side only for now, Englewood to the Keys. The Atlantic side of the Keys is excluded. Build the area as one setting so it can be widened later. |
| Florida Bay | Included (assumed, not yet confirmed). |
| Captain-sourced data | No. |
| Salinity layer | Yes. |
| History feature | Yes. Retention plan is *proposed* (see History plan). |
| Readings | As many per day as possible. |
| Hosting | Collector and archiving run on GitHub Actions in this public repo. The map runs on GitHub Pages and should be embeddable on the CFCW site. |
| Development | Claude Code cloud sessions. Sessions push to a branch and open a pull request; CFCW merges. |

## How this repo works

- Production data fetching happens on GitHub's hosted runners, which have open internet. Standard runners are free for public repos, so an hourly job costs nothing.
- The scheduled workflow only runs from `main`. The hourly collector starts once its pull request is merged.
- The Claude Code session environment should allow these domains so sources can be tested from the session:
  `*.noaa.gov`, `*.usgs.gov`, `erddap.secoora.org`, `erddap.gcoos.org`, `erddap.sensors.ioos.us`, `recon.sccf.org`.
  A 403 with `x-deny-reason: host_not_allowed` means the session allowlist blocked it, not the source.

## Build rules

- Discover stations automatically by bounding box. Hard-code nothing except fallbacks.
- Every run re-pulls a trailing window and dedupes: the last several hours for sensors, and any missing hour in the last 5 days for GOES-19. GitHub's cron is best-effort and can delay or skip runs, so this keeps gaps from forming.
- Schedule at an off-peak minute (for example `17 * * * *`) and include `workflow_dispatch` so runs can be started by hand.
- The workflow needs `permissions: contents: write` to commit outputs.
- GitHub disables scheduled workflows in public repos after 60 days with no repository activity. The collector's commits should count as activity, but confirm.
- Keep large grids out of git history. *Proposed* until object storage is chosen: pack each day's hourly satellite subset into one compressed file and attach it to a GitHub Release. Raw sensor readings are small and can live in the repo as daily files.
- Convert everything to UTC and consistent units. Display temperature in °F.
- Style for docs, UI text, code comments and commit messages: plain language, no em dashes.

## Pipeline

1. **Hourly collector** (scheduled GitHub Actions job): pulls every source inside the area.
2. **Clean and tag:** range checks, spike removal, flatline/stuck-sensor and stale-data detection. Tag depth: buoys report several depths, and the main map uses near-surface, with an optional bottom toggle.
3. **Blend into a grid:**
   - Temperature: the satellite field is the background. At each sensor, compute sensor minus satellite, interpolate those corrections across the grid and add them back. When clouds block an hour, fall back to the last clear frame corrected by sensors.
   - Interpolate through water, not across land. Use a coastline mask with water-path (cost) distance, so Caloosahatchee readings don't bleed across Sanibel into the Gulf.
   - Salinity: sensor-only. Satellite salinity is far too coarse near shore. It will be strongest in the estuaries and shaded as low-confidence elsewhere.
   - A confidence layer shades areas far from any sensor or under cloud.
4. **Live map** (GitHub Pages): hourly frames on a time slider, isotherms in °F, a thermal fronts layer, confidence shading, temperature/salinity toggle, and tap-a-sensor charts.

## Data sources

**Proposed bounding box:** about 24.4 to 27.0 N and 83.6 to 80.3 W, masked to drop the Atlantic side of the Keys. Englewood is near 26.96 N.

| Source | What it gives | Cadence | Access notes |
|---|---|---|---|
| NOAA CO-OPS tide stations | Water temp; conductivity/salinity at some stations | 6 min | Data API `api.tidesandcurrents.noaa.gov/api/prod/datagetter`, product `water_temperature`. 6-minute data is limited to 1 month per request. Use the Metadata API for station discovery. Verified 2026-11-14: Fort Myers 8725520, Vaca Key 8723970 and Key West 8724580 report water temperature; Naples 8725110 does not, and no CO-OPS station in the area reports conductivity or salinity. Key West sits on the Atlantic side; whether it stays as an edge anchor is an open question. Also mirrored on CoastWatch ERDDAP as dataset `nosCoopsMWT`. |
| USGS gages | Water temp (param 00010), salinity (00480), specific conductance (00095) | 15 min | Charlotte Harbor, Estero Bay, tidal Caloosahatchee, Ten Thousand Islands, Florida Bay. The collector uses the Water Data OGC API v1 (`api.waterdata.usgs.gov/ogcapi/v1`, collections `time-series-metadata`, `monitoring-locations`, `continuous`). Many estuarine gages are typed "Stream" or "Lake", so filtering by site type does not separate fresh from salt water. As of 2026-11-14, Port Boca Grande 02293332, Cape Haze transect 264420082100000 and Estero Bay 262620081523700 have no current temperature or salinity series in either the new or legacy service. |
| SCCF RECON | Temp, salinity, DO, chlorophyll, more | Hourly | On GCOOS ERDDAP as datasets `SCCF_<n>` (found 2026-11-14; 7 active in the area). Running since 2007; sensors replaced in the RECON 2.0 upgrade (2021 onward). |
| USF COMPS buoys | Temp, salinity | Near real time | C10 off Sarasota (NDBC 42013) and C12 (NDBC 42022) sit north of Englewood but anchor the offshore edge (listed as edge anchors in `config/area.json`). C13 West Florida South Buoy is at 26.01 N, 83.086 W. Current data is on GCOOS ERDDAP (`USF_COMPS_C10`, `C12`, `C13`, `C22`), which carries one near-surface temperature and salinity sensor per buoy, repeated across the current-meter depth bins. NDBC's copy of C13 (42023) stopped updating on 2026-11-03. |
| NDBC buoys and C-MAN | Water temp | Hourly-ish | Discover via the NDBC active stations list and keep the Gulf side only. Pulaski Shoal and Molasses Reef were decommissioned in early 2023. |
| Everglades National Park | Water temp, level, rain, salinity | Hourly | On SECOORA ERDDAP as `gov-nps-ever-<id>` (37 active in the area). Data arrives 6 to 8 hours late, which is why the sensor window is 24 hours. |
| GOES-19 satellite SST | Hourly SST, about 2 km grid | Hourly | CoastWatch Caribbean/Gulf node: `cwcgom.aoml.noaa.gov/erddap/griddap/goes19SSThourly`. **Appears to keep only about 5 days of data, so archive from the first run.** |
| ACSPO L3S-LEO (polar orbiters) | Daily 0.02° super-collated SST plus thermal fronts | Daily | `noaacwLEOACSPOSSTL3SCDaily` (reanalysis). As of 2026-11-14 it is gone from `coastwatch.noaa.gov/erddap`; `cwcgom.aoml.noaa.gov/erddap` and `coastwatch.pfeg.noaa.gov/erddap` list it (and the NRT version) but data requests redirect to coastwatch.noaa.gov and return 404. Find a working source before the backfill (step 4). |

**Aggregator shortcuts** (bounding-box queries, harmonized formats):
- GCOOS ERDDAP: erddap.gcoos.org
- SECOORA ERDDAP: erddap.secoora.org
- IOOS Environmental Sensor Map ERDDAP: erddap.sensors.ioos.us/erddap

**Volume estimates:** roughly 5,000 to 10,000 sensor readings a day once the station list is pinned down, and a few hundred thousand satellite pixel readings on a clear day. Clouds cut the satellite count.

## History plan (*proposed*)

- **Raw sensor readings:** keep forever (tens of MB a year).
- **Hourly blended maps (temp and salinity):** rolling 2 years at full detail, then thin to daily low, average and high.
- **Event days** (cold snaps, discharge pulses, heat waves): keep hourly forever.
- **Daily maps:** keep forever, backfilled to 2012 from ACSPO L3S plus sensor archives. This covers the 2016 and 2018 benchmark years and gives about 14 years for a "normal for this date" baseline.
- Salinity history depends on which sensors existed each year. USGS Estero Bay goes back to 2001 and RECON to 2007, so a sensor-only replay of the January 2010 cold snap is possible.
- **Storage:** two years of hourly grids is well under a few GB. Long-term archives go in cheap object storage, not in the git repository.

History features: scrub any day back to 2012 or any hour in the last 2 years; a difference map (for example, today minus the same date in 2018); departure from normal for the date; event replays, such as a discharge plume shown in salinity and temperature side by side; full record per sensor.

## Next steps

1. Done: repository created (`captainsforcleanwater/swfl-water-map`).
2. Done (pending merge): the collector in `collector/`, inventory in `STATIONS.md`. Write the collector. **Its first run produces the station inventory:** every sensor in the area with ID, provider, location, cadence, depth and whether it measures salinity. Commit it as a readable file.
3. Done (pending merge): `.github/workflows/collect.yml`. Start the hourly collector and archiving immediately, before the map is polished. GOES-19 hourly history older than about 5 days can't be recovered later.
4. Backfill daily history to 2012.
5. Ship the v1 map with the salinity layer and history.

## Open questions for CFCW

- Approve or adjust the history plan.
- Confirm Florida Bay is in, and decide whether Key West's Atlantic-side station stays as an edge anchor.
- Is the map public from day one, or internal first?
- SCCF RECON access: is there a contact at SCCF who could provide a data feed if needed?
