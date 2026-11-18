"""NOAA AIGEFS: S3-style layout with .idx + byte ranges (like GEFS), optional-family cycle logic, rebuild-when-late."""
import http.server
import socketserver
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

eccodes = pytest.importorskip("eccodes")

from gefs_pipeline.cli import should_skip
from gefs_pipeline.config import load_config
from gefs_pipeline.fetcher import Http
from gefs_pipeline.sources import AigefsFamily, make_families, resolve_cycle, select_families

RUN = datetime(2026, 11, 18, 0, tzinfo=timezone.utc)


def msl_message(level_pa: float, res=1.0) -> bytes:
    nj, ni = int(180 / res) + 1, int(360 / res)
    g = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    for k, v in dict(Ni=ni, Nj=nj, latitudeOfFirstGridPointInDegrees=90.0, latitudeOfLastGridPointInDegrees=-90.0,
                     longitudeOfFirstGridPointInDegrees=0.0, longitudeOfLastGridPointInDegrees=360.0 - res,
                     iDirectionIncrementInDegrees=res, jDirectionIncrementInDegrees=res, dataDate=20261118, dataTime=0,
                     shortName="prmsl", bitsPerValue=24).items():
        eccodes.codes_set(g, k, v)
    eccodes.codes_set_values(g, np.full(ni * nj, level_pa))
    raw = eccodes.codes_get_message(g)
    eccodes.codes_release(g)
    return raw


def sfc_file(level_pa: float):
    """A surface file shaped like AIGEFS's: UGRD, VGRD, TMP, PRMSL, PRES ...  -> (blob, .idx text)."""
    parts = [("UGRD", "10 m above ground", b"U" * 900), ("TMP", "2 m above ground", b"T" * 700),
             ("PRMSL", "mean sea level", msl_message(level_pa)), ("PRES", "surface", b"P" * 800)]
    blob, lines, n = b"", [], 1
    for var, lev, data in parts:
        lines.append(f"{n}:{len(blob)}:d=2026111800:{var}:{lev}:24 hour fcst:ENS=+0")
        blob += data
        n += 1
    return blob, "\n".join(lines) + "\n"


class Handler(http.server.BaseHTTPRequestHandler):
    files: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        Handler.seen.append((self.command, self.path, self.headers.get("Range")))
        f = Handler.files.get(self.path)
        if f is None:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        rng = self.headers.get("Range")
        if rng and self.command == "GET":
            a, b = rng.removeprefix("bytes=").split("-")
            a, b = int(a), min(int(b), len(f) - 1)
            body = f[a:b + 1]
            self.send_response(206); self.send_header("Content-Range", f"bytes {a}-{b}/{len(f)}")
        else:
            body = f
            self.send_response(200)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


@pytest.fixture()
def server():
    Handler.files, Handler.seen = {}, []
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def cfg_for(server):
    cfg = load_config()
    cfg.aigefs.base_url = server + "/EAGLE_ensemble"
    return cfg


def path(member_no, fh, run=RUN):
    return (f"/EAGLE_ensemble/aigefs.{run:%Y%m%d}/{run:%H}/mem{member_no:03d}/model/atmos/grib2/"
            f"aigefs.t{run:%H}z.sfc.f{fh:03d}.grib2")


def publish(fh, members=range(31)):
    for m in members:
        blob, idx = sfc_file(100000.0 + 50.0 * m)             # member m -> 1000 + 0.5*m hPa
        Handler.files[path(m, fh)] = blob
        Handler.files[path(m, fh) + ".idx"] = idx.encode()


def mk():
    return Http(user_agent="t", rps=0, backoff_base=0.01, backoff_max=0.05, sleep=lambda s: None)


