#!/usr/bin/env python3
"""Re-fit the estimated-gust factors ([wind.estimate] in config.toml) from what GEFS and the ECMWF IFS ensemble forecast.

    python tools/calibrate_gust.py --cycle 2026111900 [--members 12] [--hours 12,24,36,48,60,72,96,120,144,168] [--out /tmp/calib]

Downloads member 10 m wind + gust pairs of both systems (about 650 MB, a few minutes at the configured request rates), then prints, per land / water
and 3 mph wind-speed bin, the mean gust / wind ratio of each system and their average, plus the bias of the CURRENT config curves against each.
Copy the pooled ratios into [wind.estimate] (smoothing them and tapering where a bin has too few samples).  Use a cycle with a decent wind
climate in it: a quiet pattern says nothing about 40 mph winds."""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gefs_pipeline.config import load_config                      # noqa: E402
from gefs_pipeline.gefs import parse_cycle                        # noqa: E402
from gefs_pipeline.gustest import gust_factor                     # noqa: E402
from gefs_pipeline.landmask import land_mask                      # noqa: E402
from gefs_pipeline.pipeline import make_http                      # noqa: E402
from gefs_pipeline.sources import make_families                   # noqa: E402

VIEW = (21, 53, -128, -63)                                          # south, north, west, east: the viewer's default area


def pull(cfg, run, hours, nmem, cache):
    http, out = make_http(cfg), {}
    for key, fam in {f.key: f for f in make_families(cfg, ["gefs", "ifs"])}.items():
        with ThreadPoolExecutor(6) as pool:
            jobs = [pool.submit(fam.fetch_member, http, run, fam.plan(http, run, fh), m, fh, cache, True) for fh in hours for m in fam.members[:nmem]]
        pairs = [(r.wind["wind"], r.wind["gust"]) for r in (j.result() for j in jobs) if r.wind and "gust" in r.wind]
        out[key] = (np.array([w.values for w, _ in pairs]), np.array([g.values for _, g in pairs]), pairs[0][0].lats, pairs[0][0].lons)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycle", required=True)
    ap.add_argument("--members", type=int, default=12)
    ap.add_argument("--hours", default="12,24,36,48,60,72,96,120,144,168")
    ap.add_argument("--cache", default="/tmp/calib_cache")
    a = ap.parse_args()
    cfg = load_config()
    data = pull(cfg, parse_cycle(a.cycle), [int(x) for x in a.hours.split(",")], a.members, Path(a.cache))
    edges = np.arange(0, 66, 3.0)
    ctr = (edges[:-1] + edges[1:]) / 2
    table = {}
    for src, (w, g, lats, lons) in data.items():
        land = land_mask(lats, lons)
        view = (lats[:, None] >= VIEW[0]) & (lats[:, None] <= VIEW[1]) & (lons[None, :] >= VIEW[2]) & (lons[None, :] <= VIEW[3])
        for surf, m in (("land", land & view), ("sea", ~land & view)):
            M = np.broadcast_to(m, w.shape)
            ww, gg = w[M], g[M]
            r = np.full(len(ctr), np.nan)
            for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
                s = (ww >= lo) & (ww < hi)
                if s.sum() > 1500:
                    r[i] = gg[s].mean() / ww[s].mean()
            table[(src, surf)] = r
        est = w * gust_factor(w, np.broadcast_to(land, w.shape), cfg.wind.estimate)
        for surf, m in (("land", land & view), ("sea", ~land & view)):
            M = np.broadcast_to(m, w.shape) & (w >= 10)
            print(f"# current curves vs {src} gust, {surf}, wind >= 10 mph: bias {np.mean(est[M] - g[M]):+.1f} mph, MAE {np.mean(np.abs(est[M] - g[M])):.1f}")
    for surf in ("land", "sea"):
        print(f"\n{surf}:  speed(mph)  gefs  ifs  pooled")
        for c, x, y in zip(ctr, table[("gefs", surf)], table[("ifs", surf)]):
            if not (np.isnan(x) and np.isnan(y)):
                print(f"   {c:5.1f}   {x:5.2f} {y:5.2f}  {np.nanmean([x, y]):5.2f}")


if __name__ == "__main__":
    main()
