"""Build the administrative basemap (Natural Earth 1:10m, public domain) clipped to the viewer domain.

Run once (the output is committed):  python -m gefs_pipeline basemap
Layers written to site/data/basemap/:
  land.json    polygons       (neutral land fill)
  lakes.json   polygons       (larger lakes only; Great Lakes etc.)
  coast.json   lines          (coastline)
  admin0.json  lines, t=0 international boundary | t=1 disputed/indefinite/lease | t=2 maritime (treaty/median)
  admin1.json  lines          (states / provinces / first-order divisions)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import requests
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry

log = logging.getLogger("gefs.basemap")
NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson"
EXTRA = 3.0          # degrees of basemap beyond the data domain (the viewer is locked to the domain)
MIN_LAKE_DEG2 = 0.04  # ~400 km2: drops the myriad small Canadian lakes that would clutter the map


def _download(name: str, cache: Path) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    p = cache / f"{name}.geojson"
    if not p.exists():
        log.info("downloading %s", name)
        r = requests.get(NE.format(name), timeout=180)
        r.raise_for_status()
        p.write_bytes(r.content)
    return json.loads(p.read_text())


def _round(coords, nd):
    if isinstance(coords[0], (int, float)):
        return [round(c, nd) for c in coords]
    return [_round(c, nd) for c in coords]


def _clean(g: BaseGeometry, kind: str, clip: BaseGeometry, simplify: float) -> BaseGeometry | None:
    if g is None or g.is_empty:
        return None
    g = g.intersection(clip)
    if g.is_empty:
        return None
    g = g.simplify(simplify, preserve_topology=True)
    parts = [x for x in getattr(g, "geoms", [g]) if x.geom_type.startswith("Polygon" if kind == "poly" else "LineString") and not x.is_empty]
    if not parts:
        return None
    from shapely.geometry import MultiLineString, MultiPolygon
    return MultiPolygon(parts) if kind == "poly" else MultiLineString(parts)


def _write(path: Path, features: list[dict], nd: int = 3) -> None:
    fc = {"type": "FeatureCollection", "features": features}
    s = json.dumps(fc, separators=(",", ":"))
    path.write_text(s)
    log.info("%-12s %3d features %7.1f KB", path.name, len(features), len(s) / 1024)


def build_basemap(cfg, out: Path, cache: Path) -> None:
    d = cfg.domain
    clip = box(d.west - EXTRA, d.south - EXTRA, d.east + EXTRA, d.north + EXTRA)
    out.mkdir(parents=True, exist_ok=True)

    def feats(name, kind, simplify, keep=lambda p, g: True, props=lambda p: {}):
        res = []
        for f in _download(name, cache)["features"]:
            if not f.get("geometry"):
                continue
            g = shape(f["geometry"])
            if not g.intersects(clip) or not keep(f["properties"], g):
                continue
            c = _clean(g, kind, clip, simplify)
            if c is not None:
                geom = mapping(c)
                geom["coordinates"] = _round(geom["coordinates"], 3)
                res.append({"type": "Feature", "properties": props(f["properties"]), "geometry": geom})
        return res

    _write(out / "land.json", feats("ne_10m_land", "poly", 0.02))
    _write(out / "lakes.json", feats("ne_10m_lakes", "poly", 0.01, keep=lambda p, g: g.area >= MIN_LAKE_DEG2))
    _write(out / "coast.json", feats("ne_10m_coastline", "line", 0.008))

    def a0_type(p):
        cls = (p.get("FEATURECLA") or "").lower()
        return 0 if cls.startswith("international") else 1
    a0 = feats("ne_10m_admin_0_boundary_lines_land", "line", 0.004, props=lambda p: {"t": a0_type(p)})
    maritime = feats("ne_10m_admin_0_boundary_lines_maritime_indicator", "line", 0.01,
                     keep=lambda p, g: (p.get("FEATURECLA") or "") in ("Marine Indicator Median", "Marine Indicator Treaty", "Boundary Treaty"),
                     props=lambda p: {"t": 2})
    _write(out / "admin0.json", a0 + maritime)
    _write(out / "admin1.json", feats("ne_10m_admin_1_states_provinces_lines", "line", 0.006,
                                      keep=lambda p, g: (p.get("FEATURECLA") or "") == "Admin-1 boundary"))
    (out / "ATTRIBUTION.txt").write_text(
        "Basemap vectors: Natural Earth (naturalearthdata.com), public domain, 1:10m Cultural + Physical vectors,\n"
        "clipped to the viewer domain and simplified by gefs_pipeline/basemap.py.\n")
