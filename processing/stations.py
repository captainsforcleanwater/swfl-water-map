"""Physical stations: one entry per sensor location, even when two services carry it.

Stations flagged `possible_duplicate_of` in the inventory, and stations at the same spot
(within SAME_SPOT_M) that measure the same thing, are grouped. The group takes the ID of its
highest-priority member (the order in collector/inventory.py), and each hourly value comes from
one member only: the highest-priority member that has a good reading for that hour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from collector.common import DATA, distance_m
from collector.inventory import SYSTEM_PRIORITY
from collector.tags import setting

SAME_SPOT_M = 50


@dataclass
class PhysStation:
    id: str
    name: str
    provider: str
    lat: float
    lon: float
    members: list[str]
    setting: str
    setting_reason: str
    cadence_min: dict = field(default_factory=dict)   # member uid -> cadence


def load_raw_stations() -> list[dict]:
    return json.loads((DATA / "stations.json").read_text()).get("stations", [])


def _priority(st: dict) -> tuple:
    pri = {s: i for i, s in enumerate(SYSTEM_PRIORITY)}
    return (pri.get(st["system"], 99), st["uid"])


def physical_stations(raw: list[dict] | None = None) -> tuple[list[PhysStation], dict[str, str]]:
    """Group collected stations. Returns the groups and a member uid -> group id map."""
    raw = [s for s in (raw if raw is not None else load_raw_stations()) if s.get("collect")]
    by_uid = {s["uid"]: s for s in raw}
    parent = {u: u for u in by_uid}

    def find(u):
        while parent[u] != u:
            parent[u] = parent[parent[u]]
            u = parent[u]
        return u

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for s in raw:
        d = s.get("possible_duplicate_of")
        if d and d in by_uid:
            union(d, s["uid"])
    for i, a in enumerate(raw):
        for b in raw[i + 1:]:
            if (set(a["variables"]) & set(b["variables"])
                    and distance_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= SAME_SPOT_M):
                union(a["uid"], b["uid"])
    groups: dict[str, list[dict]] = {}
    for s in raw:
        groups.setdefault(find(s["uid"]), []).append(s)
    out, member_of = [], {}
    for members in groups.values():
        members.sort(key=_priority)
        p = members[0]
        tags = [setting(m["uid"], m.get("extra")) for m in members]
        inland = [t for t in tags if t[0] == "inland"]
        tag = inland[0] if inland else tags[0]
        ps = PhysStation(id=p["uid"], name=p["name"], provider=p["provider"], lat=p["lat"], lon=p["lon"],
                         members=[m["uid"] for m in members], setting=tag[0], setting_reason=tag[1],
                         cadence_min={m["uid"]: m.get("cadence_min") for m in members})
        out.append(ps)
        for m in members:
            member_of[m["uid"]] = ps.id
    out.sort(key=lambda s: -s.lat)
    return out, member_of
