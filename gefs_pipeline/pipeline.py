"""Orchestration: resolve cycle -> (prefetching) fetch every family's members -> per-system grids -> static files + manifest.

Per forecast hour and system these gzip'd binary grids are written (see gridio.py):
    data/<run>/<system>/mean/fNNN.bin.gz   ensemble mean + member low centers
    data/<run>/<system>/min/fNNN.bin.gz    member-wise minimum + which member supplies it
    data/<run>/<system>/prob/fNNN.bin.gz   members below each pressure level
and, where the system publishes 10 m wind (w...) and gust (g...), in mph:
    data/<run>/<system>/{w,g}mean/fNNN.bin.gz   ensemble mean of the members' speeds
    data/<run>/<system>/{w,g}max/fNNN.bin.gz    highest member + which member supplies it
    data/<run>/<system>/{w,g}prob/fNNN.bin.gz   members at or above each speed level
Everything else (blends of any systems, the swath = running minimum, colouring, minimum-field lows, tooltips)
is computed in the browser from these.
"""
from __future__ import annotations

import logging
import os
import gzip
import json
import shutil
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from . import __version__
from .config import ROOT
from .decode import DecodeError, Grid
from .fetcher import FetchError, Http, NotFound
from .palette import RAMPS, build_scale, color_at
from .tracks import link_tracks
from . import gridio
from .products import detect_lows, ensemble_max, ensemble_mean, ensemble_min, write_json
from .gustest import estimate_gust
from .sources import Family, MemberResult

log = logging.getLogger("gefs.pipeline")


