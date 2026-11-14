# Leave-one-out check

Period: 2026-11-11T17:00:00Z to 2026-11-14T17:00:00Z. Generated 2026-11-14T19:00:20Z by `python -m processing.validate`.

Each station is left out in turn, the hour is blended from the others with the settings in
`config/processing.json`, and the blended value at the station is compared with its reading.
Errors are blended minus measured. Temperature in degrees C (F in brackets), salinity in PSU.

| | Temperature | Salinity |
|---|---:|---:|
| Station-hours | 3840 | 3766 |
| Typical error (median absolute) | 0.45 (0.82 F) | 2.90 |
| Mean absolute error | 0.62 (1.12 F) | 3.88 |
| RMS error | 0.85 (1.52 F) | 5.61 |
| 90% of errors below | 1.37 (2.47 F) | 8.22 |
| Worst single hour | 3.57 (6.42 F) | 31.31 |
| Bias | +0.06 | +0.81 |
| Without sensors: typical error | 0.68 (satellite only) | 3.47 (background only) |
| Without sensors: RMS error | 1.37 | 7.03 |

## Temperature: stations with the largest errors

| Station | Name | Lat | Lon | Hours | Typical error (C) | Worst (C) | Bias (C) | Mean reading |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `usgs:251253080320100` | Trout Creek At Mouth Near Key Largo, Fl | 25.215 | -80.534 | 71.0 | 1.97 | 2.61 | -1.50 | 29.37 |
| `usgs:02297460` | Peace River At Harbour Heights Fl | 26.988 | -81.994 | 73.0 | 1.84 | 2.95 | +1.78 | 23.97 |
| `erddap-secoora:gov-nps-ever-lsnf1` | Long Sound, FL (LSNF1) | 25.235 | -80.457 | 71.0 | 1.63 | 2.99 | -1.56 | 29.16 |
| `erddap-secoora:gov-nps-ever-tpef1` | Tarpon Bay East, FL (TPEF1) | 25.410 | -80.964 | 71.0 | 1.48 | 2.08 | -1.29 | 27.72 |
| `erddap-gcoos:SCCF_56` | SCCF 56 - SCCF RECON McIntyre Creek | 26.464 | -82.104 | 73.0 | 1.35 | 2.96 | +1.52 | 25.55 |
| `erddap-secoora:gov-nps-ever-thrf1` | Thursday Point, FL (THRF1) | 25.203 | -80.372 | 71.0 | 1.18 | 2.00 | +1.19 | 26.92 |
| `erddap-secoora:gov-nps-ever-canf1` | Cane Patch, FL (CANF1) | 25.422 | -80.942 | 71.0 | 0.86 | 1.38 | +0.75 | 26.33 |
| `erddap-secoora:gov-nps-ever-mukf1` | Murray Key, FL (MUKF1) | 25.106 | -80.942 | 71.0 | 0.76 | 2.14 | +0.68 | 27.10 |
| `erddap-secoora:gov-nps-ever-wiwf1` | Willy Willy, FL (WIWF1) | 25.587 | -81.044 | 71.0 | 0.74 | 2.74 | +0.82 | 26.26 |
| `erddap-gcoos:SCCF_13` | SCCF 13 - SCCF RECON Shell Point | 26.525 | -82.171 | 73.0 | 0.73 | 2.60 | -0.98 | 26.00 |
| `ndbc:RKQF1` | Lower Henderson Creek, Rookery Bay Reserve, FL | 26.027 | -81.734 | 73.0 | 0.73 | 1.87 | -0.41 | 26.99 |
| `erddap-secoora:gov-nps-ever-href1` | Harney River, FL (HREF1) | 25.425 | -81.060 | 71.0 | 0.70 | 1.20 | +0.59 | 26.50 |

## Salinity: stations with the largest errors

| Station | Name | Lat | Lon | Hours | Typical error (PSU) | Worst (PSU) | Bias (PSU) | Mean reading |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `usgs:02297460` | Peace River At Harbour Heights Fl | 26.988 | -81.994 | 73.0 | 23.00 | 31.31 | +21.09 | 0.13 |
| `erddap-secoora:gov-nps-ever-canf1` | Cane Patch, FL (CANF1) | 25.422 | -80.942 | 71.0 | 11.22 | 14.50 | +11.08 | 0.37 |
| `erddap-secoora:gov-nps-ever-lmrf1` | Lostmans River, FL (LMRF1) | 25.556 | -81.169 | 71.0 | 9.45 | 18.18 | -10.68 | 22.60 |
| `erddap-secoora:gov-nps-ever-wiwf1` | Willy Willy, FL (WIWF1) | 25.587 | -81.044 | 71.0 | 8.96 | 13.88 | +9.58 | 2.12 |
| `erddap-secoora:gov-nps-ever-nrrf1` | North River, FL (NRRF1) | 25.340 | -80.911 | 71.0 | 6.96 | 9.86 | +7.20 | 5.99 |
| `usgs:251253080320100` | Trout Creek At Mouth Near Key Largo, Fl | 25.215 | -80.534 | 71.0 | 6.72 | 12.07 | +0.49 | 23.91 |
| `erddap-secoora:gov-nps-ever-hcef1` | Highway Creek, FL (HCEF1) | 25.254 | -80.444 | 71.0 | 6.70 | 8.15 | +6.57 | 24.24 |
| `erddap-secoora:gov-nps-ever-jbyf1` | Joe Bay, FL (JBYF1) | 25.225 | -80.541 | 71.0 | 6.47 | 12.38 | +2.77 | 17.38 |
| `erddap-secoora:gov-nps-ever-href1` | Harney River, FL (HREF1) | 25.425 | -81.060 | 71.0 | 5.98 | 9.92 | -5.90 | 20.03 |
| `erddap-secoora:gov-nps-ever-tbyf1` | Terrapin Bay, FL (TBYF1) | 25.157 | -80.725 | 71.0 | 5.73 | 9.11 | +5.71 | 32.14 |
| `erddap-secoora:gov-nps-ever-wwef1` | Whitewater East, FL (WWEF1) | 25.232 | -80.938 | 70.0 | 5.60 | 7.60 | -5.46 | 21.35 |
| `usgs:255138081321701` | Faka Union Boundary Near Panther Key Fl | 25.861 | -81.538 | 73.0 | 5.42 | 10.39 | -5.71 | 32.93 |

