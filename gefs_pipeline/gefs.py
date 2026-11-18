"""GEFS bucket layout helpers (verified against s3://noaa-gefs-pds, GEFS v12+ layout).

  gefs.YYYYMMDD/HH/atmos/pgrb2sp25/gep01.tHHz.pgrb2s.0p25.fFFF(.idx)   0.25-deg, f000-f240 (3-hourly)
  gefs.YYYYMMDD/HH/atmos/pgrb2ap5 /gep01.tHHz.pgrb2a.0p50.fFFF(.idx)   0.50-deg, f000-f384
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import member_names
from .fetcher import Http

log = logging.getLogger("gefs.layout")


def parse_cycle(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def forecast_hours(cfg, max_fh: int | None = None) -> list[int]:
    stop = cfg.forecast.stop if max_fh is None else min(cfg.forecast.stop, max_fh)
    return list(range(cfg.forecast.start, stop + 1, cfg.forecast.step))


def products_for_fh(cfg, fh: int) -> list:
    """Products able to serve this forecast hour, highest resolution first (config order)."""
    return [p for p in cfg.source.products if fh <= p.max_fh]


def object_url(cfg, run: datetime, product, member: str, fh: int) -> str:
    return (f"{cfg.source.base_url}/gefs.{run:%Y%m%d}/{run:%H}/atmos/{product.dir}/"
            f"{member}.t{run:%H}z.{product.file_tag}.f{fh:03d}")


def choose_product(http: Http, cfg, run: datetime, fh: int):
    """Highest-resolution product that actually exists for this hour (probes the control member's .idx)."""
    for p in products_for_fh(cfg, fh):
        if http.exists(object_url(cfg, run, p, "gec00", fh) + ".idx"):
            return p
        log.info("f%03d: %s not available, trying next resolution", fh, p.dir)
    return None


def cycle_is_complete(http: Http, cfg, run: datetime, members: list[str], fhs: list[int]) -> bool:
    """True when the *last* forecast hour exists for every member (S3 publishes hours progressively)."""
    fh = fhs[-1]
    prods = products_for_fh(cfg, fh)
    if not prods:
        return False
    p = prods[0]                                  # highest-resolution product: what frames are fetched from
    return all(http.exists(object_url(cfg, run, p, m, fh) + ".idx") for m in members)


def resolve_cycle(http: Http, cfg, fhs: list[int], now: datetime | None = None, lookback: int = 8) -> datetime:
    """Newest 00/06/12/18Z cycle that is fully published."""
    now = now or datetime.now(timezone.utc)
    run = now.replace(minute=0, second=0, microsecond=0)
    run -= timedelta(hours=run.hour % 6)
    members = member_names(cfg)
    for _ in range(lookback):
        # cheap sentinel first: last member, last hour
        p = products_for_fh(cfg, fhs[-1])[0]
        sentinel = object_url(cfg, run, p, members[-1], fhs[-1]) + ".idx"
        if http.exists(sentinel) and cycle_is_complete(http, cfg, run, members, fhs):
            return run
        log.info("cycle %s not complete yet", run.strftime("%Y%m%d%H"))
        run -= timedelta(hours=6)
    raise RuntimeError("no complete GEFS cycle found in the look-back window")
