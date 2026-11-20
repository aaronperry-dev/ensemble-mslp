"""Polite HTTP client for the public NOAA GEFS bucket.

Rate-limit / server-friendliness features
  * one limiter per host (requests per second), shared by all worker threads; each is ADAPTIVE: a 429 / 503 halves that host's
    rate (never below a floor) and it creeps back up to the configured ceiling after a run of clean replies
  * small, fixed number of keep-alive connections (thread-local sessions)
  * exponential backoff + jitter on 429 / 5xx (S3 "SlowDown" is a 503) and on network errors,
    honouring a Retry-After header when present
  * HTTP Range requests: only the bytes of the one GRIB2 message we need are transferred
    (~0.5 MB for 0.25-deg PRMSL instead of the ~13.5 MB file), located through the tiny .idx file
  * integrity checks on every ranged read (206, Content-Range, length, GRIB/7777 framing)
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("gefs.http")

RETRY_STATUS = {429, 500, 502, 503, 504}


class NotFound(Exception):
    """HTTP 404 (object not published, e.g. 0.25-deg beyond f240)."""


class FetchError(Exception):
    """Gave up after retries / unexpected response."""


class RateLimiter:
    """Spaces request *starts* at least 1/rate seconds apart, across threads.

    `rps` is the CEILING.  The rate in force starts there; penalize() (called on HTTP 429 / 503) halves it, at most once per
    `cooldown` seconds so a burst of simultaneous 503s counts as one signal, never below `floor` x ceiling; after
    `recover_after` consecutive clean replies ok() raises it by 15 % until the ceiling is reached again (AIMD)."""

    def __init__(self, rps: float, *, floor: float = 0.2, cooldown: float = 2.0, recover_after: int = 25, clock=time.monotonic):
        self.ceiling = float(rps) if rps and rps > 0 else 0.0
        self.rate = self.ceiling
        self.floor = self.ceiling * floor
        self.cooldown, self.recover_after, self._clock = cooldown, recover_after, clock
        self._lock = threading.Lock()
        self._next = 0.0
        self._clean = 0
        self._last_penalty = -1e9
        self.penalties = 0

    @property
    def interval(self) -> float:
        return 1.0 / self.rate if self.rate else 0.0

    def wait(self) -> None:
        with self._lock:
            if not self.rate:
                return
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + 1.0 / self.rate
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    def penalize(self) -> None:
        with self._lock:
            now = self._clock()
            self._clean = 0
            if not self.rate or now - self._last_penalty < self.cooldown:
                return
            self._last_penalty = now
            self.penalties += 1
            self.rate = max(self.floor, self.rate / 2)

    def ok(self) -> None:
        with self._lock:
            if not self.rate or self.rate >= self.ceiling:
                return
            self._clean += 1
            if self._clean >= self.recover_after:
                self._clean = 0
                self.rate = min(self.ceiling, self.rate * 1.15)


@dataclass
class Stats:
    requests: int = 0
    retries: int = 0
    bytes: int = 0
    not_found: int = 0

    def __post_init__(self):
        self._lock = threading.Lock()
        self.hosts: dict[str, dict[str, int]] = {}       # per-host breakdown: requests, ranged (206), whole (200), bytes, retries

    def add(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def add_host(self, host: str, **kw):
        with self._lock:
            h = self.hosts.setdefault(host, {"requests": 0, "ranged": 0, "whole": 0, "bytes": 0, "retries": 0})
            for k, v in kw.items():
                h[k] += v

    def summary(self) -> list[str]:
        """One line per host: requests, how many were byte-range (206) reads, bytes moved, retries."""
        out = []
        for host, h in sorted(self.hosts.items()):
            per = h["bytes"] / max(1, h["ranged"] + h["whole"])
            out.append(f"{host}: {h['requests']} requests ({h['ranged']} byte-range, {h['whole']} whole-file / inventory), "
                       f"{h['bytes'] / 1e6:.0f} MB, {per / 1e3:.0f} kB per read, {h['retries']} retries")
        return out


class MirrorGroup:
    """Several servers that hold byte-identical copies of the same files (e.g. ECMWF open data on AWS and on Google Cloud).

    pick() rotates over the servers that are currently healthy, so the load is shared and each server's own limit is
    easier to stay under.  A server that answers 429 / 5xx or drops the connection is put on a cool-down (30 s, doubling
    to `cooldown_max` while it keeps failing, cleared by one success) and the retry goes straight to another server instead
    of sleeping.  A server that answers 404 for a file is skipped for that request only (a copy may lag behind)."""

    def __init__(self, bases: list[str], *, fallback: list[str] = (), cooldown: float = 30.0, cooldown_max: float = 600.0,
                 clock=time.monotonic):
        """`fallback` servers (a subset of `bases`, or extra ones) are the last resort: used only while every other server is
        cooling down."""
        self.bases = [b.rstrip("/") for b in bases] + [b.rstrip("/") for b in fallback if b.rstrip("/") not in bases]
        self.fallback = {b.rstrip("/") for b in fallback}
        self.cooldown, self.cooldown_max, self._clock = cooldown, cooldown_max, clock
        self._lock = threading.Lock()
        self._until = {b: 0.0 for b in self.bases}
        self._fails = {b: 0 for b in self.bases}
        self._rr = 0
        self.failovers = 0

    def split(self, url: str) -> str | None:
        """The part of `url` after the matching base, or None when the URL belongs to no server of this group."""
        for b in self.bases:
            if url.startswith(b + "/"):
                return url[len(b):]
        return None

    def pick(self, avoid: set[str] = frozenset()) -> str:
        with self._lock:
            now = self._clock()
            live = [b for b in self.bases if b not in avoid]
            if not live:
                live = list(self.bases)
            healthy = [b for b in live if self._until[b] <= now]
            main = [b for b in healthy if b not in self.fallback]
            pool = main or healthy                              # a last-resort server is used only when no other one is healthy
            if pool:
                self._rr += 1
                return pool[self._rr % len(pool)]
            return min(live, key=lambda b: self._until[b])      # all cooling: the one that recovers first

    def healthy_alternative(self, base: str) -> bool:
        with self._lock:
            now = self._clock()
            return any(b != base and self._until[b] <= now for b in self.bases)

    def bad(self, base: str) -> None:
        with self._lock:
            self._fails[base] += 1
            self._until[base] = self._clock() + min(self.cooldown_max, self.cooldown * 2 ** (self._fails[base] - 1))
            self.failovers += 1

    def good(self, base: str) -> None:
        with self._lock:
            self._fails[base] = 0
            self._until[base] = 0.0


class Http:
    def __init__(self, *, user_agent: str, rps: float = 8, max_retries: int = 6, backoff_base: float = 1.0,
                 backoff_max: float = 60.0, timeout: tuple[float, float] = (10, 60), sleep=time.sleep,
                 host_rps: dict[str, float] | None = None, adaptive: bool = True,
                 mirrors: list[list[str]] | None = None):
        self.user_agent = user_agent
        self.limiter = RateLimiter(rps)
        # optional gentler limit for particular hosts (substring of the hostname), e.g. {"ecmwf-forecasts": 3}
        self.host_limiters = {h: RateLimiter(r, **({} if adaptive else {"floor": 1.0})) for h, r in (host_rps or {}).items()}
        if not adaptive:
            self.limiter = RateLimiter(rps, floor=1.0)
        self.mirror_groups = [g if isinstance(g, MirrorGroup) else MirrorGroup(g) for g in (mirrors or []) if len(g if not isinstance(g, MirrorGroup) else g.bases) > 1]
        self.aborted: set = set()                  # job contexts (family, hour) or families (str) whose requests must stop
        self.abort_all = False
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.timeout = timeout
        self.stats = Stats()
        self._local = threading.local()
        self._sleep = sleep  # injectable for tests

    # -- session handling ------------------------------------------------------------------------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": self.user_agent, "Accept-Encoding": "identity"})
            adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._local.s = s
        return s

    # -- core request with backoff ---------------------------------------------------------------
    def _backoff(self, attempt: int, retry_after: str | None, why: str, url: str) -> None:
        delay = min(self.backoff_max, self.backoff_base * (2 ** attempt))
        delay = random.uniform(delay / 2, delay)  # jitter de-synchronises the workers
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        self.stats.add(retries=1)
        self.stats.add_host(self._route(url)[0], retries=1)
        log.warning("%s on %s - retry %d/%d in %.1fs", why, url.rsplit("/", 1)[-1], attempt + 1, self.max_retries, delay)
        if self._sleep is not time.sleep:
            self._sleep(delay)
            return
        end = time.monotonic() + delay                     # sleep in slices so that an abandoned job stops waiting at once
        while (left := end - time.monotonic()) > 0:
            self._check_abort()
            time.sleep(min(1.0, left))

    def _limiter_for(self, url: str) -> RateLimiter:
        return self._route(url)[1]

    def _route(self, url: str) -> tuple[str, RateLimiter]:
        """(label, limiter) for a URL: the configured host whose name occurs in the hostname, else the default limiter."""
        netloc = url.split("/", 3)[2]
        for host, lim in self.host_limiters.items():
            if host in netloc:
                return host, lim
        return netloc, self.limiter

    def set_context(self, ctx) -> None:
        """Tag the calling thread's requests with a job context, e.g. ("aifs", 120), so abort() can stop them."""
        self._local.ctx = ctx

    def abort(self, ctx) -> None:
        """Make requests of that job context (a (family, hour) pair) or of that whole family (a string) fail at once."""
        self.aborted.add(ctx)

    def _check_abort(self) -> None:
        ctx = getattr(self._local, "ctx", None)
        if ctx is not None and (self.abort_all or ctx in self.aborted or ctx[0] in self.aborted):
            raise FetchError(f"abandoned: {ctx[0]} f{ctx[1]:03d} was skipped (too slow)")

    def _group(self, url: str) -> tuple[MirrorGroup | None, str]:
        for g in self.mirror_groups:
            rel = g.split(url)
            if rel is not None:
                return g, rel
        return None, ""

    def request(self, method: str, url: str, *, headers: dict | None = None, ok=(200, 206)) -> requests.Response:
        group, rel = self._group(url)
        missing: set[str] = set()                    # mirrors that answered 404 for this very request
        for attempt in range(self.max_retries + 1):
            self._check_abort()
            base = ""
            if group is not None:
                base = group.pick(missing)
                url = base + rel
            label, limiter = self._route(url)
            limiter.wait()
            self.stats.add(requests=1)
            self.stats.add_host(label, requests=1)
            try:
                r = self._session().request(method, url, headers=headers, timeout=self.timeout, stream=True)
                if r.status_code in ok:
                    if method != "HEAD":
                        _ = r.content  # read body now (inside the retry scope)
                    n = len(r.content) if method != "HEAD" else 0
                    self.stats.add(bytes=n)
                    self.stats.add_host(label, bytes=n, **({"ranged": 1} if r.status_code == 206 else {"whole": 1} if method != "HEAD" else {}))
                    limiter.ok()
                    if group is not None:
                        group.good(base)
                    return r
                status, retry_after = r.status_code, r.headers.get("Retry-After")
                r.close()
                if status in (429, 503):
                    limiter.penalize()                      # the server is asking us to slow down: halve this host's rate
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
                if attempt >= self.max_retries:
                    raise FetchError(f"{method} {url}: {type(e).__name__}: {e}") from e
                if group is not None:
                    group.bad(base)
                    if group.healthy_alternative(base):
                        log.warning("%s on %s - trying another mirror", type(e).__name__, url.rsplit("/", 1)[-1])
                        self.stats.add(retries=1)
                        self.stats.add_host(label, retries=1)
                        continue
                self._backoff(attempt, None, type(e).__name__, url)
                continue
            if status == 404:
                if group is not None:
                    missing.add(base)
                    if len(missing) < len(group.bases):     # another copy may already have it: ask that one
                        continue
                self.stats.add(not_found=1)
                raise NotFound(url)
            if status in RETRY_STATUS and attempt < self.max_retries:
                if group is not None:
                    group.bad(base)
                    if group.healthy_alternative(base):     # no need to sleep: another server can take this request now
                        log.warning("HTTP %d on %s - trying another mirror", status, url.rsplit("/", 1)[-1])
                        self.stats.add(retries=1)
                        self.stats.add_host(label, retries=1)
                        continue
                self._backoff(attempt, retry_after, f"HTTP {status}", url)
                continue
            if group is not None and attempt < self.max_retries and group.healthy_alternative(base):
                group.bad(base)                              # e.g. a server that ignores Range or refuses us: use another one
                log.warning("HTTP %d on %s from %s - trying another mirror", status, url.rsplit("/", 1)[-1], url.split("/", 3)[2])
                continue
            raise FetchError(f"{method} {url}: HTTP {status}")
        raise FetchError(f"{method} {url}: retries exhausted")

    # -- helpers ---------------------------------------------------------------------------------
    def exists(self, url: str) -> bool:
        try:
            self.request("HEAD", url, ok=(200,))
            return True
        except NotFound:
            return False

    def get_text(self, url: str) -> str:
        return self.request("GET", url, ok=(200,)).text

    def get_bytes(self, url: str) -> bytes:
        """Download a whole (small) object, e.g. the ~2 MB GEPS all-member file; verifies the length when announced."""
        r = self.request("GET", url, ok=(200,))
        data = r.content
        cl = r.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) != len(data):
            raise FetchError(f"short read for {url}: {len(data)} of {cl} B")
        return data

    def get_range(self, url: str, start: int, end: int | None = None) -> bytes:
        """Fetch bytes [start, end] (inclusive; open-ended if end is None) and validate the reply."""
        rng = f"bytes={start}-{'' if end is None else end}"
        r = self.request("GET", url, headers={"Range": rng}, ok=(206,))
        data = r.content
        m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", r.headers.get("Content-Range", ""))
        if not m or int(m.group(1)) != start or int(m.group(2)) - start + 1 != len(data):
            raise FetchError(f"bad Content-Range for {url}: {r.headers.get('Content-Range')} (got {len(data)} B)")
        if end is not None and int(m.group(2)) > end:
            raise FetchError(f"server returned more bytes than requested for {url}")
        return data


