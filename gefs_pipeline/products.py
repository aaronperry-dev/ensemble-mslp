"""Derived products: ensemble mean / member-wise minimum and per-member low-pressure centers."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter, minimum_filter


# ---------------------------------------------------------------------------------------------------
# ensemble statistics
# ---------------------------------------------------------------------------------------------------
def ensemble_mean(stack: np.ndarray) -> np.ndarray:
    return np.nanmean(stack, axis=0)


def ensemble_min(stack: np.ndarray) -> np.ndarray:
    """Member-wise minimum: at every grid point, the lowest MSLP any member forecasts."""
    return np.nanmin(stack, axis=0)


def ensemble_max(stack: np.ndarray) -> np.ndarray:
    """Member-wise maximum (winds, gusts): at every grid point, the highest value any member forecasts."""
    return np.nanmax(stack, axis=0)


# ---------------------------------------------------------------------------------------------------
# low-pressure centers (one list per member)
# ---------------------------------------------------------------------------------------------------
def detect_lows(lats, lons, field, *, threshold_hpa: float, radius_deg: float, smooth_deg: float,
                min_sep_deg: float, domain: tuple[float, float, float, float]) -> list[tuple[float, float, float]]:
    """Local pressure minima below `threshold_hpa`.

    A point qualifies when its (lightly smoothed) value is the minimum within `radius_deg`.  Smoothing is
    used only to find the location; the reported pressure is the raw minimum in the 3x3 cells around it.
    Border cells are skipped (cannot be proven to be minima); the caller pads the domain to compensate.
    Returns [(lon, lat, hPa), ...] restricted to `domain` = (west, east, south, north).
    """
    res = float(abs(lats[1] - lats[0]))
    sig = max(smooth_deg / res, 0.0)
    sm = gaussian_filter(field, sigma=sig, mode="nearest") if sig > 0 else field
    size = 2 * int(round(radius_deg / res)) + 1
    is_min = sm == minimum_filter(sm, size=size, mode="nearest")
    is_min[[0, -1], :] = False
    is_min[:, [0, -1]] = False
    west, east, south, north = domain
    cands = []
    for j, i in zip(*np.nonzero(is_min)):
        j0, j1, i0, i1 = max(j - 1, 0), j + 2, max(i - 1, 0), i + 2
        win = field[j0:j1, i0:i1]
        jj, ii = np.unravel_index(int(np.nanargmin(win)), win.shape)
        p = float(win[jj, ii])
        lat, lon = float(lats[j0 + jj]), float(lons[i0 + ii])
        if p < threshold_hpa and west <= lon <= east and south <= lat <= north:
            cands.append((p, lon, lat))
    cands.sort()  # deepest first -> greedy de-duplication keeps the deeper of two close minima
    kept: list[tuple[float, float, float]] = []
    for p, lon, lat in cands:
        too_close = any(
            math.hypot((lon - klon) * math.cos(math.radians((lat + klat) / 2)), lat - klat) < min_sep_deg
            for klon, klat, _kp in kept
        )
        if not too_close:
            kept.append((lon, lat, p))
    return kept


def write_json(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    s = json.dumps(obj, separators=(",", ":"), allow_nan=False)
    path.write_text(s, encoding="utf-8")
    return len(s)
