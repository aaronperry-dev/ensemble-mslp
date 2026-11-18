"""Polite HTTP client for the public NOAA GEFS bucket.

Rate-limit / server-friendliness features
  * one global token-bucket style limiter (requests per second) shared by all worker threads
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
    """Spaces request *starts* at least 1/rps seconds apart, across threads."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps and rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


@dataclass
class Stats:
    requests: int = 0
    retries: int = 0
    bytes: int = 0
    not_found: int = 0

    def __post_init__(self):
        self._lock = threading.Lock()

    def add(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)


class Http:
    def __init__(self, *, user_agent: str, rps: float = 8, max_retries: int = 6, backoff_base: float = 1.0,
                 backoff_max: float = 60.0, timeout: tuple[float, float] = (10, 60), sleep=time.sleep,
                 host_rps: dict[str, float] | None = None):
        self.user_agent = user_agent
        self.limiter = RateLimiter(rps)
        # optional gentler limit for particular hosts (substring of the hostname), e.g. {"ecmwf-forecasts": 3}
        self.host_limiters = {h: RateLimiter(r) for h, r in (host_rps or {}).items()}
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
        log.warning("%s on %s - retry %d/%d in %.1fs", why, url.rsplit("/", 1)[-1], attempt + 1, self.max_retries, delay)
        self._sleep(delay)

    def _limiter_for(self, url: str) -> RateLimiter:
        for host, lim in self.host_limiters.items():
            if host in url.split("/", 3)[2]:
                return lim
        return self.limiter

    def request(self, method: str, url: str, *, headers: dict | None = None, ok=(200, 206)) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            self._limiter_for(url).wait()
            self.stats.add(requests=1)
            try:
                r = self._session().request(method, url, headers=headers, timeout=self.timeout, stream=True)
                if r.status_code in ok:
                    if method != "HEAD":
                        _ = r.content  # read body now (inside the retry scope)
                    self.stats.add(bytes=len(r.content) if method != "HEAD" else 0)
                    return r
                status, retry_after = r.status_code, r.headers.get("Retry-After")
                r.close()
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
                if attempt >= self.max_retries:
                    raise FetchError(f"{method} {url}: {type(e).__name__}: {e}") from e
                self._backoff(attempt, None, type(e).__name__, url)
                continue
            if status == 404:
                self.stats.add(not_found=1)
                raise NotFound(url)
            if status in RETRY_STATUS and attempt < self.max_retries:
                self._backoff(attempt, retry_after, f"HTTP {status}", url)
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
