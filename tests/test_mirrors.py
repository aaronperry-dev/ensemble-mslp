"""Mirror failover: equivalent copies of a dataset on several servers share the load, and a throttled / lagging server is skipped."""
import http.server
import socketserver
import threading

import pytest

from gefs_pipeline.config import load_config
from gefs_pipeline.fetcher import Http, MirrorGroup, NotFound
from gefs_pipeline.pipeline import make_http

BLOB = bytes(range(256)) * 40


def server(mode):
    """mode: 'ok' | 'throttle' (always 503) | 'missing' (always 404).  Returns (base_url, hits, shutdown)."""
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            hits.append((self.command, self.path))
            if mode != "ok":
                self.send_response(503 if mode == "throttle" else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            rng = self.headers.get("Range")
            body, code = BLOB, 200
            if rng:
                a, b = rng.split("=")[1].split("-")
                a, b = int(a), int(b) if b else len(BLOB) - 1
                body, code = BLOB[a:b + 1], 206
            self.send_response(code)
            if code == 206:
                self.send_header("Content-Range", f"bytes {a}-{a + len(body) - 1}/{len(BLOB)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/data", hits, srv.shutdown


@pytest.fixture
def servers():
    made = []

    def mk(mode):
        s = server(mode)
        made.append(s)
        return s
    yield mk
    for *_, stop in made:
        stop()


def client(*bases, **kw):
    return Http(user_agent="t", rps=0, max_retries=6, sleep=lambda s: None, mirrors=[list(bases)], **kw)


def test_throttled_server_is_skipped_and_requests_still_succeed(servers):
    a, hits_a, _ = servers("throttle")
    b, hits_b, _ = servers("ok")
    h = client(a, b)
    for i in range(12):
        assert h.get_range(a + "/x.grib2", i * 10, i * 10 + 9) == BLOB[i * 10:i * 10 + 10]
    assert len(hits_b) == 12
    assert 1 <= len(hits_a) <= 2           # one probe, then cooling down; nothing is hammered
    assert h.mirror_groups[0].failovers >= 1


def test_healthy_servers_share_the_load(servers):
    a, hits_a, _ = servers("ok")
    b, hits_b, _ = servers("ok")
    h = client(a, b)
    for _ in range(20):
        h.get_range(b + "/x", 0, 9)
    assert len(hits_a) >= 5 and len(hits_b) >= 5 and len(hits_a) + len(hits_b) == 20


def test_a_copy_that_lags_behind_is_not_a_missing_file(servers):
    a, _, _ = servers("missing")
    b, _, _ = servers("ok")
    h = client(a, b)
    assert h.exists(a + "/x.index") is True
    assert h.get_range(a + "/x", 5, 14) == BLOB[5:15]


def test_missing_everywhere_is_notfound(servers):
    a, _, _ = servers("missing")
    b, _, _ = servers("missing")
    with pytest.raises(NotFound):
        client(a, b).get_text(a + "/x")
    assert client(a, b).exists(b + "/x") is False


def test_all_throttled_still_gives_up_with_an_error(servers):
    a, _, _ = servers("throttle")
    b, _, _ = servers("throttle")
    h = Http(user_agent="t", rps=0, max_retries=3, sleep=lambda s: None, mirrors=[[a, b]])
    with pytest.raises(Exception, match="503"):
        h.get_range(a + "/x", 0, 9)


def test_urls_outside_the_group_are_untouched(servers):
    a, _, _ = servers("ok")
    b, _, _ = servers("ok")
    other, hits_o, _ = servers("ok")
    h = client(a, b)
    h.get_range(other + "/y", 0, 3)
    assert len(hits_o) == 1


def test_cooldown_doubles_and_clears_on_success():
    t = [0.0]
    g = MirrorGroup(["http://a/x", "http://b/x"], cooldown=30, cooldown_max=100, clock=lambda: t[0])
    g.bad("http://a/x"); assert g._until["http://a/x"] == 30
    t[0] = 31; g.bad("http://a/x"); assert g._until["http://a/x"] == 31 + 60
    t[0] = 200; g.bad("http://a/x"); assert g._until["http://a/x"] == 300          # capped at cooldown_max
    assert g.pick() == "http://b/x"
    g.good("http://a/x")
    assert g._until["http://a/x"] == 0 and g._fails["http://a/x"] == 0


def test_config_builds_the_mirror_group_and_a_limiter_per_mirror():
    cfg = load_config()
    h = make_http(cfg)
    assert h.mirror_groups and h.mirror_groups[0].bases[0] == cfg.ifs.base_url.rstrip("/")
    assert any("storage.googleapis.com" in b for b in h.mirror_groups[0].bases)
    assert "storage.googleapis.com" in h.host_limiters and "ecmwf-forecasts" in h.host_limiters
    cfg.ifs.mirrors = []
    assert make_http(cfg).mirror_groups == []
