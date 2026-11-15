"""Look for station coordinate errors.

    python analysis/station_coordinates.py GNIS_FILE OUT_MD [NHD_DIR]

GNIS_FILE is the USGS Geographic Names file for Florida (DomesticNames_FL.txt from
https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/DomesticNames/DomesticNames_FL_Text.zip).
Coordinate overrides in config/station_tags.json are applied first. Three checks:

1. Stations that share a latitude or a longitude exactly with another station more than 300 m away
   (a copy-paste error in the source metadata, like SCCF Shell Point carrying Redfish Pass's longitude).
2. Stations far from mapped tidal water (the 100 m mask).
3. Stations far from the place they are named for: the water body in the station name (the part
   before "near" or "at") is looked up in GNIS, and the distance to the nearest feature with that
   name is reported. Rivers, creeks and canals are measured to their mapped line in the USGS
   National Hydrography Dataset flowlines (NHD_DIR/<HU4>/Shape/NHDFlowline.shp), because GNIS
   only holds their mouth and source.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector.common import distance_m  # noqa: E402
from processing.stations import load_raw_stations  # noqa: E402

LIMIT_KM = {"Bay": 6, "Cape": 4, "Island": 4, "Bar": 4, "Channel": 6, "Gut": 4, "Lake": 4,
            "Stream": 2, "Canal": 2, "Locale": 4, "Populated Place": 8, "Basin": 8}
SW_COUNTIES = {"Lee", "Collier", "Monroe", "Charlotte", "Sarasota", "Miami-Dade", "Hendry", "Glades", "DeSoto"}
DROP = re.compile(r"\b(fl|fla|florida|near|nr|at|abv|blw|below|above|the|reserve|rookery bay|sccf|recon|"
                  r"buoy|wfs|wetland|usf|comps)\b", re.I)


DIRECTION = ("East", "West", "North", "South", "Middle", "Upper", "Lower")


def place_names(name: str) -> list[str]:
    """Candidate names for the water body a station is named for, most specific first. Only the
    part before "near", "at" and the like: "Near Homestead" names a town, not the water."""
    n = re.sub(r"\([^)]*\)", "", name)                     # (GBTF1), (NBDC: 42023)
    n = re.sub(r"^SCCF \d+ - SCCF RECON ", "", n)
    n = re.sub(r"^[A-Z0-9]{5} - ", "", n)                  # LONF1 - Long Key
    n = re.sub(r",?\s*(FL|Fl|Fla|Florida)\.?\s*$", "", n).strip()
    parts = re.split(r"\s+(?:At|Near|Nr|Abv|Blw|Upstream Of|In)\s+|,", n, flags=re.I)[:1]
    out = []
    for p in parts:
        p = re.sub(r"\b(Mouth|Boundary|Boat Basin|Upstream|Upper|Lower|East|West|North|South|Middle|2E|1E|5C|Dock|Bridge|Marina|Field Station)\b",
                   lambda m: m.group(0) if m.group(0) in ("East", "West", "North", "South", "Middle", "Upper", "Lower") else "", p)
        p = re.sub(r"\s+", " ", p).strip(" -")
        if len(p) >= 4 and not re.fullmatch(r"[\d\s.\-]+", p):
            out.append(p)
            words = p.split()
            if words[0] in DIRECTION and len(words) > 1:
                out.append(" ".join(words[1:]))
            if words[-1] in DIRECTION and len(words) > 1:
                out.append(" ".join(words[:-1]))
    # GNIS spelling variants: Key/Keys, and a bare name may be "<name> Bay" or "<name> Creek"
    more = []
    for p in out:
        more += [p + "s", p[:-1] if p.endswith("s") else p, p + " Bay", p + " Creek"]
    return out + [m for m in more if m not in out]


def flowline_points(nhd_dir: Path | None, names: set[str]) -> dict[str, list[tuple[float, float]]]:
    """Vertices of NHD flowlines whose GNIS name is in `names` (lower case)."""
    out: dict[str, list] = defaultdict(list)
    if nhd_dir is None:
        return out
    import shapefile
    for shp in sorted(nhd_dir.glob("*/Shape/NHDFlowline.shp")):
        r = shapefile.Reader(str(shp))
        fields = [f[0].lower() for f in r.fields[1:]]
        gi = fields.index("gnis_name")
        for sr in r.iterShapeRecords():
            nm = (sr.record[gi] or "").lower()
            if nm in names:
                out[nm] += [(y, x) for x, y in sr.shape.points[::3]]
    return out


def main(gnis_file: str, out_md: str, nhd_dir: str | None = None) -> None:
    g = pd.read_csv(gnis_file, sep="|", dtype=str, encoding="utf-8-sig")
    g = g[g["county_name"].isin(SW_COUNTIES)].copy()
    for c in ("prim_lat_dec", "prim_long_dec", "source_lat_dec", "source_long_dec"):
        g[c] = pd.to_numeric(g[c], errors="coerce")
    g["key"] = g["feature_name"].str.lower().str.replace(r"[^a-z0-9 ]", "", regex=True).str.strip()
    by_name = defaultdict(list)
    for r in g.itertuples():
        by_name[r.key].append(r)

    st = [s for s in load_raw_stations() if s.get("collect")]
    wanted = {re.sub(r"[^a-z0-9 ]", "", pn.lower()).strip() for s in st for pn in place_names(s["name"])}
    lines_by_name = flowline_points(Path(nhd_dir) if nhd_dir else None, wanted)
    rows = []
    # 1. exact shared latitude or longitude
    shared = []
    for i, a in enumerate(st):
        for b in st[i + 1:]:
            far = distance_m(a["lat"], a["lon"], b["lat"], b["lon"]) > 300
            if far and (round(a["lat"], 5) == round(b["lat"], 5) or round(a["lon"], 5) == round(b["lon"], 5)):
                which = "latitude" if round(a["lat"], 5) == round(b["lat"], 5) else "longitude"
                shared.append((a, b, which))
    # 2 and 3
    from processing.waterpaths import WaterGraph
    graph = WaterGraph.load()
    for s in st:
        sn = graph.snap(s["lat"], s["lon"], 10.0)
        off_km = None if sn is None else sn[1]
        best = None
        for pn in place_names(s["name"]):
            key = re.sub(r"[^a-z0-9 ]", "", pn.lower()).strip()
            for cand in by_name.get(key, []):
                pts = [(cand.prim_lat_dec, cand.prim_long_dec)]
                if cand.feature_class in ("Stream", "Canal"):
                    if pd.notna(cand.source_lat_dec):
                        pts.append((cand.source_lat_dec, cand.source_long_dec))
                    pts += lines_by_name.get(key, [])
                d = min(distance_m(s["lat"], s["lon"], la, lo) for la, lo in pts if pd.notna(la)) / 1000
                if best is None or d < best[0]:
                    best = (d, pn, cand.feature_name, cand.feature_class, cand.prim_lat_dec, cand.prim_long_dec)
            if best:
                break
        rows.append({"station": s["uid"], "name": s["name"], "lat": s["lat"], "lon": s["lon"],
                     "off_water_km": "none within 10 km" if off_km is None else round(off_km, 2),
                     "gnis_name": best[2] if best else "", "gnis_class": best[3] if best else "",
                     "gnis_km": round(best[0], 1) if best else None,
                     "gnis_lat": best[4] if best else None, "gnis_lon": best[5] if best else None})
    df = pd.DataFrame(rows)
    far_name = df[df["gnis_km"].notna() & (df["gnis_km"] > df["gnis_class"].map(LIMIT_KM).fillna(6))]
    off = df[df["off_water_km"].map(lambda v: isinstance(v, str) or v > 0.5)]
    lines = ["# Station coordinate audit", "",
             "Generated by `python analysis/station_coordinates.py`. Overrides in `config/station_tags.json` applied.", "",
             "## Stations sharing a latitude or longitude exactly with a station more than 300 m away", ""]
    lines += [f"- `{a['uid']}` ({a['name']}) and `{b['uid']}` ({b['name']}): same {w}" for a, b, w in shared] or ["None."]
    lines += ["", "## Stations far from the place they are named for", "",
              "| Station | Name | Lat | Lon | Named place in GNIS | Class | Distance (km) | GNIS lat, lon |",
              "|---|---|---:|---:|---|---|---:|---|"]
    for r in far_name.sort_values("gnis_km", ascending=False).itertuples():
        lines.append(f"| `{r.station}` | {r.name} | {r.lat:.4f} | {r.lon:.4f} | {r.gnis_name} | {r.gnis_class} | "
                     f"{r.gnis_km} | {r.gnis_lat:.4f}, {r.gnis_lon:.4f} |")
    if far_name.empty:
        lines.append("| none | | | | | | | |")
    lines += ["", "## Stations more than 500 m from mapped tidal water", "",
              "| Station | Name | Lat | Lon | Distance to tidal water (km) |", "|---|---|---:|---:|---:|"]
    for r in off.itertuples():
        lines.append(f"| `{r.station}` | {r.name} | {r.lat:.4f} | {r.lon:.4f} | {r.off_water_km} |")
    unmatched = df[df["gnis_km"].isna()]
    lines += ["", f"Names with no GNIS match (not checked): {len(unmatched)} of {len(df)}: "
              + ", ".join(sorted(unmatched["name"].str.slice(0, 40))), ""]
    Path(out_md).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    df.to_csv(Path(out_md).with_suffix(".csv"), index=False)


if __name__ == "__main__":
    main(*sys.argv[1:4])
