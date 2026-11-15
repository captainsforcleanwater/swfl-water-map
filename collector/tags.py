"""Station setting tag: coastal (tidal water) or inland (fresh, not tidal).

The salinity and temperature grids use coastal stations only. Tags come from
config/station_tags.json when a station is listed there, otherwise from the rule below.
Kept in the collector so the inventory files show the tag; the processing step uses the
same function.
"""

from __future__ import annotations

import json
from functools import lru_cache

from .common import ROOT

TAGS_PATH = ROOT / "config" / "station_tags.json"

# USGS site type codes that mean fresh, non-tidal water.
INLAND_USGS_TYPES = {
    "WE": "USGS site type wetland",
    "ST-CA": "USGS site type canal",
    "ST-DCH": "USGS site type ditch",
    "LK": "USGS site type lake",
    "GW": "USGS site type groundwater well",
    "SP": "USGS site type spring",
}


@lru_cache(maxsize=1)
def overrides_file() -> dict:
    """The whole of config/station_tags.json (setting tags and QC holds)."""
    if not TAGS_PATH.exists():
        return {}
    return json.loads(TAGS_PATH.read_text())


def overrides() -> dict:
    return overrides_file().get("setting", {})


def coordinates(uid: str) -> dict | None:
    """Hand-corrected position for a station whose source metadata is wrong, or None.
    {"lat": ..., "lon": ..., "source": ..., "reason": ...} from config/station_tags.json."""
    return overrides_file().get("coordinates", {}).get(uid)


def corrected_note(c: dict) -> str:
    return f"coordinates corrected in config/station_tags.json ({c.get('source', 'by hand')})"


def setting(uid: str, extra: dict | None) -> tuple[str, str]:
    """(setting, reason) for a station. setting is 'coastal' or 'inland'."""
    o = overrides().get(uid)
    if o:
        return o["setting"], o.get("reason", "set in config/station_tags.json")
    site_type = (extra or {}).get("site_type", "")
    for code, why in INLAND_USGS_TYPES.items():
        if site_type == code or site_type.startswith(code + "-"):
            return "inland", why
    return "coastal", ""