# ---- GRIB2 .idx parsing -----------------------------------------------------------------------------
@dataclass(frozen=True)
class IdxEntry:
    num: int
    offset: int
    var: str
    level: str
    step: str


def parse_idx(text: str) -> list[IdxEntry]:
    """Parse wgrib2-style inventory lines:  `26:12999061:d=2026111806:PRMSL:mean sea level:anl:ENS=+1`."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        out.append(IdxEntry(int(parts[0]), int(parts[1]), parts[3], parts[4], parts[5]))
    return out


def find_message(entries: list[IdxEntry], var: str, level: str) -> tuple[int, int | None]:
    """Byte range [start, end] of the first matching message; end is None if it is the last message."""
    for i, e in enumerate(entries):
        if e.var == var and e.level == level:
            nxt = entries[i + 1].offset - 1 if i + 1 < len(entries) else None
            return e.offset, nxt
    raise KeyError(f"{var}:{level} not in inventory")


# ---- ECMWF open-data .index (one JSON object per line) ------------------------------------------------
def parse_json_index(text: str, param: str) -> dict[tuple[str, str], tuple[int, int]]:
    """Reduce an ECMWF `.index` file to {(type, number): (offset, length)} for one parameter.

    The index of a 0.25-deg ENS step lists ~8 850 messages (2-3 MB); only the handful we need is kept.
    `number` is "" for messages without a member number (control / deterministic forecasts).
    """
    out: dict[tuple[str, str], tuple[int, int]] = {}
    needle = f'"{param}"'
    for line in text.splitlines():
        if needle not in line:               # cheap pre-filter before json parsing
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("param") != param or "_offset" not in row or "_length" not in row:
            continue
        out.setdefault((str(row.get("type", "")), str(row.get("number", ""))), (int(row["_offset"]), int(row["_length"])))
    return out


def parse_json_index_params(text: str, params: tuple[str, ...]) -> dict[str, dict[tuple[str, str], tuple[int, int]]]:
    """Like parse_json_index, for several parameters at once -> {param: {(type, number): (offset, length)}}.
    One pass over the (2-3 MB) index serves MSLP and the wind fields together."""
    out: dict[str, dict[tuple[str, str], tuple[int, int]]] = {p: {} for p in params}
    needles = tuple(f'"{p}"' for p in params)
    for line in text.splitlines():
        if not any(n in line for n in needles):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        p = row.get("param")
        if p in out and "_offset" in row and "_length" in row:
            out[p].setdefault((str(row.get("type", "")), str(row.get("number", ""))), (int(row["_offset"]), int(row["_length"])))
    return out


def find_ranges(entries: list[IdxEntry], wanted: list[tuple[str, str]]) -> list[tuple[int, int | None, list[int]]]:
    """Byte ranges that cover the wanted (var, level) messages with as few requests as possible.

    Messages that sit next to each other in the file (GEFS: UGRD then VGRD at 10 m) are merged into one range.
    Returns [(start, end, [indices into `wanted` in file order])]; end is None for the last message of the file.
    Raises KeyError when a wanted message is not in the inventory."""
    pos = []
    for w in wanted:
        for i, e in enumerate(entries):
            if e.var == w[0] and e.level == w[1]:
                pos.append(i)
                break
        else:
            raise KeyError(f"{w[0]}:{w[1]} not in inventory")
    order = sorted(range(len(wanted)), key=lambda k: pos[k])
    runs: list[list[int]] = []
    for k in order:
        if runs and pos[k] == pos[runs[-1][-1]] + 1:
            runs[-1].append(k)
        else:
            runs.append([k])
    out = []
    for run in runs:
        first, last = pos[run[0]], pos[run[-1]]
        end = entries[last + 1].offset - 1 if last + 1 < len(entries) else None
        out.append((entries[first].offset, end, run))
    return out


def plan_parts(entries: list[IdxEntry], wanted: dict[str, tuple[str, str]], max_gap: int = 0):
    """Plan the byte-range requests for several messages of ONE file: `wanted` = {part name: (var, level)}.

    Messages that are next to each other, or separated by at most `max_gap` bytes of messages we do not want, share one request:
    one request costs far more of the rate limit than a few hundred kB of extra bytes.  Returns
    (ranges, missing): ranges = [(start, end | None, [(part, offset inside the range, length | None)])], in file order
    (end / length are None for the last message of the file); missing = parts that are not in the inventory."""
    pos: dict[str, int] = {}
    missing: list[str] = []
    for part, (var, level) in wanted.items():
        for i, e in enumerate(entries):
            if e.var == var and e.level == level:
                pos[part] = i
                break
        else:
            missing.append(part)
    groups: list[list[str]] = []
    for part in sorted(pos, key=pos.get):
        if groups:
            j = pos[groups[-1][-1]]
            prev_end = entries[j + 1].offset if j + 1 < len(entries) else None      # first byte after the previous wanted message
            if prev_end is not None and entries[pos[part]].offset - prev_end <= max_gap:
                groups[-1].append(part)
                continue
        groups.append([part])
    ranges = []
    for g in groups:
        first = entries[pos[g[0]]].offset
        last = pos[g[-1]]
        end = entries[last + 1].offset - 1 if last + 1 < len(entries) else None
        parts = []
        for part in g:
            i = pos[part]
            parts.append((part, entries[i].offset - first, entries[i + 1].offset - entries[i].offset if i + 1 < len(entries) else None))
        ranges.append((first, end, parts))
    return ranges, missing


def fetch_parts(http: Http, url: str, entries: list[IdxEntry], wanted: dict[str, tuple[str, str]], max_gap: int = 0):
    """Download the wanted messages of one file with as few Range requests as plan_parts() allows.
    Returns ({part: raw GRIB message bytes}, [parts missing from the inventory])."""
    ranges, missing = plan_parts(entries, wanted, max_gap)
    out: dict[str, bytes] = {}
    for start, end, parts in ranges:
        data = http.get_range(url, start, end)
        for part, off, ln in parts:
            blob = data[off:] if ln is None else data[off:off + ln]
            if (ln is not None and len(blob) != ln) or blob[:4] != b"GRIB":
                raise FetchError(f"bad message slice for {part} in {url}")
            out[part] = blob
    return out, missing
