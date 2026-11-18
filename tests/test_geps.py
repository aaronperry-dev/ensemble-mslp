"""Canadian GEPS: one all-member GRIB2 file per step -> split, order members by their ensemble keys, decode, blend."""
import http.server
import socketserver
import threading
from datetime import datetime, timezone

import numpy as np
import pytest

eccodes = pytest.importorskip("eccodes")

from gefs_pipeline.config import load_config
from gefs_pipeline.decode import DecodeError, decode_message, member_key, split_messages
from gefs_pipeline.fetcher import Http
from gefs_pipeline.sources import GepsFamily, make_families, resolve_cycle

RUN = datetime(2026, 11, 18, 0, tzinfo=timezone.utc)
RES = 1.0


def geps_message(level_pa: float, *, typ: int, number: int, shape_hole=False) -> bytes:
    """A 0.5-style global lat/lon MSLP message (S->N scan from 0E like ECCC's) carrying GRIB2 ensemble keys."""
    nj, ni = int(180 / RES) + 1, int(360 / RES)
    g = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set(g, "productDefinitionTemplateNumber", 1)         # individual ensemble forecast
    for k, v in dict(Ni=ni, Nj=nj, latitudeOfFirstGridPointInDegrees=-90.0, latitudeOfLastGridPointInDegrees=90.0,
                     longitudeOfFirstGridPointInDegrees=0.0, longitudeOfLastGridPointInDegrees=360.0 - RES,
                     iDirectionIncrementInDegrees=RES, jDirectionIncrementInDegrees=RES, dataDate=20261118, dataTime=0,
                     shortName="prmsl", bitsPerValue=16, typeOfEnsembleForecast=typ, perturbationNumber=number,
                     numberOfForecastsInEnsemble=21).items():
        eccodes.codes_set(g, k, v)
    eccodes.codes_set_values(g, np.full(ni * nj, level_pa))
    raw = eccodes.codes_get_message(g)
    eccodes.codes_release(g)
    return raw


def member_file(order):
    """order: list of (type, number); MSLP level encodes the member: 1000 + 0.5*number hPa (control = 1000)."""
    return b"".join(geps_message(100000.0 + 50.0 * n, typ=t, number=n) for t, n in order)


def full_order(shuffle=True):
    members = [(1, 0)] + [(3, i) for i in range(1, 21)]                # control (low-res, type 1) + 20 positively perturbed
    return members[::-1] if shuffle else members                       # reversed: control LAST in the file


class Handler(http.server.BaseHTTPRequestHandler):
    blobs: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        Handler.seen.append((self.command, self.path))
        blob = Handler.blobs.get(self.path)
        if blob is None:
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(blob))); self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(blob)


@pytest.fixture()
def server():
    Handler.blobs, Handler.seen = {}, []
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def path(fh, run=RUN):
    return (f"/{run:%Y%m%d}/WXO-DD/ensemble/geps/grib2/raw/{run:%H}/{fh:03d}/"
            f"CMC_geps-raw_PRMSL_MSL_0_latlon0p5x0p5_{run:%Y%m%d%H}_P{fh:03d}_allmbrs.grib2")


def mk():
    return Http(user_agent="t", rps=0, backoff_base=0.01, backoff_max=0.05, sleep=lambda s: None)


def cfg_for(server):
    cfg = load_config()
    cfg.geps.base_url = server + "/{date}/WXO-DD/ensemble/geps/grib2/raw"
    return cfg


def test_split_and_member_keys():
    raw = member_file(full_order())
    msgs = split_messages(raw)
    assert len(msgs) == 21 and all(m[:4] == b"GRIB" and m[-4:] == b"7777" for m in msgs)
    assert member_key(msgs[-1]) == (0, 0)                              # control sorts before every perturbed member
    assert member_key(msgs[0]) == (1, 20)
    with pytest.raises(DecodeError):
        split_messages(raw[:-100])


def test_members_ordered_by_ensemble_keys_not_file_order(server):
    cfg = cfg_for(server)
    Handler.blobs[path(24)] = member_file(full_order(shuffle=True))
    fam, h = GepsFamily(cfg), mk()
    assert fam.members[0] == "cc00" and fam.members[-1] == "cp20" and len(fam.members) == 21
    url = fam.plan(h, RUN, 24)
    assert url and url.endswith("_P024_allmbrs.grib2")
    for name, level in (("cc00", 1000.0), ("cp01", 1000.5), ("cp20", 1010.0)):
        g = fam.fetch_grid(h, RUN, url, name, 24, None)
        assert float(np.nanmean(g.values)) == pytest.approx(level, abs=0.02), name
    assert g.res == 1.0 and g.lats[0] < g.lats[-1] and g.lons[0] < g.lons[-1]
    gets = [p for m, p in Handler.seen if m == "GET"]
    assert len(gets) == 1                                              # one download serves all 21 members of the step


def test_cache_means_no_second_download(server, tmp_path):
    cfg = cfg_for(server)
    Handler.blobs[path(24)] = member_file(full_order())
    h = mk()
    f1 = GepsFamily(cfg)
    url = f1.plan(h, RUN, 24)
    f1.fetch_grid(h, RUN, url, "cp05", 24, tmp_path)
    n = len(Handler.seen)
    GepsFamily(cfg).fetch_grid(h, RUN, url, "cp06", 24, tmp_path)      # new object, warm disk cache
    assert len(Handler.seen) == n


def test_missing_member_raises_keyerror_which_the_pipeline_tolerates(server):
    cfg = cfg_for(server)
    Handler.blobs[path(24)] = member_file(full_order()[:15])           # file with only 15 members
    fam, h = GepsFamily(cfg), mk()
    url = fam.plan(h, RUN, 24)
    assert fam.fetch_grid(h, RUN, url, "cp05", 24, None) is not None
    with pytest.raises(KeyError):
        fam.fetch_grid(h, RUN, url, "cp20", 24, None)


def test_schedule_and_readiness(server):
    cfg = cfg_for(server)
    fam, h = GepsFamily(cfg), mk()
    assert fam.max_fh(RUN) == 384 and fam.max_fh(RUN.replace(hour=12)) == 384 and fam.max_fh(RUN.replace(hour=6)) == 144
    assert fam.plan(h, RUN, 24) is None                                # not published yet
    assert not fam.cycle_ready(h, RUN, [0, 6, 12, 240])
    for fh in (0, 240):
        Handler.blobs[path(fh)] = b"x"
    assert fam.cycle_ready(h, RUN, [0, 6, 12, 240])


def test_dated_and_today_layouts():
    cfg = load_config()
    fam = GepsFamily(cfg)
    u = fam._url(RUN, 6)
    assert u == ("https://dd.weather.gc.ca/20261118/WXO-DD/ensemble/geps/grib2/raw/00/006/"
                 "CMC_geps-raw_PRMSL_MSL_0_latlon0p5x0p5_2026111800_P006_allmbrs.grib2")
    cfg.geps.base_url = "https://dd.weather.gc.ca/today/ensemble/geps/grib2/raw"
    assert "/today/ensemble/geps/grib2/raw/00/006/" in GepsFamily(cfg)._url(RUN, 6)


def test_registered_in_family_factory():
    assert [f.key for f in make_families(load_config(), ["geps"])] == ["geps"]
