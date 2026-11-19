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

  * GepsFamily  - Canadian GEPS (ECCC), MSC Datamart     (21 members: cc00 + cp01..cp20), 0.5 deg, 3-hourly to 192 h,
                  6-hourly to 384 h on 00/12Z.  One all-member GRIB2 file per step:
  <base>/<HH>/<hhh>/CMC_geps-raw_PRMSL_MSL_0_latlon0p5x0p5_<YYYYMMDDHH>_P<hhh>_allmbrs.grib2   (~2.2 MB)
  with <base> = https://dd.weather.gc.ca/<YYYYMMDD>/WXO-DD/ensemble/geps/grib2/raw  (a `today/` form also exists).

  * AigefsFamily - NOAA AIGEFS (AI ensemble, GraphCast-based), s3://noaa-nws-graphcastgfs-pds  (31 members mem000..mem030),
                  0.25 deg, 6-hourly to 384 h, all four cycles.  Same .idx + Range scheme as GEFS:
  EAGLE_ensemble/aigefs.<YYYYMMDD>/<HH>/mem<NNN>/model/atmos/grib2/aigefs.t<HH>z.sfc.f<FFF>.grib2 (+ .idx; PRMSL:mean sea level)
  Publication is irregular (7.5 h to > 24 h after cycle time), so it is configured as an *optional* family (see [run]).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .decode import DecodeError, Grid, decode_message, member_key, speed_from_uv, split_messages
from .fetcher import FetchError, Http, NotFound, fetch_parts, find_message, find_ranges, parse_idx, parse_json_index, parse_json_index_params
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


class _Inventories:
    """Parsed .idx inventories by URL.  MSLP and wind of one member-hour read the same file, so it is fetched once
    (single-flight: a second worker asking for the same inventory waits for the first instead of requesting it again)."""

    def __init__(self):
        self._d: dict[str, list] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def get(self, http: Http, url: str) -> list:
        with self._lock:
            hit = self._d.get(url)
            if hit is not None:
                return hit
            gate = self._locks.setdefault(url, threading.Lock())
        with gate:
            with self._lock:
                hit = self._d.get(url)
            if hit is None:
                hit = parse_idx(http.get_text(url + ".idx"))
                with self._lock:
                    if len(self._d) > 4000:                   # a build touches < 5k (member, hour) files; keep it bounded anyway
                        self._d.clear()
                        self._locks.clear()
                    self._d[url] = hit
        return hit


@dataclass
class MemberResult:
    """One member-hour: the MSLP grid, plus its wind / gust grids (or why they are missing)."""
    grid: Grid
    wind: dict | None = None            # {"wind": Grid mph, "gust": Grid mph}
    wind_error: Exception | None = None


def _store(cache: Path | None, data: bytes) -> None:
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, cache)


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

    # -- 10 m wind and gust (mph) -----------------------------------------------------------------------
    has_wind = False                                    # the system publishes 10 m wind
    has_gust = False                                    # ... and a 10 m gust

    def wind_blobs(self, http: Http, run: datetime, plan, member: str, fh: int, cache_dir: Path | None) -> dict:
        """Raw GRIB bytes of one member's wind: {"uv": u+v messages | "speed": one speed message, "gust": one gust message}.
        A missing gust is simply left out; a missing wind raises (the pipeline then publishes no wind for that hour)."""
        raise NotImplementedError

    def fetch_member(self, http: Http, run: datetime, plan, member: str, fh: int, cache_dir: Path | None,
                     want_wind: bool = False) -> MemberResult:
        """MSLP (+ wind and gust when wanted) of one member and hour.  A wind failure never costs the member its MSLP.
        Systems whose files hold several fields in one inventory (GEFS, AIGEFS) override this to share requests."""
        grid = self.fetch_grid(http, run, plan, member, fh, cache_dir)
        if not want_wind:
            return MemberResult(grid)
        try:
            return MemberResult(grid, self.fetch_wind(http, run, plan, member, fh, cache_dir))
        except (NotFound, FetchError, DecodeError, KeyError) as e:
            return MemberResult(grid, None, e)

    def fetch_wind(self, http: Http, run: datetime, plan, member: str, fh: int, cache_dir: Path | None) -> dict[str, Grid]:
        """-> {"wind": Grid mph, "gust": Grid mph (when published)} on the member's native grid, cut to the domain."""
        return self.decode_wind(self.wind_blobs(http, run, plan, member, fh, cache_dir))

    def decode_wind(self, b: dict) -> dict[str, Grid]:
        box = _bbox(self.cfg)
        out: dict[str, Grid] = {}
        if b.get("speed"):
            out["wind"] = decode_message(b["speed"], *box, field="speed")
        elif b.get("uv"):
            msgs = split_messages(b["uv"])
            if len(msgs) != 2:
                raise DecodeError(f"expected the u and v messages, found {len(msgs)}")
            out["wind"] = speed_from_uv(decode_message(msgs[0], *box, field="speed"), decode_message(msgs[1], *box, field="speed"))
        else:
            raise DecodeError("no wind message")
        if b.get("gust"):
            out["gust"] = decode_message(b["gust"], *box, field="speed")
        return out


