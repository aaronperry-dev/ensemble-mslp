"""Derived products: ensemble mean / member-wise minimum, low-pressure centres, filled contour polygons."""
from __future__ import annotations

import json
import math
from pathlib import Path

import contourpy
import numpy as np
from scipy.interpolate import RectBivariateSpline
from scipy.ndimage import gaussian_filter, minimum_filter


# ---------------------------------------------------------------------------------------------------
# ensemble statistics
# ---------------------------------------------------------------------------------------------------
def ensemble_mean(stack: np.ndarray) -> np.ndarray:
    return np.nanmean(stack, axis=0)


def ensemble_min(stack: np.ndarray) -> np.ndarray:
    """Member-wise minimum: at every grid point, the lowest MSLP any member forecasts."""
    return np.nanmin(stack, axis=0)


# ---------------------------------------------------------------------------------------------------
# low-pressure centres (one list per member)
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


# ---------------------------------------------------------------------------------------------------
# value grid for the browser's cursor read-out
# ---------------------------------------------------------------------------------------------------
def sample_grid(lats, lons, field, *, domain: tuple[float, float, float, float], step_deg: float) -> dict:
    """Decimate `field` to ~`step_deg` inside `domain` = (west, east, south, north).

    Values are stored as integer tenths of hPa (row-major, south->north, west->east) so the JSON stays small;
    the viewer interpolates bilinearly.  Missing cells are written as null.
    """
    west, east, south, north = domain
    res = float(abs(lats[1] - lats[0]))
    k = max(1, int(round(step_deg / res)))
    jj = np.nonzero((lats >= south - 1e-6) & (lats <= north + 1e-6))[0][::k]
    ii = np.nonzero((lons >= west - 1e-6) & (lons <= east + 1e-6))[0][::k]
    sub = field[np.ix_(jj, ii)]
    tenths = np.rint(sub * 10.0)
    vals = [None if not np.isfinite(v) else int(v) for v in tenths.ravel()]
    return {"lon0": round(float(lons[ii[0]]), 4), "lat0": round(float(lats[jj[0]]), 4),
            "d": round(res * k, 4), "nx": int(len(ii)), "ny": int(len(jj)), "v": vals}


# ---------------------------------------------------------------------------------------------------
# filled contours -> GeoJSON
# ---------------------------------------------------------------------------------------------------
def upsample(lats, lons, field, target_deg: float):
    """Cubic-spline upsample so that 0.5-deg frames get smooth (not blocky) contour polygons."""
    res = float(abs(lats[1] - lats[0]))
    k = int(round(res / target_deg))
    if k <= 1 or np.isnan(field).any():
        return lats, lons, field
    spline = RectBivariateSpline(lats, lons, field, kx=3, ky=3)
    nlat = np.linspace(lats[0], lats[-1], (len(lats) - 1) * k + 1)
    nlon = np.linspace(lons[0], lons[-1], (len(lons) - 1) * k + 1)
    return nlat, nlon, spline(nlat, nlon).astype(np.float32)


def _ring(points: np.ndarray, nd: int) -> list[list[float]] | None:
    pts = np.round(points, nd)
    keep = np.ones(len(pts), dtype=bool)
    keep[1:] = np.any(pts[1:] != pts[:-1], axis=1)     # drop consecutive duplicates after rounding
    pts = pts[keep]
    if len(pts) < 4:
        return None
    return pts.tolist()


def contour_bands(lats, lons, field, edges: list[float], precision: int = 2) -> list[dict]:
    """Filled contour polygons, one GeoJSON Feature per class.

    Class i covers [edges[i-1], edges[i]); class 0 is open below, the last class is open above.
    """
    z = np.ma.masked_invalid(field)
    gen = contourpy.contour_generator(lons, lats, z, name="serial", fill_type=contourpy.FillType.OuterOffset)
    bounds = [-1.0e4, *edges, 1.0e4]
    feats = []
    for i in range(len(bounds) - 1):
        pts_list, off_list = gen.filled(bounds[i], bounds[i + 1])
        polys = []
        for pts, off in zip(pts_list, off_list):
            rings = [_ring(pts[off[k]:off[k + 1]], precision) for k in range(len(off) - 1)]
            if rings and rings[0] is not None:
                polys.append([r for r in rings if r is not None])  # first ring = outer, rest = holes
        if polys:
            feats.append({
                "type": "Feature",
                "properties": {"i": i, "lo": None if i == 0 else edges[i - 1], "hi": None if i == len(edges) else edges[i]},
                "geometry": {"type": "MultiPolygon", "coordinates": polys},
            })
    return feats


def write_json(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    s = json.dumps(obj, separators=(",", ":"), allow_nan=False)
    path.write_text(s, encoding="utf-8")
    return len(s)
