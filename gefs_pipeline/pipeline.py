"""Orchestration: resolve cycle -> (prefetching) fetch every family's members -> per-family + blended products
-> static files + manifest.

Views written for each forecast hour (data/<run>/<view>/<product>/fNNN.json):
    views    gefs | ifs | blend        (blend only when both families have that hour)
    products mean (filled contours + member lows) | min (member-wise minimum envelope + its low centres)
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from itertools import combinations
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from . import __version__
from .config import ROOT
from .decode import DecodeError, Grid
from .fetcher import FetchError, Http, NotFound
from .palette import build_scale
from .products import (contour_bands, detect_lows, ensemble_mean, ensemble_min, sample_grid, upsample, write_json)
from .sources import Family

log = logging.getLogger("gefs.pipeline")


def make_http(cfg, **over) -> Http:
    f = cfg.fetch
    ua = f.user_agent
    if os.environ.get("GITHUB_REPOSITORY"):          # identify ourselves properly when running in Actions
        ua = ua.replace("YOUR-USER/gefs-mslp-viewer", os.environ["GITHUB_REPOSITORY"])
    return Http(user_agent=ua, rps=over.get("rps", f.requests_per_second),
                host_rps={h: float(getattr(cfg, sec).requests_per_second) for h, sec in (("ecmwf-forecasts", "ifs"), ("weather.gc.ca", "geps")) if hasattr(cfg, sec)},
                max_retries=f.max_retries,
                backoff_base=f.backoff_base_s, backoff_max=f.backoff_max_s,
                timeout=(f.timeout_connect_s, f.timeout_read_s))


# ---------------------------------------------------------------------------------------------------
# one family, one hour -> fields
# ---------------------------------------------------------------------------------------------------
@dataclass
class FamilyFrame:
    fam: Family
    plan: object
    grids: dict[str, Grid]          # members that arrived (same grid as the reference member)
    names: list[str]                # ... in family order


def _usable(fam: Family, grids: dict[str, Grid], fh: int) -> FamilyFrame | None:
    names = [m for m in fam.members if m in grids]
    if not names:
        return None
    ref = grids[names[0]]
    ok = [m for m in names if grids[m].values.shape == ref.values.shape]
    if len(ok) < len(names):
        log.warning("f%03d %s: dropped %d members with a different grid", fh, fam.key, len(names) - len(ok))
    return FamilyFrame(fam, None, {m: grids[m] for m in ok}, ok)


def _fields(cfg, ff: FamilyFrame) -> SimpleNamespace:
    """Mean, minimum, member lows and minimum-envelope lows for one family on its native grid."""
    d = cfg.domain
    dom = (d.west, d.east, d.south, d.north)
    names = ff.names
    ref = ff.grids[names[0]]
    stack = np.stack([ff.grids[m].values for m in names])
    midx = {m: i for i, m in enumerate(ff.fam.members)}
    to_member = np.array([midx[m] for m in names])

    mean = ensemble_mean(stack)
    mn = ensemble_min(stack)
    owner = to_member[np.argmin(np.where(np.isfinite(stack), stack, np.inf), axis=0)]   # family-member index at each cell

    L = cfg.lows
    lows = []
    for m in names:
        g = ff.grids[m]
        for lon, lat, p in detect_lows(g.lats, g.lons, g.values, threshold_hpa=L.threshold_hpa, radius_deg=L.radius_deg,
                                       smooth_deg=L.smooth_deg, min_sep_deg=L.min_separation_deg, domain=dom):
            lows.append([round(lon, 2), round(lat, 2), round(p, 1), midx[m]])
    return SimpleNamespace(lats=ref.lats, lons=ref.lons, mean=mean, min=mn, owner=owner, lows=lows,
                           n=len(names), res=ref.res, valid=ref.valid_time, source=ff.fam.plan_tag(ff.plan))


def _min_lows(cfg, lats, lons, mn, owner, offset: int = 0):
    """Low centres of a minimum field, each attributed to the member that supplies the minimum there."""
    d = cfg.domain
    LM = cfg.lows_min
    out = []
    for lon, lat, p in detect_lows(lats, lons, mn, threshold_hpa=LM.threshold_hpa, radius_deg=LM.radius_deg,
                                   smooth_deg=LM.smooth_deg, min_sep_deg=LM.min_separation_deg,
                                   domain=(d.west, d.east, d.south, d.north)):
        j = int(np.argmin(np.abs(lats - lat)))
        i = int(np.argmin(np.abs(lons - lon)))
        out.append([round(lon, 2), round(lat, 2), round(p, 1), int(owner[j, i]) + offset])
    return out


def _to_grid(lats, lons, f: SimpleNamespace, field: np.ndarray, nearest: bool = False) -> np.ndarray:
    """Resample `field` (on f's grid) onto (lats, lons).  A no-op when the grids already match (the normal case)."""
    if f.lats.shape == lats.shape and f.lons.shape == lons.shape and np.allclose(f.lats, lats) and np.allclose(f.lons, lons):
        return field
    interp = RegularGridInterpolator((f.lats, f.lons), field.astype(np.float64), method="nearest" if nearest else "linear",
                                     bounds_error=False, fill_value=np.nan)
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    return interp((la, lo)).astype(field.dtype)


# ---------------------------------------------------------------------------------------------------
# writing one view
# ---------------------------------------------------------------------------------------------------
def _emit(cfg, out_dir: Path, run_id: str, view: str, fh: int, scales: dict, meta: dict, lats, lons,
          mean, mn, lows, mlows, per_family: dict | None = None) -> None:
    d = cfg.domain
    dom = (d.west, d.east, d.south, d.north)
    prec = int(cfg.contours.precision)
    tip = float(cfg.tooltip.grid_deg)
    f3 = f"f{fh:03d}"
    for prod, field, lw in (("mean", mean, lows), ("min", mn, mlows)):
        la, lo, z = upsample(lats, lons, field, float(cfg.contours.upsample_to_deg))
        feats = contour_bands(la, lo, z, scales[prod][0], prec)
        grid = sample_grid(lats, lons, field, domain=dom, step_deg=tip)
        if per_family:                                  # blend: also ship each family's own value for the read-out
            grid["fam"] = {k: sample_grid(lats, lons, v[prod], domain=dom, step_deg=tip)["v"] for k, v in per_family.items()}
        write_json(out_dir / run_id / view / prod / f"{f3}.json",
                   {"type": "FeatureCollection", "features": feats, "meta": meta, "lows": lw, "grid": grid})


def blend_sets(cfg, order: list[str]) -> list[list[str]]:
    """Family combinations to blend, largest first, each in `order` (e.g. [gefs,ifs,geps], [gefs,ifs], ...)."""
    if len(order) < 2:
        return []
    if not getattr(cfg.blend, "subsets", True):
        return [list(order)]
    sets = [list(c) for n in range(len(order), 1, -1) for c in combinations(order, n)]
    return sets


def blend_key(parts: list[str]) -> str:
    return "_".join(parts)


def build_frame(cfg, frames: dict[str, FamilyFrame], order: list[str], fh: int, scales: dict, out_dir: Path,
                run_id: str) -> dict:
    """Write every view available for this hour and return the manifest entry."""
    fields = {k: _fields(cfg, frames[k]) for k in order if k in frames}
    avail = {}
    for k, f in fields.items():
        meta = {"fh": fh, "valid": f.valid, "grid_deg": f.res, "members": f.n, "source": f.source, "family": k}
        _emit(cfg, out_dir, run_id, k, fh, scales, meta, f.lats, f.lons, f.mean, f.min, f.lows,
              _min_lows(cfg, f.lats, f.lons, f.min, f.owner), None)
        avail[k] = {"members": f.n, "grid_deg": f.res, "source": f.source}
    valid = next(iter(fields.values())).valid

    # ---- blends: a combination is built only when every family in it has this hour -----------------------
    todo = [p for p in blend_sets(cfg, order) if all(k in fields for k in p)]
    if todo and len({f.valid for f in fields.values()}) > 1:
        log.warning("f%03d: families disagree on the valid time - no blend", fh)
        todo = []
    cache: dict[tuple[str, str], np.ndarray] = {}
    for parts in todo:
        ref = fields[parts[0]]                              # blend on the first (finest, by config order) grid

        def on_ref(k, name, arr, nearest=False):
            key = (parts[0], k, name)
            if key not in cache:
                cache[key] = _to_grid(ref.lats, ref.lons, fields[k], arr, nearest)
            return cache[key]

        wt = {k: (1.0 if cfg.blend.weighting == "equal" else float(fields[k].n)) for k in parts}
        means = {k: on_ref(k, "mean", fields[k].mean) for k in parts}
        mins = {k: on_ref(k, "min", fields[k].min) for k in parts}
        num = sum(np.where(np.isfinite(means[k]), means[k], 0.0) * wt[k] for k in parts)
        den = sum(np.isfinite(means[k]) * wt[k] for k in parts)
        bmean = np.where(den > 0, num / np.maximum(den, 1e-9), np.nan).astype(np.float32)

        off, offsets = 0, {}                                # member index of each family inside THIS blend's member list
        for k in parts:
            offsets[k] = off
            off += len(frames[k].fam.members)
        bmin = mins[parts[0]].copy()
        bowner = on_ref(parts[0], "owner", fields[parts[0]].owner, True).astype(np.int32) + offsets[parts[0]]
        for k in parts[1:]:                                 # the lowest value wins; its member is the owner there
            lower = np.isfinite(mins[k]) & (~np.isfinite(bmin) | (mins[k] < bmin))
            bmin = np.where(lower, mins[k], bmin)
            own = on_ref(k, "owner", fields[k].owner, True).astype(np.int32) + offsets[k]
            bowner = np.where(lower, own, bowner)
        blows = [[lo, la, p, mi + offsets[k]] for k in parts for lo, la, p, mi in fields[k].lows]
        n = sum(fields[k].n for k in parts)
        view = blend_key(parts)
        meta = {"fh": fh, "valid": valid, "grid_deg": ref.res, "members": n, "family": view,
                "source": "+".join(fields[k].source for k in parts)}
        _emit(cfg, out_dir, run_id, view, fh, scales, meta, ref.lats, ref.lons, bmean, bmin, blows,
              _min_lows(cfg, ref.lats, ref.lons, bmin, bowner),
              {k: {"mean": means[k], "min": mins[k]} for k in parts})
        avail[view] = {"members": n, "grid_deg": ref.res, "parts": {k: fields[k].n for k in parts}}
    return {"fh": fh, "valid": valid, "avail": avail}


# ---------------------------------------------------------------------------------------------------
# full build
# ---------------------------------------------------------------------------------------------------
def build(cfg, *, run: datetime, out: Path, cache_dir: Path | None, http: Http, workers: int,
          families: list[Family], fhs: list[int], min_frames: int = 1) -> dict:
    site_src = ROOT / "site"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(site_src, out, dirs_exist_ok=True)               # viewer + basemap
    data_dir = out / "data"
    run_id = f"{run:%Y%m%d}_{run:%H}"
    shutil.rmtree(data_dir / run_id, ignore_errors=True)

    order = [f.key for f in families]
    fam = {f.key: f for f in families}
    sets = blend_sets(cfg, order)

    edges, colors, scale_segments = build_scale(cfg.scale)         # ONE scale shared by every view so they compare directly
    scales = {"mean": (edges, colors), "min": (edges, colors)}
    need = {k: max(1, int(np.ceil(len(fam[k].members) * float(cfg.fetch.min_member_fraction)))) for k in order}
    t0 = time.time()

    def submit(pool, fh):
        jobs = {}
        for k in order:
            f = fam[k]
            if fh > f.max_fh(run):
                continue                                              # e.g. IFS 06/18Z runs stop at 144 h
            plan = f.plan(http, run, fh)
            if plan is None:
                log.warning("f%03d: %s not published - skipped", fh, k)
                continue
            jobs[k] = (plan, {m: pool.submit(f.fetch_grid, http, run, plan, m, fh, cache_dir) for m in f.members})
        return fh, jobs

    frames: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as pool:
        queue = deque()
        todo = deque(fhs)
        # keep one frame of look-ahead: the next frame downloads while the current one is being contoured
        for _ in range(min(2, len(todo))):
            queue.append(submit(pool, todo.popleft()))
        while queue:
            fh, jobs = queue.popleft()
            if todo:
                queue.append(submit(pool, todo.popleft()))
            ready: dict[str, FamilyFrame] = {}
            for k, (plan, futs) in jobs.items():
                grids: dict[str, Grid] = {}
                for m, fut in futs.items():
                    try:
                        grids[m] = fut.result()
                    except (NotFound, FetchError, DecodeError, KeyError) as e:
                        log.warning("f%03d %s %s: %s", fh, k, m, repr(e) if not str(e) else e)
                ff = _usable(fam[k], grids, fh)
                if ff is None or len(ff.names) < need[k]:
                    log.error("f%03d %s: only %d/%d members (need %d) - family skipped for this hour",
                              fh, k, 0 if ff is None else len(ff.names), len(fam[k].members), need[k])
                    continue
                ff.plan = plan
                ready[k] = ff
            if not ready:
                continue
            frames.append(build_frame(cfg, ready, order, fh, scales, data_dir, run_id))
            log.info("f%03d done  %s   [%d/%d frames, %.0fs, %d req, %.0f MB]", fh,
                     "  ".join(f"{k}:{v['members']}" for k, v in frames[-1]["avail"].items()),
                     len(frames), len(fhs), time.time() - t0, http.stats.requests, http.stats.bytes / 1e6)

    if len(frames) < min_frames:
        raise RuntimeError(f"only {len(frames)} frame(s) built (need >= {min_frames})")
    frames.sort(key=lambda f: f["fh"])

    fam_meta = {k: {"label": fam[k].label, "name": fam[k].name, "org": fam[k].org, "note": fam[k].note,
                    "members": list(fam[k].members),
                    "frames": sum(1 for f in frames if k in f["avail"])} for k in order}
    views = list(order)
    for parts in sets:
        key = blend_key(parts)
        n_frames = sum(1 for f in frames if key in f["avail"])
        if not n_frames:
            continue
        fam_meta[key] = {"label": "Blend", "name": " + ".join(fam[k].label for k in parts) + " blend", "org": "",
                         "note": ("Equal-weight mean of the family means (each system counts once)"
                                  if cfg.blend.weighting == "equal"
                                  else "Mean of all members pooled (each member counts once)"),
                         "members": [m for k in parts for m in fam[k].members], "parts": list(parts),
                         "weighting": cfg.blend.weighting, "frames": n_frames}
        views.append(key)
    init = run.strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schema": 2,
        "generator": f"gefs-mslp-viewer {__version__}",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run": {"id": run_id, "init": init, "cycle": f"{run:%H}", "date": f"{run:%Y%m%d}"},
        "domain": {"west": cfg.domain.west, "east": cfg.domain.east, "south": cfg.domain.south, "north": cfg.domain.north,
                   "initial": list(cfg.domain.initial_view)},
        "families": fam_meta,
        "views": views,
        "lows_threshold_hpa": cfg.lows.threshold_hpa,
        "products": {
            k: {"title": getattr(cfg.products, k).title, "short": getattr(cfg.products, k).short,
                "has_lows": bool(getattr(cfg.products, k).has_lows), "units": "hPa",
                "edges": edges, "colors": colors}
            for k in ("mean", "min")
        },
        "scale": {"segments": scale_segments},
        "frames": frames,
    }
    write_json(data_dir / "manifest.json", manifest)
    # keep only the current run in the published site
    for p in data_dir.iterdir():
        if p.is_dir() and p.name not in (run_id, "basemap"):
            shutil.rmtree(p, ignore_errors=True)
    log.info("built %d frames in %.0fs  (%d HTTP requests, %.0f MB transferred, %d retries)",
             len(frames), time.time() - t0, http.stats.requests, http.stats.bytes / 1e6, http.stats.retries)
    return manifest