# =====================================================================================================
class GefsFamily(Family):
    key, label, name, org = "gefs", "GEFS", "NCEP GEFS", "NOAA/NCEP"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.members = ["gec00"] + [f"gep{i:02d}" for i in range(1, int(cfg.source.n_perturbed) + 1)]
        self._inv = _Inventories()

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
            start, end = find_message(self._inv.get(http, url), self.cfg.source.variable, self.cfg.source.level)
            return http.get_range(url, start, end)

        return decode_message(_cached(cache, fetch), *_bbox(self.cfg))

    has_wind = True
    has_gust = True

    def fetch_member(self, http, run, plan, member, fh, cache_dir, want_wind=False):
        url = object_url(self.cfg, run, plan, member, fh)
        base = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{plan.dir}.f{fh:03d}" if cache_dir else None
        raw, blobs, werr = _idx_member(self, http, url, base, want_wind, self.cfg.wind.gefs if want_wind else None,
                                       self.cfg.source.variable, self.cfg.source.level)
        return _member_result(self, raw, blobs, werr, want_wind)

    def wind_blobs(self, http, run, plan, member, fh, cache_dir):
        w = self.cfg.wind.gefs
        url = object_url(self.cfg, run, plan, member, fh)
        base = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{plan.dir}.f{fh:03d}" if cache_dir else None
        return _idx_wind_blobs(lambda: self._inv.get(http, url), http, url, w, base)


def _idx_member(fam, http: Http, url: str, base: Path | None, want_wind: bool, wcfg, pvar: str, plevel: str):
    """NCEP-style file (GEFS, AIGEFS) with a .idx: ONE inventory read, then the fewest Range requests that cover the MSLP
    message and, when wanted, the 10 m u / v messages and the gust.  Messages that are adjacent (GEFS u + v) or separated by
    less than `[fetch] merge_gap_kb` (AIGEFS: u, v, a 2 m temperature, MSLP, all within ~4 MB) share a request; GEFS's
    gust, winds and MSLP sit tens of MB apart, so they stay separate.  Parts already on disk are not requested again.
    -> (MSLP message bytes, {"uv", "gust"} wind blobs or None, wind error or None)"""
    max_gap = int(getattr(fam.cfg.fetch, "merge_gap_kb", 1024)) * 1024
    pc = base.parent / f"{base.name}.grib2" if base is not None else None
    uvc, gc = _sib(base, "uv"), _sib(base, "gust")
    have = lambda c: c is not None and c.exists()
    gust_cfg = bool(want_wind and getattr(wcfg, "gust", None))
    wanted: dict[str, tuple[str, str]] = {}
    if not have(pc):
        wanted["p"] = (pvar, plevel)
    if want_wind and not have(uvc):
        wanted["u"], wanted["v"] = (wcfg.u, wcfg.level), (wcfg.v, wcfg.level)
    if gust_cfg and not have(gc):
        wanted["g"] = (wcfg.gust, wcfg.gust_level)
    got, missing = ({}, [])
    if wanted:
        got, missing = fetch_parts(http, url, fam._inv.get(http, url), wanted, max_gap)
    if "p" in missing:
        raise KeyError(f"{pvar}:{plevel} not in inventory")
    if "p" in got:
        _store(pc, got["p"])
    raw = got["p"] if "p" in got else pc.read_bytes()
    if not want_wind:
        return raw, None, None
    blobs: dict = {}
    werr = None
    if uvc is not None and uvc.exists() and "u" not in got:
        blobs["uv"] = uvc.read_bytes()
    elif "u" in got and "v" in got:
        blobs["uv"] = got["u"] + got["v"]
        _store(uvc, blobs["uv"])
    else:
        werr = KeyError(f"{wcfg.u}/{wcfg.v}:{wcfg.level} not in inventory")
    if werr is None and gust_cfg:
        if "g" in got:
            blobs["gust"] = got["g"]
            _store(gc, got["g"])
        elif have(gc):
            blobs["gust"] = gc.read_bytes()
    return raw, (blobs if werr is None else None), werr


