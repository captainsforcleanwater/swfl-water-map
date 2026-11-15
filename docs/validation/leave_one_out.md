# Leave-one-out check

Period: 2026-11-11T17:00:00Z to 2026-11-15T01:00:00Z. Generated 2026-11-15T02:32:55Z by `python -m processing.validate`.

Each station is left out in turn, the hour is blended from the others with the settings in
`config/processing.json`, and the blended value at the station is compared with its reading.
Errors are blended minus measured. Temperature in degrees C (F in brackets), salinity in PSU.

| | Temperature | Salinity |
|---|---:|---:|
| Station-hours | 4871 | 4142 |
| Typical error (median absolute) | 0.48 (0.86 F) | 2.96 |
| Mean absolute error | 0.63 (1.13 F) | 3.92 |
| RMS error | 0.85 (1.52 F) | 5.65 |
| 90% of errors below | 1.36 (2.44 F) | 8.39 |
| Worst single hour | 3.45 (6.22 F) | 31.07 |
| Bias | +0.06 | +0.75 |
| Without sensors: typical error | 0.79 (satellite only) | 3.71 (background only) |
| Without sensors: RMS error | 1.66 | 7.09 |

## Temperature: stations with the largest errors

| Station | Name | Lat | Lon | Hours | Typical error (C) | Worst (C) | Bias (C) | Mean reading |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `erddap-secoora:gov-nps-ever-tcvf1` | Trout Cove, FL (TCVF1) | 25.213 | -80.533 | 78.0 | 1.84 | 2.59 | -1.40 | 29.02 |
| `erddap-secoora:gov-nps-ever-lsnf1` | Long Sound, FL (LSNF1) | 25.235 | -80.457 | 78.0 | 1.64 | 3.08 | -1.60 | 28.99 |
| `erddap-secoora:gov-nps-ever-tpef1` | Tarpon Bay East, FL (TPEF1) | 25.410 | -80.964 | 78.0 | 1.38 | 2.09 | -1.20 | 27.44 |
| `erddap-secoora:gov-nps-ever-thrf1` | Thursday Point, FL (THRF1) | 25.203 | -80.372 | 78.0 | 1.19 | 2.05 | +1.16 | 26.73 |
| `usgs:02297460` | Peace River At Harbour Heights Fl | 26.988 | -81.994 | 81.0 | 1.12 | 2.41 | +1.12 | 23.81 |
| `erddap-gcoos:SCCF_56` | SCCF 56 - SCCF RECON McIntyre Creek | 26.464 | -82.104 | 80.0 | 1.06 | 2.90 | +1.27 | 25.27 |
| `usgs:255534081324000` | Pumpkin River Near Goodland, Fl | 25.926 | -81.544 | 80.0 | 0.98 | 3.41 | +0.55 | 25.81 |
| `usgs:251253080320100` | Trout Creek At Mouth Near Key Largo, Fl | 25.215 | -80.534 | 81.0 | 0.90 | 1.92 | +0.12 | 27.51 |
| `erddap-secoora:gov-nps-ever-canf1` | Cane Patch, FL (CANF1) | 25.422 | -80.942 | 78.0 | 0.85 | 1.38 | +0.66 | 26.18 |
| `usgs:255654081350200` | Blackwater River Near Goodland, Fl | 25.952 | -81.584 | 81.0 | 0.83 | 2.16 | +0.54 | 25.63 |
| `erddap-gcoos:SCCF_11` | SCCF 11 - SCCF RECON Tarpon Bay | 26.468 | -82.063 | 80.0 | 0.82 | 1.65 | -0.79 | 25.63 |
| `erddap-gcoos:SCCF_13` | SCCF 13 - SCCF RECON Shell Point | 26.523 | -82.008 | 80.0 | 0.81 | 1.15 | +0.59 | 25.82 |

## Salinity: stations with the largest errors

| Station | Name | Lat | Lon | Hours | Typical error (PSU) | Worst (PSU) | Bias (PSU) | Mean reading |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `usgs:02297460` | Peace River At Harbour Heights Fl | 26.988 | -81.994 | 81.0 | 23.13 | 31.07 | +21.35 | 0.14 |
| `erddap-secoora:gov-nps-ever-canf1` | Cane Patch, FL (CANF1) | 25.422 | -80.942 | 78.0 | 10.86 | 14.50 | +10.67 | 0.36 |
| `erddap-secoora:gov-nps-ever-lmrf1` | Lostmans River, FL (LMRF1) | 25.556 | -81.169 | 78.0 | 9.29 | 18.18 | -10.38 | 22.15 |
| `erddap-secoora:gov-nps-ever-wiwf1` | Willy Willy, FL (WIWF1) | 25.587 | -81.044 | 78.0 | 8.71 | 13.88 | +9.41 | 1.99 |
| `erddap-secoora:gov-nps-ever-nrrf1` | North River, FL (NRRF1) | 25.340 | -80.911 | 78.0 | 7.16 | 9.86 | +7.29 | 5.81 |
| `erddap-secoora:gov-nps-ever-tcvf1` | Trout Cove, FL (TCVF1) | 25.213 | -80.533 | 78.0 | 6.95 | 12.07 | +1.34 | 22.65 |
| `erddap-secoora:gov-nps-ever-hcef1` | Highway Creek, FL (HCEF1) | 25.254 | -80.444 | 78.0 | 6.80 | 17.52 | +6.91 | 23.96 |
| `erddap-secoora:gov-nps-ever-jbyf1` | Joe Bay, FL (JBYF1) | 25.225 | -80.541 | 78.0 | 6.30 | 12.38 | +2.27 | 16.78 |
| `erddap-secoora:gov-nps-ever-href1` | Harney River, FL (HREF1) | 25.425 | -81.060 | 78.0 | 5.82 | 9.92 | -5.62 | 19.21 |
| `usgs:255138081321701` | Faka Union Boundary Near Panther Key Fl | 25.861 | -81.538 | 81.0 | 5.82 | 11.83 | -6.18 | 32.94 |
| `erddap-secoora:gov-nps-ever-tbyf1` | Terrapin Bay, FL (TBYF1) | 25.157 | -80.725 | 78.0 | 5.79 | 9.10 | +5.91 | 32.10 |
| `erddap-secoora:gov-nps-ever-wwef1` | Whitewater East, FL (WWEF1) | 25.232 | -80.938 | 77.0 | 5.66 | 7.60 | -5.53 | 21.39 |

