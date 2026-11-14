"""Shared helpers: config, HTTP session, area test, reading records."""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "area.json"
DATA = ROOT / "data"

log = logging.getLogger("collector")

USER_AGENT = "swfl-water-map-collector/1.0 (+https://github.com/captainsforcleanwater/swfl-water-map)"

# Variable names used in the stored readings. Units are fixed per variable.
TEMP = "water_temp_c"            # degrees Celsius (the map converts to F for display)
SAL = "salinity_psu"             # practical salinity (PSU, treated as equal to ppt)
SPCOND = "spec_cond_us_cm"       # specific conductance at 25 C, microsiemens per cm


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    """Parse an ISO-ish timestamp to an aware UTC datetime."""
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = USER_AGENT
    return s


# ---------------------------------------------------------------- area

class Area:
    def __init__(self, cfg: dict):
        b = cfg["bbox"]
        self.lat_min, self.lat_max = b["lat_min"], b["lat_max"]
        self.lon_min, self.lon_max = b["lon_min"], b["lon_max"]
        self.polygon = [tuple(p) for p in cfg["polygon"]]
        self.edge_anchors = set(cfg.get("edge_anchors", []))

    @property
    def bbox_str(self) -> str:
        """west,south,east,north"""
        return f"{self.lon_min},{self.lat_min},{self.lon_max},{self.lat_max}"

    def in_bbox(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max

    def in_polygon(self, lat: float, lon: float) -> bool:
        inside = False
        pts = self.polygon
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > lat) != (yj > lat):
                x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
                if lon < x_cross:
                    inside = not inside
            j = i
        return inside


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- records

@dataclass
class Station:
    uid: str                  # "<provider>:<id>", unique across sources
    provider: str             # who operates the sensor, e.g. "NOAA CO-OPS"
    system: str               # where we pull it from, e.g. "coops", "usgs", "erddap-secoora"
    station_id: str
    name: str
    lat: float
    lon: float
    variables: list = field(default_factory=list)   # subset of TEMP, SAL, SPCOND
    depths: list = field(default_factory=list)      # depth tags or metres, as discovered
    last_obs: str = ""        # ISO time of newest observation the source reports
    url: str = ""
    notes: str = ""
    extra: dict = field(default_factory=dict)       # source-specific fetch details
    # filled in later
    in_area: bool = False
    edge_anchor: bool = False
    collect: bool = False
    active: bool = False
    possible_duplicate_of: str = ""
    cadence_min: float | None = None


@dataclass
class Reading:
    time: datetime
    station_uid: str
    variable: str
    value: float
    depth_m: float | None = None
    depth_tag: str = "surface"   # surface, mid, bottom
    sensor: str = ""             # tells apart two sensors of the same kind at one station
    qc: str = ""


def depth_tag_from_text(text: str | None) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("bottom", "bttm", "lower", "btm")):
        return "bottom"
    if "mid" in t:
        return "mid"
    return "surface"


def depth_tag_from_m(depth: float | None, all_depths: list[float]) -> str:
    """Tag a numeric depth (metres below surface) relative to the station's other depths."""
    if depth is None or not all_depths:
        return "surface"
    ds = sorted(set(round(d, 1) for d in all_depths if d is not None))
    if len(ds) <= 1:
        return "surface" if depth <= 5 else "mid"
    d = round(depth, 1)
    if d == ds[0] and d <= 5:
        return "surface"
    if d == ds[-1]:
        return "bottom"
    return "mid"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def hours_ago(h: float) -> datetime:
    return utcnow() - timedelta(hours=h)
