"""Estimated 10 m wind gust for systems that do not publish one (GEPS, AIGEFS): gust = wind speed x a gust factor.

The factor depends on the wind speed and on the surface: over land (rough, deeply mixed boundary layer) gusts are 1.6-2.0x the
mean wind and the ratio falls as the wind strengthens; over water gusts are only ~1.25-1.3x.  Both curves were fitted to what the
two systems that DO publish gusts forecast (GEFS and the ECMWF IFS ensemble, member by member, see tools/calibrate_gust.py) and are
stored in config.toml [wind.estimate].  The estimate is applied to every member BEFORE the ensemble statistics, so the highest
member and the exceedance probabilities stay meaningful.  It is a statistical estimate, not a model forecast, and is labelled so
everywhere in the viewer."""
from __future__ import annotations

import numpy as np

from .decode import Grid
from .landmask import land_mask


def gust_factor(speed_mph: np.ndarray, land: np.ndarray, est) -> np.ndarray:
    """Gust / wind ratio for each cell: piecewise-linear in the wind speed, one curve over land and one over water."""
    f_land = np.interp(speed_mph, est.land_speed, est.land_factor)
    f_sea = np.interp(speed_mph, est.sea_speed, est.sea_factor)
    return np.where(land, f_land, f_sea).astype("f4")


def estimate_gust(wind: Grid, est) -> Grid:
    """Estimated gust (mph) on the grid of a member's 10 m wind speed (mph).  Never below the wind speed itself."""
    land = land_mask(wind.lats, wind.lons)
    v = np.asarray(wind.values, "f4")
    g = np.maximum(v * gust_factor(v, land, est), v)
    return Grid(wind.lats, wind.lons, g.astype("f4"), wind.valid_time, wind.step_hours, "gust_est")
