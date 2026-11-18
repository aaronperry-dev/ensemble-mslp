"""ECMWF IFS ENS source: index parsing, byte-range fetch of single members, control fallback, cycle logic.
A local server mimics the ecmwf-forecasts bucket (JSON-lines .index + Range-capable .grib2)."""
import http.server
import json
import socketserver
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

eccodes = pytest.importorskip("eccodes")

from gefs_pipeline.config import load_config
from gefs_pipeline.fetcher import Http, NotFound, parse_json_index
from gefs_pipeline.sources import IfsFamily, make_families, resolve_cycle

RUN = datetime(2026, 11, 18, 0, tzinfo=timezone.utc)


def msl_message(level_pa: float, res=1.0) -> bytes:
    nj, ni = int(180 / res) + 1, int(360 / res)
    g = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    for k, v in dict(Ni=ni, Nj=nj, latitudeOfFirstGridPointInDegrees=90.0, latitudeOfLastGridPointInDegrees=-90.0,
                     longitudeOfFirstGridPointInDegrees=180.0, longitudeOfLastGridPointInDegrees=180.0 - res,
                     iDirectionIncrementInDegrees=res, jDirectionIncrementInDegrees=res, dataDate=20261118,
                     dataTime=0, shortName="msl", bitsPerValue=24).items():
        eccodes.codes_set(g, k, v)
    eccodes.codes_set_values(g, np.full(ni * nj, level_pa))
    raw = eccodes.codes_get_message(g)
    eccodes.codes_release(g)
    return raw


def build_file(entries):
    """entries: [(type, number, param, bytes)] -> (blob, index_text) in the ECMWF JSON-lines format."""
    blob, rows = b"", []
    for typ, num, param, data in entries:
        row = {"domain": "g", "date": "20261118", "time": "0000", "expver": "0001", "class": "od", "type": typ,
               "stream": "enfo", "levtype": "sfc", "step": "24", "param": param, "_offset": len(blob), "_length": len(data)}
        if num is not None:
            row["number"] = str(num)
        rows.append(json.dumps(row))
        blob += data
    return blob, "\n".join(rows) + "\n"


def member_level(n):                # pf member n -> 100000 + 100*n Pa  (1000 + n hPa)
    return 100000.0 + 100.0 * n


def make_files(with_control_in_oper=True):
    ens_entries = []
    for n in range(1, 51):
        ens_entries.append(("pf", n, "2t", b"X" * 700))             # noise from other parameters
        ens_entries.append(("pf", n, "msl", msl_message(member_level(n))))
    ens = build_file(ens_entries)
    oper = build_file([("fc", None, "2t", b"Y" * 500), ("fc", None, "msl", msl_message(member_level(0)))])
    cf = build_file([("cf", None, "msl", msl_message(member_level(0)))])
    return {"ens": ens, "oper": oper, "cf": cf, "control_in_oper": with_control_in_oper}


class Handler(http.server.BaseHTTPRequestHandler):
    files = None
    seen = []

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
        f = Handler.files
        path = self.path
        if "/ifs/0p25/enfo/" in path and "-enfo-ef." in path:
            blob, idx = f["cf"] if False else f["ens"]
        elif "/ifs/0p25/oper/" in path and "-oper-fc." in path:
            if not f["control_in_oper"]:
                return self._send(404)
            blob, idx = f["oper"]
        else:
            return self._send(404)
        if path.endswith(".index"):
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
    cfg.ifs.base_url = server
    cfg.domain.pad_deg = 3
    return cfg


def mk():
    return Http(user_agent="test", rps=0, backoff_base=0.01, backoff_max=0.05, sleep=lambda s: None)


def test_parse_json_index_keeps_only_the_requested_parameter():
    _, idx = make_files()["ens"]
    d = parse_json_index(idx, "msl")
    assert len(d) == 50 and ("pf", "1") in d and ("pf", "50") in d and ("pf", "0") not in d
    off, ln = d[("pf", "3")]
    assert off > 0 and ln > 100


def test_members_and_schedule():
    cfg = load_config()
    fam = IfsFamily(cfg)
    assert fam.members[0] == "ec00" and fam.members[1] == "ep01" and fam.members[-1] == "ep50" and len(fam.members) == 51
    assert fam.max_fh(RUN) == 360                                          # 00Z: full range
    assert fam.max_fh(RUN.replace(hour=12)) == 360
    assert fam.max_fh(RUN.replace(hour=6)) == 144                          # 06/18Z stop at 144 h
    assert fam.hours(RUN.replace(hour=18), [0, 72, 144, 150, 240]) == [0, 72, 144]
    assert [f.key for f in make_families(cfg)] == ["gefs", "ifs"]
    with pytest.raises(ValueError):
        make_families(cfg, ["nope"])


