"""Practical salinity from conductivity: PSS-78 (UNESCO 1983) with the Hill et al. (1986)
extension below 2 PSU, as used by USGS to compute salinity from specific conductance.

Specific conductance is conductivity already corrected to 25 C, so salinity comes from
PSS-78 at T = 25 C and sea-level pressure.
"""

from __future__ import annotations

import numpy as np

C35_15_0 = 42.914   # mS/cm, conductivity of standard seawater (S = 35) at 15 C, 0 dbar

_A = (0.0080, -0.1692, 25.3851, 14.0941, -7.0261, 2.7081)
_B = (0.0005, -0.0056, -0.0066, -0.0375, 0.0636, -0.0144)
_C = (0.6766097, 2.00564e-2, 1.104259e-4, -6.9698e-7, 1.0031e-9)
_K = 0.0162


def salinity_from_conductivity(cond_ms_cm, temp_c, pressure_dbar=0.0):
    """Practical salinity from in-situ conductivity (mS/cm), temperature (C, ITS-90) and pressure."""
    c = np.asarray(cond_ms_cm, dtype="f8")
    t = 1.00024 * np.asarray(temp_c, dtype="f8")     # ITS-90 to IPTS-68
    p = np.asarray(pressure_dbar, dtype="f8")
    r = c / C35_15_0
    rt_t = _C[0] + t * (_C[1] + t * (_C[2] + t * (_C[3] + t * _C[4])))
    rp = 1 + p * (2.070e-5 + p * (-6.370e-10 + p * 3.989e-15)) / (
        1 + t * (3.426e-2 + t * 4.464e-4) + (4.215e-1 - 3.107e-3 * t) * r)
    rt = np.clip(r / (rp * rt_t), 0, None)
    sq = np.sqrt(rt)
    ft = (t - 15) / (1 + _K * (t - 15))
    s = sum(a * sq ** i for i, a in enumerate(_A)) + ft * sum(b * sq ** i for i, b in enumerate(_B))
    # Hill et al. (1986) for low salinity
    x = 400 * rt
    y = 100 * rt
    hill = s - _A[0] / (1 + 1.5 * x + x * x) - _B[0] * ft / (1 + np.sqrt(y) + y * np.sqrt(y))
    s = np.where(s < 2, hill, s)
    return np.where(np.isfinite(c) & (c >= 0), np.clip(s, 0, None), np.nan)


def salinity_from_spec_cond(spec_cond_us_cm):
    """Practical salinity from specific conductance at 25 C (microsiemens per cm)."""
    return salinity_from_conductivity(np.asarray(spec_cond_us_cm, dtype="f8") / 1000.0, 25.0)
