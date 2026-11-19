"""Land / water mask of any lat-lon grid, rasterised from the basemap polygons the viewer already ships
(Natural Earth land, minus the lakes).  Dependency-free (numpy only): an even-odd scanline fill of every polygon ring.

Used by the estimated-gust model: gusts relative to the mean wind are much larger over land (rough, mixed layer) than over water."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np

BASEMAP = Path(__file__).resolve().parent.parent / "site" / "data" / "basemap"


def _rings(path: Path):
    """Yield one list of rings (outer ring first, then holes) per polygon of a GeoJSON FeatureCollection."""
    for ft in json.loads(path.read_text())["features"]:
        g = ft["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]] if g["type"] == "Polygon" else []
        yield from polys


def _fill(polys, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """True where a grid point (lats ascending or descending, lons ascending) lies inside any polygon (even-odd over its rings)."""
    ny, nx = len(lats), len(lons)
    out = np.zeros((ny, nx), bool)
    dlon = float(lons[1] - lons[0])
    lo_lat, hi_lat = float(min(lats[0], lats[-1])), float(max(lats[0], lats[-1]))
    lon0 = float(lons[0])
    for rings in polys:
        poly = np.zeros((ny, nx), bool)
        touched = False
        for ring in rings:
            r = np.asarray(ring, float)
            if len(r) < 3 or r[:, 1].max() < lo_lat or r[:, 1].min() > hi_lat or r[:, 0].max() < lons[0] or r[:, 0].min() > lons[-1]:
                continue
            touched = True
            x0, y0, x1, y1 = r[:, 0], r[:, 1], np.roll(r[:, 0], -1), np.roll(r[:, 1], -1)
            keep = y0 != y1
            x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]
            for j in range(ny):
                lat = lats[j]
                m = ((y0 <= lat) & (y1 > lat)) | ((y1 <= lat) & (y0 > lat))          # edges crossing this latitude (half-open: no double counts)
                if not m.any():
                    continue
                xs = np.sort(x0[m] + (lat - y0[m]) * (x1[m] - x0[m]) / (y1[m] - y0[m]))
                # grid columns between crossing 2k and 2k+1 are inside
                a = np.ceil((xs[0::2][: len(xs) // 2] - lon0) / dlon).astype(int)
                b = np.floor((xs[1::2] - lon0) / dlon).astype(int)
                for s, e in zip(a, b):
                    s, e = max(s, 0), min(e, nx - 1)
                    if e >= s:
                        poly[j, s:e + 1] ^= True                                          # XOR: a hole ring cancels the outer ring
        if touched:
            out |= poly
    return out


@lru_cache(maxsize=8)
def _cached(lat0: float, dlat: float, ny: int, lon0: float, dlon: float, nx: int) -> np.ndarray:
    lats, lons = lat0 + dlat * np.arange(ny), lon0 + dlon * np.arange(nx)
    land = _fill(list(_rings(BASEMAP / "land.json")), lats, lons)
    lakes = BASEMAP / "lakes.json"
    if lakes.exists():
        land &= ~_fill(list(_rings(lakes)), lats, lons)
    land.setflags(write=False)
    return land


def land_mask(lats, lons) -> np.ndarray:
    """(len(lats), len(lons)) bool, True over land.  Grid must be regular; points outside the basemap's extent read as water."""
    lats, lons = np.asarray(lats, float), np.asarray(lons, float)
    return _cached(float(lats[0]), float(lats[1] - lats[0]), len(lats), float(lons[0]), float(lons[1] - lons[0]), len(lons))
