"""ECMWF AIFS ENS: separate control (-cf) and perturbed (-pf) files with JSON indexes, three byte-range reads per member
(msl, 10u, 10v: tens of MB apart in the real files), shared single-flight index, estimated gust, shared ECMWF limiter."""
import http.server
import json
import socketserver
import threading
from datetime import datetime, timezone

import numpy as np
import pytest

eccodes = pytest.importorskip("eccodes")

from gefs_pipeline.config import load_config
from gefs_pipeline.fetcher import Http
from gefs_pipeline.pipeline import make_http, _with_estimated_gust
from gefs_pipeline.sources import AifsFamily, make_families
from tests.test_wind import MPH, msg

RUN = datetime(2026, 11, 19, 0, tzinfo=timezone.utc)


def build(entries):
    """entries: [(type, number, param, levtype, bytes)] -> (blob, JSON-lines index) in the ECMWF open-data format."""
    blob, rows = b"", []
    for typ, num, param, lev, data in entries:
        row = {"domain": "g", "date": "20261119", "time": "0000", "expver": "0001", "class": "ai", "type": typ, "stream": "enfo",
               "step": "24", "levtype": lev, "param": param, "model": "aifs-ens", "_offset": len(blob), "_length": len(data)}
        if num is not None:
            row["number"] = str(num)
        rows.append(json.dumps(row))
        blob += data
    return blob, "\n".join(rows) + "\n"


def make_files():
    def member(typ, n):
        lvl = 100000.0 + 100.0 * n
        return [(typ, None if typ == "cf" else n, "t", "pl", b"P" * 600), (typ, None if typ == "cf" else n, "msl", "sfc", msg("msl", lvl)),
                (typ, None if typ == "cf" else n, "2t", "sfc", b"T" * 400), (typ, None if typ == "cf" else n, "10u", "sfc", msg("10u", 3.0 + n)),
                (typ, None if typ == "cf" else n, "2d", "sfc", b"D" * 300), (typ, None if typ == "cf" else n, "10v", "sfc", msg("10v", 4.0))]
    return {"cf": build(member("cf", 0)), "pf": build([e for n in range(1, 51) for e in member("pf", n)])}


class Handler(http.server.BaseHTTPRequestHandler):
    files, seen = None, []

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
        p = self.path
        if "/aifs-ens/0p25/enfo/" not in p:
            return self._send(404)
        kind = "cf" if "-enfo-cf." in p else "pf" if "-enfo-pf." in p else None
        if kind is None:
            return self._send(404)
        blob, idx = Handler.files[kind]
        if p.endswith(".index"):
            return self._send(200, idx.encode())
        rng = self.headers.get("Range")
        if rng:
            a, b = rng.removeprefix("bytes=").split("-")
            a, b = int(a), min(int(b), len(blob) - 1)
            return self._send(206, blob[a:b + 1], {"Content-Range": f"bytes {a}-{b}/{len(blob)}"})
        return self._send(200, blob)


@pytest.fixture()
def server():
    Handler.files, Handler.seen = make_files(), []
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def cfg_for(server):
    cfg = load_config()
    cfg.aifs.base_url = server
    cfg.domain.pad_deg = 3
    return cfg


def mk():
    return Http(user_agent="t", rps=0, backoff_base=0.01, backoff_max=0.05, sleep=lambda s: None)


def test_family_is_registered_with_51_members_and_no_own_gust():
    cfg = load_config()
    fam = {f.key: f for f in make_families(cfg)}["aifs"]
    assert fam.members[0] == "ac00" and fam.members[1] == "ap01" and fam.members[-1] == "ap50" and len(fam.members) == 51
    assert fam.has_wind and not fam.has_gust and fam.org == "ECMWF"
    assert fam.max_fh(RUN) == 360 and fam.max_fh(RUN.replace(hour=6)) == 360                # full range on every cycle
    assert "aifs" in cfg.wind.estimate.families and "aifs" in cfg.run.optional and "aifs" in cfg.wind.families


def test_share_the_ecmwf_host_limiter_with_the_ifs():
    h = make_http(load_config())
    ifs_url = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/x/ifs/y"
    aifs_url = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/x/aifs-ens/y"
    assert h._limiter_for(ifs_url) is h._limiter_for(aifs_url) and h._limiter_for(ifs_url).ceiling == 3


def test_member_is_msl_u_v_by_byte_range_control_from_cf_perturbed_from_pf(server):
    cfg = cfg_for(server)
    fam, h = AifsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    assert set(plan) == {"cf", "pf"}
    r7 = fam.fetch_member(h, RUN, plan, "ap07", 24, None, True)
    assert float(np.nanmean(r7.grid.values)) == pytest.approx(1007.0, abs=0.01)
    assert r7.wind_error is None and set(r7.wind) == {"wind"}                               # no gust published
    assert float(r7.wind["wind"].values[5, 5]) == pytest.approx(float(np.hypot(10.0, 4.0)) * MPH, abs=1e-3)
    rc = fam.fetch_member(h, RUN, plan, "ac00", 24, None, True)
    assert float(np.nanmean(rc.grid.values)) == pytest.approx(1000.0, abs=0.01)
    assert float(rc.wind["wind"].values[5, 5]) == pytest.approx(5.0 * MPH, abs=1e-3)
    ranged = [(p, r) for m, p, r in Handler.seen if m == "GET" and r]
    assert len(ranged) == 6                                                                  # 3 per member: msl, 10u, 10v
    assert sum("-enfo-cf.grib2" in p for p, r in ranged) == 3 and sum("-enfo-pf.grib2" in p for p, r in ranged) == 3
    idx_gets = [p for m, p, r in Handler.seen if m == "GET" and p.endswith(".index")]
    assert sorted(p.rsplit("-", 1)[-1] for p in idx_gets) == ["cf.index", "pf.index"]       # one inventory per file, however many members


def test_index_is_shared_by_concurrent_members_and_the_disk_cache_prevents_refetch(server, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    cfg = cfg_for(server)
    fam, h = AifsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    with ThreadPoolExecutor(6) as pool:
        res = list(pool.map(lambda m: fam.fetch_member(h, RUN, plan, m, 24, tmp_path, True), [f"ap{i:02d}" for i in range(1, 9)]))
    assert all(r.wind_error is None for r in res)
    assert len([1 for m, p, r in Handler.seen if m == "GET" and p.endswith("-enfo-pf.index")]) == 1
    n = len(Handler.seen)
    for m in ("ap01", "ap02"):
        AifsFamily(cfg).fetch_member(h, RUN, plan, m, 24, tmp_path, True)                  # fresh object, warm disk cache
    assert len(Handler.seen) == n


def test_gust_is_estimated_for_aifs_and_flagged(server):
    cfg = cfg_for(server)
    fam, h = AifsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    r = fam.fetch_member(h, RUN, plan, "ap03", 24, None, True)
    out, flagged = _with_estimated_gust(cfg, fam, {"wind": {"ap03": r.wind["wind"]}})
    assert flagged and float(out["gust"]["ap03"].values.max()) >= float(out["wind"]["ap03"].values.max())


def test_missing_step_makes_plan_none_and_cycle_not_ready(server):
    cfg = cfg_for(server)
    fam, h = AifsFamily(cfg), mk()
    assert fam.cycle_ready(h, RUN, [0, 24]) is True
    Handler.files["pf"] = ("", "")
    assert fam.plan(h, RUN, 24) is not None                                                  # HEAD of the index still answers 200
    cfg.aifs.model = "aifs-none"                                                           # a step that is not published
    assert AifsFamily(cfg).plan(h, RUN, 24) is None