def make_http(cfg, **over) -> Http:
    f = cfg.fetch
    ua = f.user_agent
    if os.environ.get("GITHUB_REPOSITORY"):          # identify ourselves properly when running in Actions
        ua = ua.replace("YOUR-USER/gefs-mslp-viewer", os.environ["GITHUB_REPOSITORY"])
    # one limiter per host (substring of the hostname -> config section); a host without its own cap uses [fetch] requests_per_second
    hosts = (("noaa-gefs-pds", "source"), ("ecmwf-forecasts", "ifs"), ("weather.gc.ca", "geps"), ("graphcastgfs", "aigefs"))
    return Http(user_agent=ua, rps=over.get("rps", f.requests_per_second),
                host_rps={h: float(getattr(getattr(cfg, sec), "requests_per_second")) for h, sec in hosts
                          if hasattr(cfg, sec) and hasattr(getattr(cfg, sec), "requests_per_second")},
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
    wind: dict | None = None        # {"wind": {member: Grid mph}, "gust": {member: Grid mph}} (a key is absent when not usable)
    gust_estimated: bool = False    # the "gust" grids were estimated from the wind (gustest.py), not forecast by the system


WIND_SCALE = 2                      # wind / gust grids are stored in half-mph steps (see gridio)
WIND_VARS = (("wind", "w"), ("gust", "g"))


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

    counts = {float(lv): (stack < float(lv)).sum(axis=0).astype(np.int32) for lv in cfg.prob.levels}   # members below each level
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
    return SimpleNamespace(lats=ref.lats, lons=ref.lons, mean=mean, min=mn, owner=owner, lows=lows, counts=counts,
                           n=len(names), res=ref.res, valid=ref.valid_time, source=ff.fam.plan_tag(ff.plan))


def _wind_fields(cfg, fam: Family, grids: dict[str, Grid], var: str) -> SimpleNamespace:
    """Mean, highest member (+ owner) and members-at-or-above counts of one wind variable (mph) for one system and hour."""
    names = [m for m in fam.members if m in grids]
    ref = grids[names[0]]
    names = [m for m in names if grids[m].values.shape == ref.values.shape]
    stack = np.stack([grids[m].values for m in names])
    midx = {m: i for i, m in enumerate(fam.members)}
    to_member = np.array([midx[m] for m in names])
    levels = [float(x) for x in (cfg.wind.speed_levels if var == "wind" else cfg.wind.gust_levels)]
    owner = to_member[np.argmax(np.where(np.isfinite(stack), stack, -np.inf), axis=0)]
    return SimpleNamespace(lats=ref.lats, lons=ref.lons, mean=ensemble_mean(stack), max=ensemble_max(stack), owner=owner,
                           counts=np.stack([(stack >= lv).sum(axis=0).astype(np.int32) for lv in levels]), levels=levels,
                           n=len(names), res=ref.res, valid=ref.valid_time)


def build_frame(cfg, frames: dict[str, FamilyFrame], order: list[str], fh: int, out_dir: Path, run_id: str,
                lows_out: dict | None = None) -> dict:
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
        levels = [float(x) for x in cfg.prob.levels]
        gridio.write_counts(base / "prob" / f"f{fh:03d}.bin.gz", {**hdr, "kind": "prob", "levels": levels},
                           np.stack([f.counts[lv] for lv in levels]))
        avail[k] = {"members": f.n, "grid_deg": f.res, "source": f.source}
        for var, pre in WIND_VARS:                                   # 10 m wind / gust (only where the system published them)
            wg = (frames[k].wind or {}).get(var)
            if not wg:
                continue
            w = _wind_fields(cfg, frames[k].fam, wg, var)
            whdr = {"fh": fh, "valid": w.valid, "nx": int(len(w.lons)), "ny": int(len(w.lats)),
                    "lon0": round(float(w.lons[0]), 4), "lat0": round(float(w.lats[0]), 4),
                    "dlon": round(float(w.lons[1] - w.lons[0]), 4), "dlat": round(float(w.lats[1] - w.lats[0]), 4),
                    "members": w.n, "source": f.source, "unit": "mph"}
            gridio.write(base / f"{pre}mean" / f"f{fh:03d}.bin.gz", {**whdr, "kind": "mean"}, w.mean, scale=WIND_SCALE)
            gridio.write(base / f"{pre}max" / f"f{fh:03d}.bin.gz", {**whdr, "kind": "max"}, w.max, w.owner, scale=WIND_SCALE)
            gridio.write_counts(base / f"{pre}prob" / f"f{fh:03d}.bin.gz", {**whdr, "kind": "prob", "levels": w.levels}, w.counts)
            avail[k][var] = {"members": w.n, "grid_deg": w.res, **({"estimated": True} if var == "gust" and frames[k].gust_estimated else {})}
        if lows_out is not None:
            lows_out.setdefault(k, {})[fh] = f.lows
    return {"fh": fh, "valid": valid, "avail": avail}


def _collect_wind(fam: Family, wfuts: dict, fh: int, need: int) -> dict | None:
    """Gather one system's wind / gust members for one hour.  Wind is optional: members that failed are logged and left out,
    and a variable with fewer than `need` members is not published for that hour (MSLP is unaffected)."""
    got: dict[str, dict[str, Grid]] = {"wind": {}, "gust": {}}
    failed: dict[str, int] = {}
    for m, item in wfuts.items():
        try:
            res = item.result() if hasattr(item, "result") else item          # a future, or a MemberResult / grid dict already in hand
            if isinstance(res, MemberResult):
                if res.wind_error is not None:
                    raise res.wind_error
                res = res.wind or {}
        except (NotFound, FetchError, DecodeError, KeyError) as e:
            failed[type(e).__name__] = failed.get(type(e).__name__, 0) + 1
            continue
        for var in got:
            if var in res:
                got[var][m] = res[var]
    if failed:
        log.warning("f%03d %s: wind unavailable for %d members (%s)", fh, fam.key, sum(failed.values()),
                    ", ".join(f"{n} x{c}" for n, c in failed.items()))
    out = {}
    for var, grids in got.items():
        if not grids:
            continue
        if len(grids) < need:
            log.warning("f%03d %s: only %d/%d members have %s - not published for this hour", fh, fam.key, len(grids), len(fam.members), var)
            continue
        out[var] = grids
    return out or None


def _with_estimated_gust(cfg, fam: Family, wind: dict | None) -> tuple[dict | None, bool]:
    """A system that publishes wind but no gust (and is listed in [wind.estimate]) gets a gust estimated from each member's wind."""
    est = getattr(cfg.wind, "estimate", None)
    if (not wind or "wind" not in wind or "gust" in wind or est is None or not getattr(est, "enabled", False)
            or fam.has_gust or fam.key not in est.families):
        return wind, False
    return {**wind, "gust": {m: estimate_gust(g, est) for m, g in wind["wind"].items()}}, True


# ---------------------------------------------------------------------------------------------------
# full build
# ---------------------------------------------------------------------------------------------------
def build(cfg, *, run: datetime, out: Path, cache_dir: Path | None, http: Http, workers: int,
          families: list[Family], fhs: list[int], min_frames: int = 1) -> dict:
    site_src = ROOT / "site"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(site_src, out, dirs_exist_ok=True)               # viewer + basemap
    # cache-busting: phones keep old CSS/JS for a long time, so every build stamps the asset URLs in index.html
    idx = out / "index.html"
    if idx.exists():
        import re
        stamp = f"{run:%Y%m%d%H}-{int(time.time())}"
        html = idx.read_text(encoding="utf-8")
        html = re.sub(r'((?:href|src)="(?:style\.css|engine\.js|gif\.js|app\.js))(?:\?v=[^"]*)?"', lambda m: f'{m.group(1)}?v={stamp}"', html)
        idx.write_text(html, encoding="utf-8")
    gridio.gzip_json_tree(out / "data" / "basemap")                  # the viewer inflates *.json.gz itself
    data_dir = out / "data"
    run_id = f"{run:%Y%m%d}_{run:%H}"
    shutil.rmtree(data_dir / run_id, ignore_errors=True)

    order = [f.key for f in families]
    fam = {f.key: f for f in families}

    edges, colors, scale_segments = build_scale(cfg.scale)         # ONE scale shared by every product so they compare directly
    wcfg = getattr(cfg, "wind", None)
    wind_on = bool(wcfg is not None and getattr(wcfg, "enabled", False))
    wind_fams = set(wcfg.families) if wind_on else set()
    wind_max = int(wcfg.max_fh) if wind_on else -1
    need = {k: max(1, int(np.ceil(len(fam[k].members) * float(cfg.fetch.min_member_fraction)))) for k in order}
    t0 = time.time()

    def submit(pools, fh):
        jobs = {}
        for k in order:
            f = fam[k]
            if fh > f.max_fh(run):
                continue                                              # e.g. IFS 06/18Z runs stop at 144 h
            plan = f.plan(http, run, fh)
            if plan is None:
                log.warning("f%03d: %s not published - skipped", fh, k)
                continue
            want_wind = bool(wind_on and f.has_wind and k in wind_fams and fh <= wind_max)
            # one job per member returns its MSLP and (when wanted) wind / gust, so the requests of a file can be shared
            futs = {m: pools[k].submit(f.fetch_member, http, run, plan, m, fh, cache_dir, want_wind) for m in f.members}
            jobs[k] = (plan, futs, want_wind)
        return fh, jobs

    frames: list[dict] = []
    lows_by_sys: dict[str, dict[int, list]] = {}
    # One worker pool PER SYSTEM (= per host, each with its own request limiter).  With a single shared pool, workers sleeping in
    # the limiter of a slow-capped host (ECMWF) would hold every thread and the other hosts would sit idle: a frame then took the
    # SUM of the systems' download times instead of the longest one.
    with ExitStack() as stack:
        pools = {k: stack.enter_context(ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"fetch-{k}")) for k in order}
        queue = deque()
        todo = deque(fhs)
        # keep one frame of look-ahead: the next frame downloads while the current one is being contoured
        for _ in range(min(2, len(todo))):
            queue.append(submit(pools, todo.popleft()))
        while queue:
            fh, jobs = queue.popleft()
            if todo:
                queue.append(submit(pools, todo.popleft()))
            ready: dict[str, FamilyFrame] = {}
            for k, (plan, futs, want_wind) in jobs.items():
                grids: dict[str, Grid] = {}
                wres: dict[str, MemberResult] = {}
                for m, fut in futs.items():
                    try:
                        res = fut.result()
                        grids[m] = res.grid
                        wres[m] = res
                    except (NotFound, FetchError, DecodeError, KeyError) as e:
                        log.warning("f%03d %s %s: %s", fh, k, m, repr(e) if not str(e) else e)
                ff = _usable(fam[k], grids, fh)
                if ff is None or len(ff.names) < need[k]:
                    log.error("f%03d %s: only %d/%d members (need %d) - family skipped for this hour",
                              fh, k, 0 if ff is None else len(ff.names), len(fam[k].members), need[k])
                    continue
                ff.plan = plan
                if want_wind:
                    ff.wind = _collect_wind(fam[k], wres, fh, need[k])
                    ff.wind, ff.gust_estimated = _with_estimated_gust(cfg, fam[k], ff.wind)
                ready[k] = ff
            if not ready:
                continue
            frames.append(build_frame(cfg, ready, order, fh, data_dir, run_id, lows_by_sys))
            log.info("f%03d done  %s   [%d/%d frames, %.0fs, %d req, %.0f MB]", fh,
                     "  ".join(f"{k}:{v['members']}" for k, v in frames[-1]["avail"].items()),
                     len(frames), len(fhs), time.time() - t0, http.stats.requests, http.stats.bytes / 1e6)

    for line in http.stats.summary():
        log.info("requests  %s", line)
    if len(frames) < min_frames:
        raise RuntimeError(f"only {len(frames)} frame(s) built (need >= {min_frames})")
    frames.sort(key=lambda f: f["fh"])

    tracks_meta = {}
    for k in order:
        if k not in lows_by_sys:
            continue
        trs = link_tracks(lows_by_sys[k], max_speed_kmh=float(cfg.tracks.max_speed_kmh), min_points=int(cfg.tracks.min_points))
        rel = f"{run_id}/{k}/tracks.json.gz"
        blob = json.dumps({"v": 1, "sys": k, "members": list(fam[k].members), "tracks": trs}, separators=(",", ":")).encode()
        (data_dir / run_id / k).mkdir(parents=True, exist_ok=True)
        (data_dir / rel).write_bytes(gzip.compress(blob, compresslevel=9, mtime=0))
        tracks_meta[k] = {"file": rel, "tracks": len(trs)}
        log.info("%s: %d member tracks", k, len(trs))

    def pct_scale(ramp: str) -> tuple[list[float], list[str]]:
        e = [float(x) for x in cfg.prob.edges]
        anchors = RAMPS[ramp]
        cols = ["transparent"] + [color_at(anchors, 0.08 + 0.92 * i / (len(e) - 1)) for i in range(len(e))]
        return e, cols

    fam_meta = {k: {"label": fam[k].label, "name": fam[k].name, "org": fam[k].org, "note": fam[k].note,
                    "members": list(fam[k].members),
                    "frames": sum(1 for f in frames if k in f["avail"]),
                    "wind_frames": sum(1 for f in frames if "wind" in f["avail"].get(k, {})),
                    "gust_frames": sum(1 for f in frames if "gust" in f["avail"].get(k, {}))} for k in order}
    # 10 m wind speed / gust products: only those some system actually published
    wind_products, wind_meta = {}, None
    have = {var: any(var in f["avail"].get(k, {}) for f in frames for k in order) for var, _ in WIND_VARS}
    if wind_on and any(have.values()):
        wedges, wcolors, wsegs = build_scale(cfg.wind_scale)
        wcolors = ["transparent"] + wcolors[1:]                      # below the first edge (calm): leave the map visible
        wpe, wpc = pct_scale(wcfg.prob_ramp)
        for var, pre in WIND_VARS:
            if not have[var]:
                continue
            for kind in ("mean", "max", "swath", "prob"):
                key = pre + kind
                P = getattr(cfg.products, key)
                prob = kind == "prob"
                wind_products[key] = {"title": P.title, "short": P.short, "has_lows": False, "units": "%" if prob else "mph", "var": var,
                                      "edges": wpe if prob else wedges, "colors": wpc if prob else wcolors}
        wind_meta = {"units": "mph", "scale": WIND_SCALE, "speed_levels": [float(x) for x in wcfg.speed_levels],
                     "gust_levels": [float(x) for x in wcfg.gust_levels], "level_names": dict(vars(wcfg.level_names)),
                     "segments": wsegs, "have": have,
                     # systems whose gust is a statistical estimate from their wind speed (not their own forecast), see gustest.py
                     "estimated_gust": [k for k in order if any(f["avail"].get(k, {}).get("gust", {}).get("estimated") for f in frames)]}
    init = run.strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "schema": 4,
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
                "has_lows": bool(getattr(cfg.products, k).has_lows), "units": "hPa", "var": "mslp",
                "edges": edges, "colors": colors}
            for k in ("mean", "min", "swath")
        } | {
            k: {"title": getattr(cfg.products, k).title, "short": getattr(cfg.products, k).short,
                "has_lows": False, "units": "%", "var": "mslp", "edges": pct_scale(ramp)[0], "colors": pct_scale(ramp)[1]}
            for k, ramp in (("prob", cfg.prob.ramp), ("strike", cfg.strike.ramp))
        } | wind_products,
        "prob": {"levels": [float(x) for x in cfg.prob.levels]},
        "strike": {"radii_km": [float(x) for x in cfg.strike.radii_km], "default_km": float(cfg.strike.default_km)},
        "tracks": tracks_meta,
        "scale": {"segments": scale_segments},
        "wind": wind_meta,
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
