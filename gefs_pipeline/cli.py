"""Command line:  python -m gefs_pipeline {resolve-cycle,build,basemap}"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, load_config, member_names
from .gefs import forecast_hours, parse_cycle, resolve_cycle
from .pipeline import build, make_http


def _common(p: argparse.ArgumentParser):
    p.add_argument("--config", default=None, help="path to config.toml")
    p.add_argument("--cycle", default=None, help="model cycle YYYYMMDDHH (default: newest complete cycle)")
    p.add_argument("--max-fh", type=int, default=None, help="limit forecast hours (testing)")
    p.add_argument("--members", type=int, default=None, help="use only the first N members (testing)")
    p.add_argument("--base-url", default=None, help="override bucket URL (testing / mirrors)")
    p.add_argument("--rps", type=float, default=None, help="override requests-per-second cap")
    p.add_argument("-v", "--verbose", action="store_true")


def _setup(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.base_url:
        cfg.source.base_url = args.base_url.rstrip("/")
    members = member_names(cfg)
    if args.members:
        members = members[: args.members]
    http = make_http(cfg, **({"rps": args.rps} if args.rps else {}))
    fhs = forecast_hours(cfg, args.max_fh)
    return cfg, members, http, fhs


def _deployed_run(url: str) -> str | None:
    import requests
    try:
        r = requests.get(url, timeout=15, headers={"Cache-Control": "no-cache"})
        return r.json()["run"]["id"] if r.ok else None
    except Exception:
        return None


def cmd_resolve(args) -> int:
    cfg, members, http, fhs = _setup(args)
    run = parse_cycle(args.cycle) if args.cycle else resolve_cycle(http, cfg, fhs)
    cyc = run.strftime("%Y%m%d%H")
    run_id = f"{run:%Y%m%d}_{run:%H}"
    skip = bool(args.skip_if_deployed and _deployed_run(args.skip_if_deployed) == run_id)
    print(f"cycle={cyc}\nrun_id={run_id}\nskip={'true' if skip else 'false'}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"cycle={cyc}\nrun_id={run_id}\nskip={'true' if skip else 'false'}\n")
    return 0


def cmd_build(args) -> int:
    cfg, members, http, fhs = _setup(args)
    run = parse_cycle(args.cycle) if args.cycle else resolve_cycle(http, cfg, fhs)
    logging.getLogger("gefs").info("building GEFS %s  |  %d members  |  %d forecast hours (f%03d-f%03d)",
                                   run.strftime("%Y-%m-%d %HZ"), len(members), len(fhs), fhs[0], fhs[-1])
    cache = None if args.no_cache else Path(args.cache_dir)
    build(cfg, run=run, out=Path(args.out), cache_dir=cache, http=http, workers=args.workers or int(cfg.fetch.workers),
          members=members, fhs=fhs, min_frames=args.min_frames)
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