def test_members_urls_and_schedule():
    cfg = load_config()
    fam = AigefsFamily(cfg)
    assert len(fam.members) == 31 and fam.members[0] == "ai00" and fam.members[-1] == "ai30"
    assert fam._url(RUN, "ai07", 24) == ("https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com/EAGLE_ensemble/aigefs.20261118/00/"
                                         "mem007/model/atmos/grib2/aigefs.t00z.sfc.f024.grib2")
    assert fam.max_fh(RUN) == 384 and fam.max_fh(RUN.replace(hour=18)) == 384        # all four cycles are full length
    assert [f.key for f in make_families(cfg, ["aigefs"])] == ["aigefs"]


def test_fetch_member_by_idx_and_byte_range(server):
    cfg = cfg_for(server)
    publish(24)
    fam, h = AigefsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    assert plan and fam.plan_tag(plan) == "sfc"
    g = fam.fetch_grid(h, RUN, plan, "ai07", 24, None)
    assert float(np.nanmean(g.values)) == pytest.approx(1003.5, abs=0.02)
    g0 = fam.fetch_grid(h, RUN, plan, "ai00", 24, None)
    assert float(np.nanmean(g0.values)) == pytest.approx(1000.0, abs=0.02)
    ranged = [r for m, p, r in Handler.seen if m == "GET" and r]
    assert len(ranged) == 2                                                           # one ranged read per member, nothing else
    blob, idx = sfc_file(100350.0)
    start = int(idx.splitlines()[2].split(":")[1])
    assert ranged[0].startswith(f"bytes={start}-")                                    # exactly the PRMSL message


def test_cache_means_no_refetch(server, tmp_path):
    cfg = cfg_for(server)
    publish(24)
    fam, h = AigefsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    fam.fetch_grid(h, RUN, plan, "ai03", 24, tmp_path)
    n = len(Handler.seen)
    AigefsFamily(cfg).fetch_grid(h, RUN, plan, "ai03", 24, tmp_path)
    assert len(Handler.seen) == n


def test_cycle_ready_needs_every_member_at_the_last_hour(server):
    cfg = cfg_for(server)
    fam, h = AigefsFamily(cfg), mk()
    assert not fam.cycle_ready(h, RUN, [0, 6, 240])
    publish(240, members=range(30))                                                   # mem030 still missing
    assert not fam.cycle_ready(h, RUN, [0, 6, 240])
    publish(240)
    assert fam.cycle_ready(h, RUN, [0, 6, 240])


def test_optional_family_does_not_gate_the_cycle_but_is_used_when_ready(server):
    cfg = cfg_for(server)
    assert "aigefs" in cfg.run.optional
    fams = [SimpleNamespace(key="gefs", cycle_ready=lambda http, run, fhs: True),
            SimpleNamespace(key="aigefs", cycle_ready=lambda http, run, fhs: False)]
    now = datetime(2026, 11, 18, 17, 30, tzinfo=timezone.utc)
    assert resolve_cycle(None, cfg, fams, [0, 6], now=now).hour == 12                 # AIGEFS missing does NOT push us back a cycle
    assert [f.key for f in select_families(None, cfg, fams, RUN, [0, 6])] == ["gefs"]
    fams[1].cycle_ready = lambda http, run, fhs: True
    assert [f.key for f in select_families(None, cfg, fams, RUN, [0, 6])] == ["gefs", "aigefs"]
    only_optional = [fams[1]]
    assert resolve_cycle(None, cfg, only_optional, [0, 6], now=now).hour == 12        # nothing required -> optional ones decide


def test_late_arrival_triggers_rebuild_of_the_live_cycle():
    ids = "20261118_00"
    assert should_skip(ids, {"gefs", "ifs", "geps"}, ids, ["gefs", "ifs", "geps"]) is True           # nothing new
    assert should_skip(ids, {"gefs", "ifs", "geps"}, ids, ["gefs", "ifs", "geps", "aigefs"]) is False  # AIGEFS arrived late
    assert should_skip(ids, {"gefs", "ifs", "geps", "aigefs"}, ids, ["gefs", "ifs", "geps"]) is True   # no need to drop it
    assert should_skip("20261117_12", {"gefs"}, ids, ["gefs"]) is False                              # a new cycle
    assert should_skip(None, set(), ids, ["gefs"]) is False                                          # site not reachable
