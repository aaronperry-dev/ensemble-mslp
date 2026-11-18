"""Orchestration: resolve cycle -> (prefetching) fetch members -> products -> static files + manifest."""
from __future__ import annotations

import logging
import os
import shutil
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import __version__
from .config import ROOT, member_names
from .decode import DecodeError, Grid, decode_message
from .fetcher import FetchError, Http, NotFound, find_message, parse_idx
from .gefs import choose_product, forecast_hours, object_url
from .palette import build_scale
from .products import (contour_bands, detect_lows, ensemble_mean, ensemble_min, upsample, write_json)

log = logging.getLogger("gefs.pipeline")


def make_http(cfg, **over) -> Http:
    f = cfg.fetch
    ua = f.user_agent
    if os.environ.get("GITHUB_REPOSITORY"):          # identify ourselves properly when running in Actions
        ua = ua.replace("YOUR-USER/gefs-mslp-viewer", os.environ["GITHUB_REPOSITORY"])
    return Http(user_agent=ua, rps=over.get("rps", f.requests_per_second), max_retries=f.max_retries,
                backoff_base=f.backoff_base_s, backoff_max=f.backoff_max_s,
                timeout=(f.timeout_connect_s, f.timeout_read_s))


# ---------------------------------------------------------------------------------------------------
# one member / one hour
# ---------------------------------------------------------------------------------------------------
def fetch_member_grid(http: Http, cfg, run: datetime, product, member: str, fh: int, cache_dir: Path | None) -> Grid:
    """idx lookup + byte-range GET of the single MSLP message (cached on disk), then decode+subset."""
    d = cfg.domain
    pad = float(d.pad_deg)
    bbox = (d.west - pad, d.east + pad, d.south - pad, d.north + pad)
    url = object_url(cfg, run, product, member, fh)
    cache = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{product.dir}.f{fh:03d}.grib2" if cache_dir else None
    raw = None
    if cache and cache.exists():
        raw = cache.read_bytes()
    if raw is None:
        entries = parse_idx(http.get_text(url + ".idx"))
        start, end = find_message(entries, cfg.source.variable, cfg.source.level)
        raw = http.get_range(url, start, end)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, cache)        # atomic: an interrupted run never leaves a half-written file
    return decode_message(raw, *bbox)


# ---------------------------------------------------------------------------------------------------
# frame products
# ---------------------------------------------------------------------------------------------------
def build_frame(cfg, grids: dict[str, Grid], members: list[str], fh: int, product, scales: dict,
                out_dir: Path, run_id: str) -> dict:
    """Write mean/min frame files and return the manifest entry."""
    names = [m for m in members if m in grids]
    ref = grids[names[0]]
    ok = [m for m in names if grids[m].values.shape == ref.values.shape]
    if len(ok) < len(names):
        log.warning("f%03d: dropped %d members with a different grid", fh, len(names) - len(ok))
    stack = np.stack([grids[m].values for m in ok])
    d = cfg.domain
    meta = {"fh": fh, "valid": ref.valid_time, "grid_deg": ref.res, "members": len(ok), "source": product.dir}
    prec = int(cfg.contours.precision)
    f3 = f"f{fh:03d}"

    # --- product 1: ensemble mean + every member's low-pressure centres --------------------------------
    mean = ensemble_mean(stack)
    la, lo, z = upsample(ref.lats, ref.lons, mean, float(cfg.contours.upsample_to_deg))
    feats = contour_bands(la, lo, z, scales["mean"][0], prec)
    L = cfg.lows
    lows = []
    for mi, m in enumerate(members):
        if m not in ok:
            continue
        g = grids[m]
        for lon, lat, p in detect_lows(g.lats, g.lons, g.values, threshold_hpa=L.threshold_hpa, radius_deg=L.radius_deg,
                                       smooth_deg=L.smooth_deg, min_sep_deg=L.min_separation_deg,
                                       domain=(d.west, d.east, d.south, d.north)):
            lows.append([round(lon, 2), round(lat, 2), round(p, 1), mi])
    write_json(out_dir / run_id / "mean" / f"{f3}.json",
               {"type": "FeatureCollection", "features": feats, "meta": meta, "lows": lows})

    # --- product 2: member-wise minimum -------------------------------------------------------------
    mn = ensemble_min(stack)
    la, lo, z = upsample(ref.lats, ref.lons, mn, float(cfg.contours.upsample_to_deg))
    feats = contour_bands(la, lo, z, scales["min"][0], prec)
    write_json(out_dir / run_id / "min" / f"{f3}.json", {"type": "FeatureCollection", "features": feats, "meta": meta})

    return {**meta, "n_lows": len(lows), "mean_hpa": [round(float(np.nanmin(mean)), 1), round(float(np.nanmax(mean)), 1)],
            "min_hpa": [round(float(np.nanmin(mn)), 1), round(float(np.nanmax(mn)), 1)]}