def _member_result(fam, raw: bytes, blobs, werr, want_wind: bool) -> MemberResult:
    grid = decode_message(raw, *_bbox(fam.cfg))
    if not want_wind:
        return MemberResult(grid)
    if werr is None:
        try:
            return MemberResult(grid, fam.decode_wind(blobs))
        except (DecodeError, KeyError) as e:
            werr = e
    return MemberResult(grid, None, werr)


def _sib(base: Path | None, tag: str) -> Path | None:
    """Sibling cache file `<base>.<tag>.grib2` (base already ends in `.fNNN`, so with_suffix() would clobber it)."""
    return base.parent / f"{base.name}.{tag}.grib2" if base is not None else None


def _idx_wind_blobs(entries_fn, http: Http, url: str, w, base: Path | None) -> dict:
    """NCEP-style .idx + byte ranges (GEFS, AIGEFS): the 10 m u and v components (adjacent messages, so ONE request) and,
    when configured and present, the gust."""
    def uv():
        got = {}
        for start, end, ks in find_ranges(entries_fn(), [(w.u, w.level), (w.v, w.level)]):
            for k, m in zip(ks, split_messages(http.get_range(url, start, end))):
                got[k] = m
        return got[0] + got[1]                           # u first, then v

    out = {"uv": _cached(_sib(base, "uv"), uv)}
    if getattr(w, "gust", None):
        def gust():
            (start, end, _), = find_ranges(entries_fn(), [(w.gust, w.gust_level)])
            return http.get_range(url, start, end)
        try:
            out["gust"] = _cached(_sib(base, "gust"), gust)
        except (KeyError, NotFound):
            pass                                         # this file has no gust
    return out


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
    def _params(self) -> tuple[str, ...]:
        w = getattr(self.cfg, "wind", None)
        extra = (w.ifs.u, w.ifs.v, *w.ifs.gust) if w is not None and hasattr(w, "ifs") else ()
        return (self.cfg.ifs.param, *extra)

    def _index(self, http: Http, grib_url: str) -> dict:
        """{param: {(type, number): (offset, length)}} for MSLP and the wind parameters (one download per step)."""
        url = grib_url[:-5] + "index"
        with self._idx_lock:                            # fetched once; other workers wait for it instead of repeating it
            if url not in self._idx:
                self._idx[url] = parse_json_index_params(http.get_text(url), self._params())
            return self._idx[url]

    def _locate(self, http, plan, member, param=None):
        """-> (grib_url, offset, length, stream)"""
        param = param or self.cfg.ifs.param
        if member == "ec00":
            try:                                         # IFS >= 50r1: control = HRES forecast
                off, ln = self._index(http, plan["oper"])[param][("fc", "")]
                return plan["oper"], off, ln, "oper"
            except (NotFound, KeyError):                 # older cycles: control still inside the ENS file (type cf)
                off, ln = self._index(http, plan["enfo"])[param][("cf", "")]
                return plan["enfo"], off, ln, "enfo"
        number = str(int(member[2:]))
        off, ln = self._index(http, plan["enfo"])[param][("pf", number)]
        return plan["enfo"], off, ln, "enfo"

    def fetch_grid(self, http, run, plan, member, fh, cache_dir):
        tag = "oper" if member == "ec00" else "enfo"
        cache = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{tag}.f{fh:03d}.grib2" if cache_dir else None

        def fetch():
            url, off, ln, _ = self._locate(http, plan, member)
            return http.get_range(url, off, off + ln - 1)

        return decode_message(_cached(cache, fetch), *_bbox(self.cfg))

    has_wind = True
    has_gust = True

    def wind_blobs(self, http, run, plan, member, fh, cache_dir):
        """10u and 10v (two ranges, side by side in the result) and the gust.  The gust is the maximum since the previous
        post-processing step, named 10fg (10fg3 at some steps up to 144 h); it is all zeros at step 0, so it is skipped there."""
        w = self.cfg.wind.ifs
        tag = "oper" if member == "ec00" else "enfo"
        base = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{tag}.f{fh:03d}" if cache_dir else None

        def one(param):
            url, off, ln, _ = self._locate(http, plan, member, param)
            return http.get_range(url, off, off + ln - 1)

        out = {"uv": _cached(_sib(base, "uv"), lambda: one(w.u) + one(w.v))}
        if fh > 0:
            def gust():
                for param in w.gust:
                    try:
                        return one(param)
                    except KeyError:
                        continue
                raise KeyError("no gust parameter in the index")
            try:
                out["gust"] = _cached(_sib(base, "gust"), gust)
            except (KeyError, NotFound):
                pass
        return out


