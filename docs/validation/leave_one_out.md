# Leave-one-out check

Period: 2026-11-11T17:00:00Z to 2026-11-14T18:00:00Z. Generated 2026-11-14T19:53:44Z by `python -m processing.validate`.

Each station is left out in turn, the hour is blended from the others with the settings in
`config/processing.json`, and the blended value at the station is compared with its reading.
Errors are blended minus measured. Temperature in degrees C (F in brackets), salinity in PSU.

| | Temperature | Salinity |
|---|---:|---:|
| Station-hours | 4486 | 3819 |
| Typical error (median absolute) | 0.46 (0.83 F) | 2.90 |
| Mean absolute error | 0.62 (1.12 F) | 3.88 |
| RMS error | 0.85 (1.53 F) | 5.62 |
| 90% of errors below | 1.40 (2.52 F) | 8.23 |
| Worst single hour | 3.45 (6.22 F) | 31.31 |
| Bias | +0.05 | +0.81 |
| Without sensors: typical error | 0.70 (satellite only) | 3.48 (background only) |
| Without sensors: RMS error | 1.45 | 7.03 |

## Temperature: stations with the largest errors

| Station | Name | Lat | Lon | Hours | Typical error (C) | Worst (C) | Bias (C) | Mean reading |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `erddap-secoora:gov-nps-ever-tcvf1` | Trout Cove, FL (TCVF1) | 25.213 | -80.533 | 72.0 | 1.96 | 2.59 | -1.49 | 29.31 |
| `usgs:02297460` | Peace River At Harbour Heights Fl | 26.988 | -81.994 | 74.0 | 1.85 | 2.98 | +1.79 | 23.95 |
| `erddap-secoora:gov-nps-ever-lsnf1` | Long Sound, FL (LSNF1) | 25.235 | -80.457 | 72.0 | 1.60 | 3.08 | -1.57 | 29.13 |
| `erddap-secoora:gov-nps-ever-tpef1` | Tarpon Bay East, FL (TPEF1) | 25.410 | -80.964 | 72.0 | 1.47 | 2.09 | -1.28 | 27.68 |
| `erddap-gcoos:SCCF_56` | SCCF 56 - SCCF RECON McIntyre Creek | 26.464 | -82.104 | 74.0 | 1.35 | 2.98 | +1.54 | 25.50 |
| `erddap-secoora:gov-nps-ever-thrf1` | Thursday Point, FL (THRF1) | 25.203 | -80.372 | 72.0 | 1.21 | 2.05 | +1.19 | 26.88 |
| `usgs:255534081324000` | Pumpkin River Near Goodland, Fl | 25.926 | -81.544 | 74.0 | 1.02 | 3.41 | +0.56 | 25.94 |
| `usgs:251253080320100` | Trout Creek At Mouth Near Key Largo, Fl | 25.215 | -80.534 | 74.0 | 0.95 | 1.92 | +0.07 | 27.81 |
| `usgs:255654081350200` | Blackwater River Near Goodland, Fl | 25.952 | -81.584 | 74.0 | 0.88 | 2.16 | +0.65 | 25.83 |
| `erddap-secoora:gov-nps-ever-canf1` | Cane Patch, FL (CANF1) | 25.422 | -80.942 | 72.0 | 0.86 | 1.38 | +0.73 | 26.30 |
| `ndbc:RKQF1` | Lower Henderson Creek, Rookery Bay Reserve, FL | 26.027 | -81.734 | 74.0 | 0.82 | 2.08 | -0.77 | 26.96 |
| `erddap-secoora:gov-nps-ever-mukf1` | Murray Key, FL (MUKF1) | 25.106 | -80.942 | 72.0 | 0.74 | 2.13 | +0.68 | 27.05 |

## Salinity: stations with the largest errors

| Station | Name | Lat | Lon | Hours | Typical error (PSU) | Worst (PSU) | Bias (PSU) | Mean reading |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `usgs:02297460` | Peace River At Harbour Heights Fl | 26.988 | -81.994 | 74.0 | 22.84 | 31.31 | +20.93 | 0.13 |
| `erddap-secoora:gov-nps-ever-canf1` | Cane Patch, FL (CANF1) | 25.422 | -80.942 | 72.0 | 11.14 | 14.50 | +11.03 | 0.37 |
| `erddap-secoora:gov-nps-ever-lmrf1` | Lostmans River, FL (LMRF1) | 25.556 | -81.169 | 72.0 | 9.44 | 18.18 | -10.66 | 22.55 |
| `erddap-secoora:gov-nps-ever-wiwf1` | Willy Willy, FL (WIWF1) | 25.587 | -81.044 | 72.0 | 8.94 | 13.88 | +9.56 | 2.10 |
| `erddap-secoora:gov-nps-ever-nrrf1` | North River, FL (NRRF1) | 25.340 | -80.911 | 72.0 | 6.94 | 9.86 | +7.19 | 5.99 |
| `erddap-secoora:gov-nps-ever-tcvf1` | Trout Cove, FL (TCVF1) | 25.213 | -80.533 | 72.0 | 6.79 | 12.07 | +0.62 | 23.72 |
| `erddap-secoora:gov-nps-ever-hcef1` | Highway Creek, FL (HCEF1) | 25.254 | -80.444 | 72.0 | 6.74 | 8.15 | +6.57 | 24.25 |
| `erddap-secoora:gov-nps-ever-jbyf1` | Joe Bay, FL (JBYF1) | 25.225 | -80.541 | 72.0 | 6.46 | 12.38 | +2.70 | 17.29 |
| `erddap-secoora:gov-nps-ever-href1` | Harney River, FL (HREF1) | 25.425 | -81.060 | 72.0 | 5.94 | 9.92 | -5.87 | 19.94 |
| `erddap-secoora:gov-nps-ever-tbyf1` | Terrapin Bay, FL (TBYF1) | 25.157 | -80.725 | 72.0 | 5.74 | 9.10 | +5.71 | 32.14 |
| `erddap-secoora:gov-nps-ever-wwef1` | Whitewater East, FL (WWEF1) | 25.232 | -80.938 | 71.0 | 5.59 | 7.60 | -5.46 | 21.36 |
| `usgs:255138081321701` | Faka Union Boundary Near Panther Key Fl | 25.861 | -81.538 | 74.0 | 5.49 | 10.56 | -5.74 | 32.91 |

