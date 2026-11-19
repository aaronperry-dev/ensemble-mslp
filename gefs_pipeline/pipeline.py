"""Orchestration: resolve cycle -> (prefetching) fetch every family's members -> per-system grids -> static files + manifest.

Per forecast hour and system two gzip'd binary grids are written (see gridio.py):
    data/<run>/<system>/mean/fNNN.bin.gz   ensemble mean + member low centres
    data/<run>/<system>/min/fNNN.bin.gz    member-wise minimum + which member supplies it
Everything else (blends of any systems, the swath = running minimum, colouring, minimum-field lows, tooltips)
is computed in the browser from these.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from . import __version__
from .config import ROOT
from .decode import DecodeError, Grid
from .fetcher import FetchError, Http, NotFound
from .palette import build_scale
from . import gridio
from .products import detect_lows, ensemble_mean, ensemble_min, write_json
from .sources import Family

log = logging.getLogger("gefs.pipeline")


def make_http(cfg, **over) -> Http:
    f = cfg.fetch
    ua = f.user_agent
    if os.environ.get("GITHUB_REPOSITORY"):          # identify ourselves properly when running in Actions
        ua = ua.replace("YOUR-USER/gefs-mslp-viewer", os.environ["GITHUB_REPOSITORY"])
    return Http(user_agent=ua, rps=over.get("rps", f.requests_per_second),
                host_rps={h: float(getattr(cfg, sec).requests_per_second) for h, sec in (("ecmwf-forecasts", "ifs"), ("weather.gc.ca", "geps"), ("graphcastgfs", "aigefs")) if hasattr(cfg, sec)},
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


def build_frame(cfg, frames: dict[str, FamilyFrame], order: list[str], fh: int, out_dir: Path, run_id: str) -> dict:
    """Write the per-system binary grids of one forecast hour and return its manifest entry.

    Only each system's own ensemble mean (+ member lows) and member-wise minimum (+ owner) are shipped; blends,
    the swath (running minimum), contour colouring and the lows of minimum fields are computed in the browser."""
    avail = {}
    valid = None
    for k in order:
        if k not in frames:
            continue
        f = _fields(cfg, frames[k])
        valid = valid or f.valid
        hdr = {"fh": fh, "valid": f.valid, "nx": int(len(f.lons)), "ny": int(len(f.lats)),
               "lon0": round(float(f.lons[0]), 4), "lat0": round(float(f.lats[0]), 4),
               "dlon": round(float(f.lons[1] - f.lons[0]), 4), "dlat": round(float(f.lats[1] - f.lats[0]), 4),
               "members": f.n, "source": f.source}
        base = out_dir / run_id / k
        gridio.write(base / "mean" / f"f{fh:03d}.bin.gz", {**hdr, "kind": "mean", "lows": f.lows}, f.mean)
        gridio.write(base / "min" / f"f{fh:03d}.bin.gz", {**hdr, "kind": "min"}, f.min, f.owner)
        avail[k] = {"members": f.n, "grid_deg": f.res, "source": f.source}
    return {"fh": fh, "valid": valid, "avail": avail}


# ---------------------------------------------------------------------------------------------------
# full build
# ---------------------------------------------------------------------------------------------------
def build(cfg, *, run: datetime, out: Path, cache_dir: Path | None, http: Http, workers: int,
          families: list[Family], fhs: list[int], min_frames: int = 1) -> dict:
    site_src = ROOT / "site"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(site_src, out, dirs_exist_ok=True)               # viewer + basemap
    gridio.gzip_json_tree(out / "data" / "basemap")                  # the viewer inflates *.json.gz itself
    data_dir = out / "data"
    run_id = f"{run:%Y%m%d}_{run:%H}"
    shutil.rmtree(data_dir / run_id, ignore_errors=True)

    order = [f.key for f in families]
    fam = {f.key: f for f in families}

    edges, colors, scale_segments = build_scale(cfg.scale)         # ONE scale shared by every product so they compare directly
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
            frames.append(build_frame(cfg, ready, order, fh, data_dir, run_id))
            log.info("f%03d done  %s   [%d/%d frames, %.0fs, %d req, %.0f MB]", fh,
                     "  ".join(f"{k}:{v['members']}" for k, v in frames[-1]["avail"].items()),
                     len(frames), len(fhs), time.time() - t0, http.stats.requests, http.stats.bytes / 1e6)

    if len(frames) < min_frames:
        raise RuntimeError(f"only {len(frames)} frame(s) built (need >= {min_frames})")
    frames.sort(key=lambda f: f["fh"])

    fam_meta = {k: {"label": fam[k].label, "name": fam[k].name, "org": fam[k].org, "note": fam[k].note,
                    "members": list(fam[k].members),
                    "frames": sum(1 for f in frames if k in f["avail"])} for k in order}
    init = run.strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schema": 3,
        "generator": f"gefs-mslp-viewer {__version__}",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run": {"id": run_id, "init": init, "cycle": f"{run:%H}", "date": f"{run:%Y%m%d}"},
        "domain": {"west": cfg.domain.west, "east": cfg.domain.east, "south": cfg.domain.south, "north": cfg.domain.north,
                   "initial": list(cfg.domain.initial_view)},
        "families": fam_meta,                                         # single systems only; the viewer builds every blend itself
        "views": list(order),
        "blend": {"weighting": cfg.blend.weighting},
        "lows_threshold_hpa": cfg.lows.threshold_hpa,
        "lows_min": {"threshold_hpa": cfg.lows_min.threshold_hpa, "radius_deg": cfg.lows_min.radius_deg,
                     "smooth_deg": cfg.lows_min.smooth_deg, "min_separation_deg": cfg.lows_min.min_separation_deg},
        "products": {
            k: {"title": getattr(cfg.products, k).title, "short": getattr(cfg.products, k).short,
                "has_lows": bool(getattr(cfg.products, k).has_lows), "units": "hPa",
                "edges": edges, "colors": colors}
            for k in ("mean", "min", "swath")
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
    site_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    (log.warning if site_mb > 850 else log.info)(
        "site size %.0f MB%s", site_mb, "  - close to GitHub Pages' 1 GB limit" if site_mb > 850 else "")
    return manifest
