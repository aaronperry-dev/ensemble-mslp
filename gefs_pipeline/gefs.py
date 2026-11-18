"""GEFS bucket layout helpers (verified against s3://noaa-gefs-pds, GEFS v12+ layout).

  gefs.YYYYMMDD/HH/atmos/pgrb2sp25/gep01.tHHz.pgrb2s.0p25.fFFF(.idx)   0.25-deg, f000-f240 (3-hourly)
  gefs.YYYYMMDD/HH/atmos/pgrb2ap5 /gep01.tHHz.pgrb2a.0p50.fFFF(.idx)   0.50-deg, f000-f384
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

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