# =====================================================================================================
class GepsFamily(Family):
    """Canadian GEPS (Environment and Climate Change Canada), MSC Datamart.

    One GRIB2 file per forecast hour holds every member (`..._allmbrs.grib2`, ~2 MB for MSLP), so the whole file is
    downloaded once per step and split into members; there is no index / byte-range step."""
    key, label, name, org = "geps", "CMC GEPS", "Canadian GEPS", "ECCC"
    note = "cc00 is the control member; cp01-cp20 are the perturbed members (member order follows the GRIB ensemble keys)"

    def __init__(self, cfg):
        super().__init__(cfg)
        c = cfg.geps
        self.members = ["cc00"] + [f"cp{i:02d}" for i in range(1, int(c.n_perturbed) + 1)]
        self._steps: dict[int, dict] = {}              # fh -> {member: Grid}; filled once per step, shared by all member jobs
        self._wsteps: dict[int, dict] = {}             # fh -> {member: raw wind-speed message}
        self._lock = threading.Lock()

    def max_fh(self, run):
        c = self.cfg.geps
        return int(c.max_fh) if run.hour in set(c.cycles) else int(c.short_max_fh)

    def _url(self, run: datetime, fh: int, variable: str | None = None) -> str:
        c = self.cfg.geps
        base = c.base_url.replace("{date}", f"{run:%Y%m%d}").rstrip("/")
        return (f"{base}/{run:%H}/{fh:03d}/CMC_geps-raw_{variable or c.variable}_{c.grid}_{run:%Y%m%d%H}_P{fh:03d}_allmbrs.grib2")

    def plan(self, http, run, fh):
        url = self._url(run, fh)
        return url if http.exists(url) else None

    def plan_tag(self, plan):
        return "allmbrs"

    def cycle_ready(self, http, run, fhs):
        hrs = self.hours(run, fhs)
        return bool(hrs) and http.exists(self._url(run, hrs[-1])) and http.exists(self._url(run, hrs[0]))

    def _step_grids(self, http, run, url, fh, cache_dir) -> dict:
        with self._lock:                                # first worker downloads + decodes; the others wait and reuse it
            if fh in self._steps:
                return self._steps[fh]
            cache = cache_dir / f"{run:%Y%m%d%H}" / f"geps.f{fh:03d}.grib2" if cache_dir else None
            raw = _cached(cache, lambda: http.get_bytes(url))
            msgs = split_messages(raw)
            keyed = sorted(range(len(msgs)), key=lambda i: (member_key(msgs[i]), i))   # control first, then by number
            if len(msgs) != len(self.members):
                log.warning("f%03d geps: file has %d messages, expected %d members", fh, len(msgs), len(self.members))
            grids = {}
            for name, i in zip(self.members, keyed):
                grids[name] = decode_message(msgs[i], *_bbox(self.cfg))
            self._steps[fh] = grids
            return grids

    def fetch_grid(self, http, run, plan, member, fh, cache_dir):
        return self._step_grids(http, run, plan, fh, cache_dir)[member]

    has_wind = True                                     # WIND_TGL_10 (speed) is in the raw files; there is no raw gust

    def wind_blobs(self, http, run, plan, member, fh, cache_dir):
        with self._lock:
            if fh not in self._wsteps:
                url = self._url(run, fh, self.cfg.wind.geps.speed)
                cache = cache_dir / f"{run:%Y%m%d%H}" / f"geps.wind.f{fh:03d}.grib2" if cache_dir else None
                msgs = split_messages(_cached(cache, lambda: http.get_bytes(url)))
                keyed = sorted(range(len(msgs)), key=lambda i: (member_key(msgs[i]), i))     # same member order as the MSLP file
                self._wsteps[fh] = {name: msgs[i] for name, i in zip(self.members, keyed)}
            return {"speed": self._wsteps[fh][member]}


