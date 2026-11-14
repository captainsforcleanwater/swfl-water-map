"""Paths and settings shared by the clean and blend steps."""

from __future__ import annotations

import json
from functools import lru_cache

from collector.common import DATA, ROOT, load_config

PROC_CONFIG = ROOT / "config" / "processing.json"
GRID_DIR = DATA / "grid"
CLEAN_DIR = DATA / "clean"
QC_DIR = DATA / "qc"
BLEND_DIR = DATA / "blend"
CACHE = ROOT / "build" / "cache"


@lru_cache(maxsize=1)
def proc_config() -> dict:
    return json.loads(PROC_CONFIG.read_text())


@lru_cache(maxsize=1)
def area_config() -> dict:
    return load_config()