# ---------------------------------------------------------------------------------------------------
# full build
# ---------------------------------------------------------------------------------------------------
def build(cfg, *, run: datetime, out: Path, cache_dir: Path | None, http: Http, workers: int, members: list[str],
          fhs: list[int], min_frames: int = 1) -> dict:
    site_src = ROOT / "site"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(site_src, out, dirs_exist_ok=True)               # viewer + basemap
    data_dir = out / "data"
    run_id = f"{run:%Y%m%d}_{run:%H}"
    shutil.rmtree(data_dir / run_id, ignore_errors=True)

    edges, colors, scale_segments = build_scale(cfg.scale)         # one scale, shared by both products
    scales = {"mean": (edges, colors), "min": (edges, colors)}
    min_members = max(1, int(np.ceil(len(members) * float(cfg.fetch.min_member_fraction))))
    t0 = time.time()

    def submit(pool, fh):
        product = choose_product(http, cfg, run, fh)
        if product is None:
            log.error("f%03d: not available in any product - skipped", fh)
            return fh, None, {}
        return fh, product, {m: pool.submit(fetch_member_grid, http, cfg, run, product, m, fh, cache_dir) for m in members}

    frames: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetch") as pool:
        queue = deque()
        todo = deque(fhs)
        # keep one frame of look-ahead: the next frame downloads while the current one is being contoured
        for _ in range(min(2, len(todo))):
            queue.append(submit(pool, todo.popleft()))
        while queue:
            fh, product, futs = queue.popleft()
            if todo:
                queue.append(submit(pool, todo.popleft()))
            grids: dict[str, Grid] = {}
            for m, fut in futs.items():
                try:
                    grids[m] = fut.result()
                except (NotFound, FetchError, DecodeError, KeyError) as e:
                    log.warning("f%03d %s: %s", fh, m, e)
            if len(grids) < min_members:
                log.error("f%03d: only %d/%d members (need %d) - frame skipped", fh, len(grids), len(members), min_members)
                continue
            frames.append(build_frame(cfg, grids, members, fh, product, scales, data_dir, run_id))
            log.info("f%03d done  %s %.2f deg  %d members  %d lows   [%d/%d frames, %.0fs, %d req, %.0f MB]",
                     fh, product.dir, frames[-1]["grid_deg"], frames[-1]["members"], frames[-1]["n_lows"],
                     len(frames), len(fhs), time.time() - t0, http.stats.requests, http.stats.bytes / 1e6)

    if len(frames) < min_frames:
        raise RuntimeError(f"only {len(frames)} frame(s) built (need >= {min_frames})")
    frames.sort(key=lambda f: f["fh"])
    init = run.strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schema": 1,
        "generator": f"gefs-mslp-viewer {__version__}",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run": {"id": run_id, "model": "GEFS", "init": init, "cycle": f"{run:%H}", "date": f"{run:%Y%m%d}"},
        "domain": {"west": cfg.domain.west, "east": cfg.domain.east, "south": cfg.domain.south, "north": cfg.domain.north,
                   "initial": list(cfg.domain.initial_view)},
        "members": members,
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
