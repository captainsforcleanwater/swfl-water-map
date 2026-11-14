"""Checks for the clean and blend steps. Run with: python -m pytest tests"""

from pathlib import Path

import numpy as np
import pytest
from scipy.sparse.csgraph import dijkstra

from collector.common import distance_m
from processing import gridfile
from processing.clean import flatline_bits, spike_bits
from processing.config import proc_config
from processing.pss78 import salinity_from_conductivity, salinity_from_spec_cond


def test_pss78_reference_values():
    # UNESCO (1983) check value: R = 1.888091, T68 = 40 C, P = 10000 dbar gives S = 40.0000
    assert salinity_from_conductivity(1.888091 * 42.914, 40 / 1.00024, 10000) == pytest.approx(40.0, abs=1e-4)
    # Standard seawater: specific conductance 53,087 uS/cm at 25 C is S = 35
    assert salinity_from_spec_cond(53087) == pytest.approx(35.0, abs=0.02)
    # Fresh water stays small and positive (Hill et al. extension)
    assert 0 < salinity_from_spec_cond(275) < 0.2


def test_gridfile_round_trip(tmp_path: Path):
    rng = np.random.default_rng(1)
    base = np.cumsum(rng.integers(-3, 4, size=(5, 20, 30)), axis=2).astype(np.int16) + 500
    conf = rng.integers(0, 101, size=(5, 20, 30)).astype(np.uint8)
    static = rng.integers(0, 256, size=(20, 30)).astype(np.uint8)
    p = tmp_path / "x.bin.gz"
    gridfile.write(p, {"date": "2026-11-14"}, {
        "v": (base, {"encoding": "delta_tx", "scale": 0.05}),
        "c": (conf, {"encoding": "delta_t8"}),
        "s": (static, {"encoding": "none"})})
    head, arr = gridfile.read(p)
    assert head["date"] == "2026-11-14" and head["format"] == "swfl-grid"
    assert np.array_equal(arr["v"], base) and np.array_equal(arr["c"], conf) and np.array_equal(arr["s"], static)


def test_spike_flags_a_single_jump_but_not_a_front():
    cfg = proc_config()["qc"]
    t = np.arange(20) * 900.0
    v = np.full(20, 26.0)
    v[10] = 30.0                                  # one bad reading
    assert spike_bits(t, v, "water_temp_c", cfg)[10] == 16
    step = np.r_[np.full(10, 26.0), np.full(10, 22.0)]   # a real cold front: a step, not a spike
    assert not spike_bits(t, step, "water_temp_c", cfg).any()


def test_flatline_flags_stuck_temperature_but_not_fresh_salinity():
    cfg = proc_config()["qc"]
    t = np.arange(0, 30 * 3600, 900.0)
    stuck = np.full(len(t), 27.3)
    assert (flatline_bits(t, stuck, "water_temp_c", cfg, None) & 64).all()
    fresh = np.full(len(t), 0.2)                  # rounding floor in fresh water, not a stuck sensor
    assert not flatline_bits(t, fresh, "salinity_psu", cfg, None).any()


@pytest.fixture(scope="module")
def graph():
    from processing.waterpaths import WaterGraph
    return WaterGraph.load()


def water_km(graph, a, b):
    na, _ = graph.snap(*a, 1.0)
    nb, _ = graph.snap(*b, 1.0)
    return float(dijkstra(graph.adj, directed=False, indices=[na])[0, nb])


def test_water_path_goes_around_sanibel(graph):
    tarpon_bay = (26.4679, -82.0631)      # SCCF RECON Tarpon Bay, behind Sanibel
    gulf = (26.415, -82.060)              # Gulf of Mexico off Sanibel's beach
    straight = distance_m(*tarpon_bay, *gulf) / 1000
    assert water_km(graph, tarpon_bay, gulf) > 2 * straight


def test_caloosahatchee_connects_to_the_gulf_but_stops_at_s79(graph):
    beautiful_island = (26.6955, -81.8138)
    shell_point_gulf = (26.45, -82.05)
    assert np.isfinite(water_km(graph, beautiful_island, shell_point_gulf))
    assert graph.snap(26.7200, -81.6546, 1.0) is None   # Alva, above the S-79 lock, is not tidal water
