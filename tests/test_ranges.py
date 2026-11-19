"""Request-efficiency tests: range coalescing, adaptive (AIMD) rate limiter, single-flight inventories, per-host stats."""
import threading
import time

import pytest

from gefs_pipeline.fetcher import FetchError, Http, IdxEntry, RateLimiter, Stats, fetch_parts, plan_parts
from gefs_pipeline.sources import _Inventories

# an AIGEFS-like inventory: u, v, TMP, PRMSL close together, then a big gap, then the gust
E = [IdxEntry(1, 0, "HGT", "500 mb", "x"),
     IdxEntry(2, 1000, "UGRD", "10 m above ground", "x"),
     IdxEntry(3, 1500, "VGRD", "10 m above ground", "x"),
     IdxEntry(4, 2000, "TMP", "2 m above ground", "x"),
     IdxEntry(5, 2600, "PRMSL", "mean sea level", "x"),
     IdxEntry(6, 3000, "SPFH", "2 m above ground", "x"),
     IdxEntry(7, 50_000_000, "GUST", "surface", "x"),
     IdxEntry(8, 50_000_400, "APCP", "surface", "x")]
W = {"p": ("PRMSL", "mean sea level"), "u": ("UGRD", "10 m above ground"), "v": ("VGRD", "10 m above ground"),
     "g": ("GUST", "surface")}


def test_plan_adjacent_only_merges_u_and_v():
    ranges, missing = plan_parts(E, W, max_gap=0)
    assert missing == []
    spans = [(a, b, [p for p, *_ in parts]) for a, b, parts in ranges]
    assert spans == [(1000, 1999, ["u", "v"]), (2600, 2999, ["p"]), (50_000_000, 50_000_399, ["g"])]


def test_plan_gap_merges_nearby_messages_but_not_distant_ones():
    ranges, _ = plan_parts(E, W, max_gap=1024 * 1024)
    assert [(a, b, [p for p, *_ in parts]) for a, b, parts in ranges] == \
        [(1000, 2999, ["u", "v", "p"]), (50_000_000, 50_000_399, ["g"])]
    # offsets / lengths inside the merged range point at each message
    parts = {p: (o, n) for p, o, n in ranges[0][2]}
    assert parts == {"u": (0, 500), "v": (500, 500), "p": (1600, 400)}


def test_plan_last_message_is_open_ended_and_missing_reported():
    entries = E[:3]
    ranges, missing = plan_parts(entries, {"v": W["v"], "p": W["p"]}, 0)
    assert missing == ["p"] and ranges == [(1500, None, [("v", 0, None)])]


def _blob(n, tag):
    return b"GRIB" + tag * (n - 4)


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.data = b"\0" * 1000 + _blob(500, b"u") + _blob(500, b"v") + b"\0" * 600 + _blob(400, b"p") + b"\0" * 100

    def get_range(self, url, start, end):
        self.calls.append((start, end))
        return self.data[start:(end + 1) if end is not None else None]


def test_fetch_parts_slices_each_message_out_of_a_shared_request():
    h = FakeHttp()
    out, missing = fetch_parts(h, "u", E[:6], {k: W[k] for k in "puv"}, max_gap=1024 * 1024)
    assert h.calls == [(1000, 2999)]                       # three messages, one request
    assert out["u"] == _blob(500, b"u") and out["v"] == _blob(500, b"v") and out["p"].startswith(b"GRIB") and len(out["p"]) == 400


def test_fetch_parts_rejects_a_slice_that_is_not_grib():
    h = FakeHttp()
    h.data = b"\0" * 5000
    with pytest.raises(FetchError):
        fetch_parts(h, "u", E[:6], {"u": W["u"]}, 0)


def test_limiter_halves_on_penalty_once_per_cooldown_and_recovers():
    t = [100.0]
    lim = RateLimiter(10, floor=0.2, cooldown=2.0, recover_after=5, clock=lambda: t[0])
    lim.penalize(); lim.penalize()                        # a burst of simultaneous 503s counts once
    assert lim.rate == 5 and lim.penalties == 1
    t[0] += 3
    lim.penalize()
    assert lim.rate == 2.5
    for _ in range(100):
        lim.ok()
    assert lim.rate == 10                                  # creeps back to the ceiling, never above it
    t[0] += 3
    for _ in range(10):
        t[0] += 3
        lim.penalize()
    assert lim.rate >= 2                                   # floor = 20 % of the ceiling


def test_limiter_without_rate_is_a_no_op():
    lim = RateLimiter(0)
    lim.penalize(); lim.ok(); lim.wait()
    assert lim.rate == 0


def test_limiter_spacing_follows_current_rate():
    lim = RateLimiter(50)
    t0 = time.monotonic()
    for _ in range(6):
        lim.wait()
    assert time.monotonic() - t0 >= 5 / 50 - 0.005


def test_inventory_single_flight():
    class H:
        n = 0

        def get_text(self, url):
            H.n += 1
            time.sleep(0.05)
            return "1:0:d=x:PRMSL:mean sea level:anl:ENS=+1\n"

    inv, h = _Inventories(), H()
    res = []
    ts = [threading.Thread(target=lambda: res.append(inv.get(h, "http://a/b"))) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert H.n == 1 and len(res) == 8 and all(r is res[0] for r in res)


def test_stats_summary_per_host():
    s = Stats()
    s.add_host("ecmwf", requests=3, ranged=3, bytes=3_000_000)
    s.add_host("graphcast", requests=2, whole=1, bytes=500_000, retries=1)
    lines = s.summary()
    assert lines[0].startswith("ecmwf: 3 requests (3 byte-range, 0 whole-file / inventory), 3 MB, 1000 kB per read")
    assert "1 retries" in lines[1]


def test_http_counts_ranged_reads_per_host_and_backs_off_on_503():
    import http.server, socketserver
    seen = {"n": 0}

    class Hd(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            seen["n"] += 1
            if seen["n"] == 1:
                self.send_response(503); self.send_header("Retry-After", "0"); self.send_header("Content-Length", "0"); self.end_headers(); return
            body = b"x" * 100
            self.send_response(206); self.send_header("Content-Range", "bytes 0-99/1000"); self.send_header("Content-Length", "100"); self.end_headers()
            self.wfile.write(body)

    srv = socketserver.TCPServer(("127.0.0.1", 0), Hd)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        http_ = Http(user_agent="t", rps=100, backoff_base=0.01, backoff_max=0.02, sleep=lambda s: None, host_rps={"127.0.0.1": 40})
        http_.get_range(f"http://127.0.0.1:{srv.server_address[1]}/f", 0, 99)
        h = http_.stats.hosts["127.0.0.1"]
        assert h["requests"] == 2 and h["ranged"] == 1 and h["retries"] == 1 and h["bytes"] == 100
        assert http_.host_limiters["127.0.0.1"].rate == 20 and http_.host_limiters["127.0.0.1"].penalties == 1
    finally:
        srv.shutdown()
