"""Ensemble "families": where each system's MSLP lives and how to fetch one member's field.

  * GefsFamily  - NOAA/NCEP GEFS, s3://noaa-gefs-pds   (31 members: gec00 + gep01..gep30), 0.25 deg, f000-f240
  * IfsFamily   - ECMWF IFS ENS, s3://ecmwf-forecasts  (51 members: ec00 + ep01..ep50),   0.25 deg, 3-hourly to
                  144 h and 6-hourly to 360 h on 00/12Z cycles (06/18Z cycles stop at 144 h)

ECMWF layout (verified against the bucket and ECMWF's open-data documentation, IFS cycle 50r1):
  <base>/<YYYYMMDD>/<HH>z/ifs/0p25/enfo/<YYYYMMDD><HH>0000-<step>h-enfo-ef.grib2  (+ .index, JSON lines)
        -> the 50 PERTURBED members (type=pf, number=1..50), all parameters, ~4.4 GB per step
  <base>/<YYYYMMDD>/<HH>z/ifs/0p25/oper/<YYYYMMDD><HH>0000-<step>h-oper-fc.grib2  (+ .index)
        -> the HRES run, which since 50r1 IS the ensemble control (the old enfo type=cf was retired); ~100 MB
  Messages are located through the .index and fetched with HTTP Range requests (~0.5 MB per field).
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .decode import Grid, decode_message
from .fetcher import Http, NotFound, find_message, parse_idx, parse_json_index
from .gefs import choose_product, object_url

log = logging.getLogger("gefs.sources")


def _bbox(cfg) -> tuple[float, float, float, float]:
    d = cfg.domain
    pad = float(d.pad_deg)
    return (d.west - pad, d.east + pad, d.south - pad, d.north + pad)


def _cached(cache: Path | None, fetch) -> bytes:
    """Return the raw GRIB message from the on-disk cache, or fetch it and store it atomically."""
    if cache is not None and cache.exists():
        return cache.read_bytes()
    raw = fetch()
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, cache)                 # atomic: an interrupted run never leaves a half-written file
    return raw


class Family:
    key = ""
    label = ""
    name = ""
    org = ""
    note = ""

    def __init__(self, cfg):
        self.cfg = cfg
        self.members: list[str] = []

    # -- schedule -------------------------------------------------------------------------------------
    def max_fh(self, run: datetime) -> int:
        raise NotImplementedError

    def hours(self, run: datetime, fhs: list[int]) -> list[int]:
        return [h for h in fhs if h <= self.max_fh(run)]

    # -- availability ---------------------------------------------------------------------------------
    def plan(self, http: Http, run: datetime, fh: int):
        """What to fetch this hour from (None = not published).  Opaque to the pipeline."""
        raise NotImplementedError

    def plan_tag(self, plan) -> str:
        raise NotImplementedError

    def cycle_ready(self, http: Http, run: datetime, fhs: list[int]) -> bool:
        raise NotImplementedError

    def fetch_grid(self, http: Http, run: datetime, plan, member: str, fh: int, cache_dir: Path | None) -> Grid:
        raise NotImplementedError


# =====================================================================================================
class GefsFamily(Family):
    key, label, name, org = "gefs", "GEFS", "NCEP GEFS", "NOAA/NCEP"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.members = ["gec00"] + [f"gep{i:02d}" for i in range(1, int(cfg.source.n_perturbed) + 1)]

    def max_fh(self, run):
        return max(p.max_fh for p in self.cfg.source.products)

    def plan(self, http, run, fh):
        return choose_product(http, self.cfg, run, fh)

    def plan_tag(self, plan):
        return plan.dir

    def _products(self, fh):
        return [p for p in self.cfg.source.products if fh <= p.max_fh]

    def cycle_ready(self, http, run, fhs):
        fh = self.hours(run, fhs)[-1]
        prods = self._products(fh)
        if not prods:
            return False
        p = prods[0]                                   # highest-resolution product: what frames are fetched from
        # cheap sentinel first (last member), then everyone
        if not http.exists(object_url(self.cfg, run, p, self.members[-1], fh) + ".idx"):
            return False
        return all(http.exists(object_url(self.cfg, run, p, m, fh) + ".idx") for m in self.members)

    def fetch_grid(self, http, run, plan, member, fh, cache_dir):
        url = object_url(self.cfg, run, plan, member, fh)
        cache = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{plan.dir}.f{fh:03d}.grib2" if cache_dir else None

        def fetch():
            entries = parse_idx(http.get_text(url + ".idx"))
            start, end = find_message(entries, self.cfg.source.variable, self.cfg.source.level)
            return http.get_range(url, start, end)

        return decode_message(_cached(cache, fetch), *_bbox(self.cfg))


# =====================================================================================================
class IfsFamily(Family):
    key, label, name, org = "ifs", "ECMWF IFS ENS", "ECMWF IFS ENS", "ECMWF"
    note = "ec00 is the IFS control (= HRES, stream oper); ep01-ep50 are the perturbed members"

    def __init__(self, cfg):
        super().__init__(cfg)
        c = cfg.ifs
        self.members = ["ec00"] + [f"ep{i:02d}" for i in range(1, int(c.n_perturbed) + 1)]
        self._idx: dict[str, dict] = {}                # index URL -> {(type, number): (offset, length)}
        self._idx_lock = threading.Lock()

    # -- layout ---------------------------------------------------------------------------------------
    def max_fh(self, run):
        c = self.cfg.ifs
        return int(c.full_max_fh) if run.hour in set(c.full_range_cycles) else int(c.short_max_fh)

    def _url(self, run: datetime, step: int, stream: str) -> str:
        c = self.cfg.ifs
        typ = "ef" if stream == "enfo" else "fc"
        return (f"{c.base_url}/{run:%Y%m%d}/{run:%H}z/{c.model}/{c.resolution}/{stream}/"
                f"{run:%Y%m%d}{run:%H}0000-{step}h-{stream}-{typ}.grib2")

    def plan(self, http, run, fh):
        ef = self._url(run, fh, "enfo")
        return {"enfo": ef, "oper": self._url(run, fh, "oper")} if http.exists(ef[:-5] + "index") else None

    def plan_tag(self, plan):
        return "enfo"

    def cycle_ready(self, http, run, fhs):
        fh = self.hours(run, fhs)[-1]
        return all(http.exists(self._url(run, fh, s)[:-5] + "index") for s in ("enfo", "oper"))

    # -- index (one per step, shared by all 50 members) -------------------------------------------------
    def _index(self, http: Http, grib_url: str) -> dict:
        url = grib_url[:-5] + "index"
        with self._idx_lock:                            # fetched once; other workers wait for it instead of repeating it
            if url not in self._idx:
                self._idx[url] = parse_json_index(http.get_text(url), self.cfg.ifs.param)
            return self._idx[url]

    def _locate(self, http, plan, member):
        """-> (grib_url, offset, length, stream)"""
        if member == "ec00":
            try:                                         # IFS >= 50r1: control = HRES forecast
                off, ln = self._index(http, plan["oper"])[("fc", "")]
                return plan["oper"], off, ln, "oper"
            except (NotFound, KeyError):                 # older cycles: control still inside the ENS file (type cf)
                off, ln = self._index(http, plan["enfo"])[("cf", "")]
                return plan["enfo"], off, ln, "enfo"
        number = str(int(member[2:]))
        off, ln = self._index(http, plan["enfo"])[("pf", number)]
        return plan["enfo"], off, ln, "enfo"

    def fetch_grid(self, http, run, plan, member, fh, cache_dir):
        tag = "oper" if member == "ec00" else "enfo"
        cache = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{tag}.f{fh:03d}.grib2" if cache_dir else None

        def fetch():
            url, off, ln, _ = self._locate(http, plan, member)
            return http.get_range(url, off, off + ln - 1)

        return decode_message(_cached(cache, fetch), *_bbox(self.cfg))


# =====================================================================================================
FAMILIES = {"gefs": GefsFamily, "ifs": IfsFamily}


def make_families(cfg, keys: list[str] | None = None) -> list[Family]:
    keys = list(keys or cfg.run.families)
    unknown = [k for k in keys if k not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown ensemble family: {unknown} (available: {sorted(FAMILIES)})")
    return [FAMILIES[k](cfg) for k in keys]


def resolve_cycle(http: Http, cfg, families: list[Family], fhs: list[int], now: datetime | None = None,
                  lookback: int = 8) -> datetime:
    """Newest allowed cycle that every enabled family has fully published (up to its own last hour)."""
    now = now or datetime.now(timezone.utc)
    allowed = set(int(c) for c in cfg.run.cycles)
    run = now.replace(minute=0, second=0, microsecond=0)
    run -= timedelta(hours=run.hour % 6)
    for _ in range(lookback):
        if run.hour in allowed:
            if all(f.cycle_ready(http, run, fhs) for f in families):
                return run
            log.info("cycle %s not complete yet", run.strftime("%Y%m%d%H"))
        run -= timedelta(hours=6)
    raise RuntimeError("no complete cycle found in the look-back window "
                       f"(families: {', '.join(f.key for f in families)}; allowed cycles: {sorted(allowed)})")
