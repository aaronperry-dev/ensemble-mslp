"""Range-request client tests against a local server that mimics S3 (206 + Content-Range) and injects 503s."""
import http.server
import socketserver
import threading
import time

import pytest

from gefs_pipeline.fetcher import FetchError, Http, NotFound, RateLimiter, find_message, parse_idx

BLOB = bytes(range(256)) * 40            # 10 240 bytes
IDX = "\n".join([
    "1:0:d=2026111806:VIS:surface:anl:ENS=+1",
    "2:1000:d=2026111806:GUST:surface:anl:ENS=+1",
    "3:2500:d=2026111806:PRMSL:mean sea level:anl:ENS=+1",
    "4:4000:d=2026111806:TMP:2 m above ground:anl:ENS=+1",
]) + "\n"
IDX_LAST = IDX.replace("4:4000:d=2026111806:TMP:2 m above ground:anl:ENS=+1\n", "")


class Handler(http.server.BaseHTTPRequestHandler):
    fail_first = {}          # path -> remaining 503s to emit
    seen = []
    ignore_range = False

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", extra=None):
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        Handler.seen.append((self.command, self.path, self.headers.get("Range")))
        if Handler.fail_first.get(self.path, 0) > 0:
            Handler.fail_first[self.path] -= 1
            return self._send(503, b"SlowDown", {"Retry-After": "0"})
        if self.path.endswith("missing"):
            return self._send(404)
        if self.path.endswith(".idx"):
            return self._send(200, (IDX_LAST if "last" in self.path else IDX).encode())
        rng = self.headers.get("Range")
        if rng and not Handler.ignore_range:
            a, b = rng.removeprefix("bytes=").split("-")
            a = int(a)
            b = min(int(b), len(BLOB) - 1) if b else len(BLOB) - 1
            return self._send(206, BLOB[a:b + 1], {"Content-Range": f"bytes {a}-{b}/{len(BLOB)}"})
        return self._send(200, BLOB)


@pytest.fixture()
def server():
    Handler.fail_first, Handler.seen, Handler.ignore_range = {}, [], False
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def mk(**kw):
    sleeps = []
    h = Http(user_agent="test", rps=0, backoff_base=0.01, backoff_max=0.05, sleep=sleeps.append, **kw)
    h.sleeps = sleeps
    return h


def test_parse_idx_and_message_bounds():
    e = parse_idx(IDX)
    assert [x.var for x in e] == ["VIS", "GUST", "PRMSL", "TMP"]
    assert find_message(e, "PRMSL", "mean sea level") == (2500, 3999)       # next offset - 1
    assert find_message(parse_idx(IDX_LAST), "PRMSL", "mean sea level") == (2500, None)  # last message -> open ended
    with pytest.raises(KeyError):
        find_message(e, "NOPE", "x")


def test_range_returns_only_requested_bytes(server):
    h = mk()
    assert h.get_range(f"{server}/obj", 2500, 3999) == BLOB[2500:4000]
    assert h.get_range(f"{server}/obj", 10000) == BLOB[10000:]                # open-ended
    assert Handler.seen[0][2] == "bytes=2500-3999"
    assert h.stats.bytes == 1500 + 240                                         # nothing else downloaded


def test_range_ignored_by_server_is_rejected(server):
    Handler.ignore_range = True            # server answers 200 + whole file -> we must NOT accept it
    with pytest.raises(FetchError):
        mk().get_range(f"{server}/obj", 10, 20)


def test_retries_503_with_backoff_then_succeeds(server):
    Handler.fail_first["/obj"] = 3
    h = mk()
    assert h.get_range(f"{server}/obj", 0, 9) == BLOB[:10]
    assert h.stats.retries == 3 and len(h.sleeps) == 3
    assert all(0 < s <= 0.05 for s in h.sleeps)


def test_gives_up_after_max_retries(server):
    Handler.fail_first["/obj"] = 99
    h = mk(max_retries=2)
    with pytest.raises(FetchError):
        h.get_range(f"{server}/obj", 0, 9)
    assert h.stats.requests == 3          # 1 try + 2 retries, then stop (no hammering)


def test_404_is_not_retried(server):
    h = mk()
    assert not h.exists(f"{server}/missing")
    with pytest.raises(NotFound):
        h.get_text(f"{server}/missing")
    assert h.stats.retries == 0


def test_retry_after_header_is_honoured(server):
    Handler.fail_first["/obj"] = 1
    h = mk()
    orig = Handler._send

    def with_retry_after(self, code, body=b"", extra=None):
        if code == 503:
            extra = {"Retry-After": "2"}
        return orig(self, code, body, extra)
    Handler._send = with_retry_after
    try:
        h.get_range(f"{server}/obj", 0, 9)
    finally:
        Handler._send = orig
    assert h.sleeps and h.sleeps[0] >= 2


def test_rate_limiter_spaces_requests():
    rl = RateLimiter(20)                   # 50 ms apart
    t0 = time.monotonic()
    for _ in range(5):
        rl.wait()
    assert time.monotonic() - t0 >= 0.19   # 4 gaps x 50 ms


def test_per_host_rate_limit_is_separate_from_the_default():
    h = Http(user_agent="t", rps=100, host_rps={"ecmwf-forecasts": 2})
    assert h._limiter_for("https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/x") is h.host_limiters["ecmwf-forecasts"]
    assert h._limiter_for("https://noaa-gefs-pds.s3.amazonaws.com/x") is h.limiter
    assert h.host_limiters["ecmwf-forecasts"].interval == pytest.approx(0.5)
