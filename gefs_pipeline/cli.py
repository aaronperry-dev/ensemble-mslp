"""Command line:  python -m gefs_pipeline {resolve-cycle,build,basemap}"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, load_config
from .gefs import forecast_hours, parse_cycle
from .pipeline import build, make_http
from .sources import make_families, resolve_cycle, select_families


def _common(p: argparse.ArgumentParser):
    p.add_argument("--config", default=None, help="path to config.toml")
    p.add_argument("--cycle", default=None, help="model cycle YYYYMMDDHH (default: newest complete cycle)")
    p.add_argument("--max-fh", type=int, default=None, help="limit forecast hours (testing)")
    p.add_argument("--members", type=int, default=None, help="use only the first N members of each family (testing)")
    p.add_argument("--families", default=None, help="comma-separated ensemble families to build, e.g. gefs,ifs (default: config [run].families)")
    p.add_argument("--base-url", default=None, help="override the GEFS bucket URL (testing / mirrors)")
    p.add_argument("--geps-base-url", default=None, help="override the GEPS (MSC Datamart) URL template (testing / mirrors)")
    p.add_argument("--aigefs-base-url", default=None, help="override the AIGEFS bucket URL (testing / mirrors)")
    p.add_argument("--ifs-base-url", default=None, help="override the ECMWF bucket URL (testing / mirrors)")
    p.add_argument("--rps", type=float, default=None, help="override requests-per-second cap")
    p.add_argument("--no-wind", action="store_true", help="skip the 10 m wind / gust products (much less data to download)")
    p.add_argument("-v", "--verbose", action="store_true")


def _setup(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.base_url:
        cfg.source.base_url = args.base_url.rstrip("/")
    if args.ifs_base_url:
        cfg.ifs.base_url = args.ifs_base_url.rstrip("/")
    if args.geps_base_url:
        cfg.geps.base_url = args.geps_base_url.rstrip("/")
    if args.aigefs_base_url:
        cfg.aigefs.base_url = args.aigefs_base_url.rstrip("/")
    if getattr(args, "no_wind", False):
        cfg.wind.enabled = False
    families = make_families(cfg, [k.strip() for k in args.families.split(",")] if args.families else None)
    if args.members:
        for f in families:
            f.members = f.members[: args.members]
    http = make_http(cfg, **({"rps": args.rps} if args.rps else {}))
    fhs = forecast_hours(cfg, args.max_fh)
    return cfg, families, http, fhs


def _deployed_info(url: str) -> tuple[str | None, set[str]]:
    """(run id, single-system families) of the manifest the live site serves."""
    import requests
    try:
        r = requests.get(url, timeout=15, headers={"Cache-Control": "no-cache"})
        if not r.ok:
            return None, set()
        m = r.json()
        return m["run"]["id"], {k for k, v in m.get("families", {}).items() if not v.get("parts")}
    except Exception:
        return None, set()


def should_skip(live_id: str | None, live_fams: set[str], run_id: str, ready: list[str]) -> bool:
    """Skip the build when the same cycle is live AND already contains every system that is ready now."""
    return live_id == run_id and set(ready) <= set(live_fams)


def cmd_resolve(args) -> int:
    cfg, families, http, fhs = _setup(args)
    run = parse_cycle(args.cycle) if args.cycle else resolve_cycle(http, cfg, families, fhs)
    cyc = run.strftime("%Y%m%d%H")
    run_id = f"{run:%Y%m%d}_{run:%H}"
    ready = [f.key for f in select_families(http, cfg, families, run, fhs)]
    skip = False
    if args.skip_if_deployed:
        live_id, live_fams = _deployed_info(args.skip_if_deployed)
        # same cycle is live AND it already contains every system that is ready now (an optional system that arrived
        # late - AIGEFS - triggers a rebuild of the live cycle)
        skip = should_skip(live_id, live_fams, run_id, ready)
        if live_id == run_id and not skip:
            print(f"live site has {sorted(live_fams)}; now ready: {ready} -> rebuilding")
    out = f"cycle={cyc}\nrun_id={run_id}\nfamilies={','.join(ready)}\nskip={'true' if skip else 'false'}"
    print(out)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(out + "\n")
    return 0


def cmd_build(args) -> int:
    cfg, families, http, fhs = _setup(args)
    run = parse_cycle(args.cycle) if args.cycle else resolve_cycle(http, cfg, families, fhs)
    families = select_families(http, cfg, families, run, fhs)           # optional systems only when fully published
    logging.getLogger("gefs").info("building %s  |  %s  |  %d forecast hours (f%03d-f%03d)", run.strftime("%Y-%m-%d %HZ"),
                                   " + ".join(f"{f.label} ({len(f.members)})" for f in families), len(fhs), fhs[0], fhs[-1])
    cache = None if args.no_cache else Path(args.cache_dir)
    build(cfg, run=run, out=Path(args.out), cache_dir=cache, http=http, workers=args.workers or int(cfg.fetch.workers),
          families=families, fhs=fhs, min_frames=args.min_frames)
    return 0


def cmd_basemap(args) -> int:
    from .basemap import build_basemap
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    build_basemap(load_config(args.config), Path(args.out), Path(args.cache_dir))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gefs_pipeline", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve-cycle", help="print the newest complete cycle (and GitHub Actions outputs)")
    _common(p)
    p.add_argument("--skip-if-deployed", metavar="MANIFEST_URL", default=None,
                   help="set skip=true if the live site already serves this run")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("build", help="fetch + process + write the static site into --out")
    _common(p)
    p.add_argument("--out", default=str(ROOT / "dist"))
    p.add_argument("--cache-dir", default=str(ROOT / ".cache" / "gefs"))
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--min-frames", type=int, default=1, help="fail if fewer frames were produced")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("basemap", help="(re)build site/data/basemap from Natural Earth (needs shapely)")
    p.add_argument("--config", default=None)
    p.add_argument("--out", default=str(ROOT / "site" / "data" / "basemap"))
    p.add_argument("--cache-dir", default=str(ROOT / ".cache" / "naturalearth"))
    p.set_defaults(fn=cmd_basemap)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