# =====================================================================================================
class AigefsFamily(Family):
    """NOAA's AI Global Ensemble Forecast System (GraphCast-derived, initialised from the GEFS analyses)."""
    key, label, name, org = "aigefs", "NOAA AIGEFS", "NOAA AIGEFS (AI)", "NOAA/NCEP"
    note = "AI-based 31-member ensemble; mem000 is member 0 (ai00), mem001-mem030 are ai01-ai30"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.members = [f"ai{i:02d}" for i in range(0, int(cfg.aigefs.n_members))]
        self._inv = _Inventories()

    def max_fh(self, run):
        return int(self.cfg.aigefs.max_fh)

    def _url(self, run: datetime, member: str, fh: int) -> str:
        c = self.cfg.aigefs
        mem = f"mem{int(member[2:]):03d}"
        return (f"{c.base_url.rstrip('/')}/{c.prefix}.{run:%Y%m%d}/{run:%H}/{mem}/model/atmos/grib2/"
                f"aigefs.t{run:%H}z.{c.product}.f{fh:03d}.grib2")

    def plan(self, http, run, fh):
        url = self._url(run, self.members[0], fh)
        return url if http.exists(url + ".idx") else None

    def plan_tag(self, plan):
        return self.cfg.aigefs.product

    def cycle_ready(self, http, run, fhs):
        hrs = self.hours(run, fhs)
        if not hrs:
            return False
        fh = hrs[-1]
        if not http.exists(self._url(run, self.members[-1], fh) + ".idx"):       # cheap sentinel: last member, last hour
            return False
        return all(http.exists(self._url(run, m, fh) + ".idx") for m in self.members)

    def fetch_grid(self, http, run, plan, member, fh, cache_dir):
        url = self._url(run, member, fh)
        cache = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{self.cfg.aigefs.product}.f{fh:03d}.grib2" if cache_dir else None

        def fetch():
            start, end = find_message(self._inv.get(http, url), self.cfg.aigefs.variable, self.cfg.aigefs.level)
            return http.get_range(url, start, end)

        return decode_message(_cached(cache, fetch), *_bbox(self.cfg))

    has_wind = True                                     # UGRD / VGRD at 10 m are in the sfc file; the AI model has no gust

    def fetch_member(self, http, run, plan, member, fh, cache_dir, want_wind=False):
        url = self._url(run, member, fh)
        base = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{self.cfg.aigefs.product}.f{fh:03d}" if cache_dir else None
        raw, blobs, werr = _idx_member(self, http, url, base, want_wind, self.cfg.wind.aigefs if want_wind else None,
                                       self.cfg.aigefs.variable, self.cfg.aigefs.level)
        return _member_result(self, raw, blobs, werr, want_wind)

    def wind_blobs(self, http, run, plan, member, fh, cache_dir):
        url = self._url(run, member, fh)
        base = cache_dir / f"{run:%Y%m%d%H}" / f"{member}.{self.cfg.aigefs.product}.f{fh:03d}" if cache_dir else None
        return _idx_wind_blobs(lambda: self._inv.get(http, url), http, url, self.cfg.wind.aigefs, base)


# =====================================================================================================
FAMILIES = {"gefs": GefsFamily, "ifs": IfsFamily, "geps": GepsFamily, "aigefs": AigefsFamily}


def make_families(cfg, keys: list[str] | None = None) -> list[Family]:
    keys = list(keys or cfg.run.families)
    unknown = [k for k in keys if k not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown ensemble family: {unknown} (available: {sorted(FAMILIES)})")
    return [FAMILIES[k](cfg) for k in keys]


def optional_keys(cfg) -> set[str]:
    return set(getattr(cfg.run, "optional", []) or [])


def select_families(http: Http, cfg, families: list[Family], run: datetime, fhs: list[int]) -> list[Family]:
    """The families to build for `run`: every required one, plus each OPTIONAL one that is fully published by now."""
    opt = optional_keys(cfg)
    out = []
    for f in families:
        if f.key in opt and not f.cycle_ready(http, run, fhs):
            log.warning("optional family %s is not fully published for %s yet - building without it", f.key, run.strftime("%Y%m%d%H"))
            continue
        out.append(f)
    return out


def resolve_cycle(http: Http, cfg, families: list[Family], fhs: list[int], now: datetime | None = None,
                  lookback: int = 8) -> datetime:
    """Newest allowed cycle that every REQUIRED family has fully published (up to its own last hour).

    Families listed in [run] optional never hold a cycle back (AIGEFS is published irregularly); they are added
    to the build when they are ready (select_families) and trigger a rebuild of the live cycle when they arrive late."""
    now = now or datetime.now(timezone.utc)
    optional = optional_keys(cfg)
    families = [f for f in families if f.key not in optional] or families
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