def test_fetch_one_member_by_byte_range_and_control_from_oper(server):
    cfg = cfg_for(server)
    fam, h = IfsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    assert plan is not None
    g7 = fam.fetch_grid(h, RUN, plan, "ep07", 24, None)
    assert float(np.nanmean(g7.values)) == pytest.approx(1007.0, abs=0.01)
    gc = fam.fetch_grid(h, RUN, plan, "ec00", 24, None)                      # control comes from the oper stream
    assert float(np.nanmean(gc.values)) == pytest.approx(1000.0, abs=0.01)
    ranges = [r for m, p, r in Handler.seen if m == "GET" and r]
    assert len(ranges) == 2                                                  # exactly one ranged read per member
    # each ranged read covers exactly one message of the indexed length, never a whole file
    idx = parse_json_index(Handler.files["ens"][1], "msl")
    want = idx[("pf", "7")][1]
    a0, b0 = ranges[0].removeprefix("bytes=").split("-")
    assert int(b0) - int(a0) + 1 == want


def test_index_fetched_once_per_step_and_cache_avoids_refetch(server, tmp_path):
    cfg = cfg_for(server)
    fam, h = IfsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    for m in ("ep01", "ep02", "ep03", "ep04"):
        fam.fetch_grid(h, RUN, plan, m, 24, tmp_path)
    idx_gets = [p for mth, p, r in Handler.seen if mth == "GET" and p.endswith("-enfo-ef.index")]
    assert len(idx_gets) == 1                                               # shared by all members of the step
    n = len(Handler.seen)
    fam2 = IfsFamily(cfg)                                                   # fresh object, warm disk cache
    for m in ("ep01", "ep02", "ep03", "ep04"):
        fam2.fetch_grid(h, RUN, plan, m, 24, tmp_path)
    assert len(Handler.seen) == n                                           # zero network requests


def test_control_falls_back_to_enfo_cf_on_older_cycles(server):
    Handler.files["control_in_oper"] = False
    ens_entries = [("cf", None, "msl", msl_message(member_level(0)))]
    for n in range(1, 51):
        ens_entries.append(("pf", n, "msl", msl_message(member_level(n))))
    Handler.files["ens"] = build_file(ens_entries)
    cfg = cfg_for(server)
    fam, h = IfsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    g = fam.fetch_grid(h, RUN, plan, "ec00", 24, None)
    assert float(np.nanmean(g.values)) == pytest.approx(1000.0, abs=0.01)


def test_missing_step_is_reported_not_fetched(server):
    cfg = cfg_for(server)
    fam = IfsFamily(cfg)
    h = mk()
    Handler.files["ens"] = ("", "")                                         # never reached: HEAD of .index must succeed
    assert fam.plan(h, RUN, 24) is not None
    with pytest.raises(NotFound):
        h.get_text(f"{server}/20261118/00z/ifs/0p25/nothing.index")


def test_resolve_cycle_picks_newest_cycle_all_families_have():
    cfg = load_config()
    ready = {}

    def fam(key, ok_cycles):
        return SimpleNamespace(key=key, cycle_ready=lambda http, run, fhs: run.hour in ok_cycles and (run.day, run.hour) not in ready.get(key, set()))

    now = datetime(2026, 11, 18, 17, 30, tzinfo=timezone.utc)
    # allowed cycles are [0, 12]; GEFS has everything, IFS only the 00Z: the 12Z cycle is skipped until IFS publishes it
    r = resolve_cycle(None, cfg, [fam("gefs", {0, 6, 12, 18}), fam("ifs", {0})], [0, 6], now=now)
    assert (r.day, r.hour) == (18, 0)
    r = resolve_cycle(None, cfg, [fam("gefs", {0, 6, 12, 18}), fam("ifs", {0, 12})], [0, 6], now=now)
    assert (r.day, r.hour) == (18, 12)
    cfg.run.cycles = [0, 6, 12, 18]
    r = resolve_cycle(None, cfg, [fam("gefs", {0, 6, 12, 18})], [0, 6], now=now)
    assert (r.day, r.hour) == (18, 12)
    with pytest.raises(RuntimeError):
        resolve_cycle(None, cfg, [fam("ifs", set())], [0, 6], now=now, lookback=3)
