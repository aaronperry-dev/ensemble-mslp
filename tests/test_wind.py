"""10 m wind speed and gust (mph): unit conversion, the cheapest byte ranges, each system's source layout, tolerant collection,
and the per-system grids written for the browser.  Local servers mimic the GEFS / AIGEFS (.idx), ECMWF (JSON index) and GEPS
(all-member file) layouts."""
import http.server
import json
import socketserver
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

eccodes = pytest.importorskip("eccodes")

from gefs_pipeline import gridio
from gefs_pipeline.config import load_config
from gefs_pipeline.decode import DecodeError, Grid, decode_message, speed_from_uv, split_messages
from gefs_pipeline.fetcher import FetchError, Http, NotFound, find_ranges, parse_idx, parse_json_index_params
from gefs_pipeline.pipeline import FamilyFrame, _collect_wind, build_frame
from gefs_pipeline.sources import AigefsFamily, GefsFamily, GepsFamily, IfsFamily

RUN = datetime(2026, 11, 18, 0, tzinfo=timezone.utc)
MPH = 2.2369363
RES = 1.0


def msg(short: str, values, *, lat_first=90.0, lon_first=0.0, members=None) -> bytes:
    """A global 1-degree lat/lon message with the given shortName (its units come from the GRIB parameter table)."""
    nj, ni = int(180 / RES) + 1, int(360 / RES)
    g = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    if members is not None:
        eccodes.codes_set(g, "productDefinitionTemplateNumber", 1)
    kv = dict(Ni=ni, Nj=nj, latitudeOfFirstGridPointInDegrees=lat_first, latitudeOfLastGridPointInDegrees=-lat_first,
              longitudeOfFirstGridPointInDegrees=lon_first, longitudeOfLastGridPointInDegrees=lon_first + 360.0 - RES,
              iDirectionIncrementInDegrees=RES, jDirectionIncrementInDegrees=RES, dataDate=20261118, dataTime=0,
              shortName=short, bitsPerValue=24)
    if members is not None:
        kv.update(typeOfEnsembleForecast=members[0], perturbationNumber=members[1], numberOfForecastsInEnsemble=21)
    for k, v in kv.items():
        eccodes.codes_set(g, k, v)
    eccodes.codes_set_values(g, np.full(ni * nj, float(values)))
    raw = eccodes.codes_get_message(g)
    eccodes.codes_release(g)
    return raw


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
            a, b = int(a), (len(f) - 1 if b == '' else min(int(b), len(f) - 1))      # "bytes=N-" = to the end of the object
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


def mk():
    return Http(user_agent="t", rps=0, backoff_base=0.01, backoff_max=0.05, sleep=lambda s: None)


def idx_file(parts):
    """parts: [(var, level, bytes)] -> (blob, wgrib2-style .idx text)"""
    blob, lines = b"", []
    for n, (var, lev, data) in enumerate(parts, 1):
        lines.append(f"{n}:{len(blob)}:d=2026111800:{var}:{lev}:24 hour fcst:ENS=+0")
        blob += data
    return blob, "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------
def test_find_ranges_merges_adjacent_messages_and_keeps_file_order():
    e = parse_idx("1:0:d=x:VIS:surface:24 hour fcst:\n2:100:d=x:GUST:surface:24 hour fcst:\n3:200:d=x:MSLET:mean sea level:24 hour fcst:\n"
                  "4:300:d=x:UGRD:10 m above ground:24 hour fcst:\n5:400:d=x:VGRD:10 m above ground:24 hour fcst:\n6:500:d=x:TMP:2 m above ground:24 hour fcst:\n")
    # u and v are neighbours -> one range [300, 499]; asking in the opposite order still reports the u/v indices of `wanted`
    assert find_ranges(e, [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")]) == [(300, 499, [0, 1])]
    assert find_ranges(e, [("GUST", "surface"), ("VGRD", "10 m above ground")]) == [(100, 199, [0]), (400, 499, [1])]
    last = parse_idx("1:0:d=x:UGRD:10 m above ground:24 hour fcst:\n2:50:d=x:VGRD:10 m above ground:24 hour fcst:\n")
    assert find_ranges(last, [("VGRD", "10 m above ground")]) == [(50, None, [0])]               # last message: open-ended
    with pytest.raises(KeyError):
        find_ranges(e, [("WIND", "10 m above ground")])


def test_json_index_params_one_pass():
    text = "\n".join(json.dumps(r) for r in [
        {"param": "msl", "type": "pf", "number": "1", "_offset": 0, "_length": 10},
        {"param": "10u", "type": "pf", "number": "1", "_offset": 10, "_length": 20},
        {"param": "10fg3", "type": "pf", "number": "1", "_offset": 30, "_length": 5},
        {"param": "2t", "type": "pf", "number": "1", "_offset": 35, "_length": 5}])
    d = parse_json_index_params(text, ("msl", "10u", "10v", "10fg"))
    assert d["msl"][("pf", "1")] == (0, 10) and d["10u"][("pf", "1")] == (10, 20) and d["10v"] == {} and d["10fg"] == {}


def test_decode_speed_converts_units_and_speed_from_components():
    box = (-180, 180, -90, 90)
    u = decode_message(msg("10u", 3.0), *box, field="speed")
    v = decode_message(msg("10v", 4.0), *box, field="speed")
    assert float(u.values[10, 10]) == pytest.approx(3.0 * MPH, abs=1e-3)
    s = speed_from_uv(u, v)
    assert float(s.values[10, 10]) == pytest.approx(5.0 * MPH, abs=1e-3)               # 3-4-5
    g = decode_message(msg("gust", 20.0), *box, field="speed")
    assert float(g.values[0, 0]) == pytest.approx(44.74, abs=0.01)
    decode_message(msg("prmsl", 100000.0), *box)                                          # a pressure message decodes as MSLP ...
    with pytest.raises(DecodeError):
        decode_message(msg("10u", 1.0), *box)                                             # ... and a wind message does not
    other = Grid(u.lats[:-1], u.lons, u.values[:-1], u.valid_time, 0)
    with pytest.raises(DecodeError):
        speed_from_uv(u, other)


# ---------------------------------------------------------------------------------------------------
def gefs_cfg(server):
    cfg = load_config()
    cfg.source.base_url = server
    return cfg


def gefs_path(member, fh):
    return f"/gefs.20261118/00/atmos/pgrb2sp25/{member}.t00z.pgrb2s.0p25.f{fh:03d}"


def test_gefs_wind_is_three_requests_idx_uv_gust(server):
    cfg = gefs_cfg(server)
    blob, idx = idx_file([("VIS", "surface", b"V" * 300), ("GUST", "surface", msg("gust", 20.0)),
                          ("PRMSL", "mean sea level", msg("prmsl", 100000.0)), ("TMP", "2 m above ground", b"T" * 200),
                          ("UGRD", "10 m above ground", msg("10u", 6.0)), ("VGRD", "10 m above ground", msg("10v", 8.0))])
    Handler.files[gefs_path("gep03", 24)] = blob
    Handler.files[gefs_path("gep03", 24) + ".idx"] = idx.encode()
    fam, h = GefsFamily(cfg), mk()
    plan = cfg.source.products[0]
    out = fam.fetch_wind(h, RUN, plan, "gep03", 24, None)
    assert float(out["wind"].values[5, 5]) == pytest.approx(10.0 * MPH, abs=1e-3)      # hypot(6, 8) m/s
    assert float(out["gust"].values[5, 5]) == pytest.approx(20.0 * MPH, abs=1e-3)
    ranged = [(p, r) for m, p, r in Handler.seen if m == "GET" and r]
    assert len(ranged) == 2                                                               # u+v in one read, gust in another
    assert len([1 for m, p, r in Handler.seen if p.endswith(".idx")]) == 1                # and the inventory once
    # MSLP of the same member reuses the cached inventory
    fam.fetch_grid(h, RUN, plan, "gep03", 24, None)
    assert len([1 for m, p, r in Handler.seen if p.endswith(".idx")]) == 1


def test_gefs_wind_without_gust_and_disk_cache(server, tmp_path):
    cfg = gefs_cfg(server)
    blob, idx = idx_file([("PRMSL", "mean sea level", msg("prmsl", 100000.0)),
                          ("UGRD", "10 m above ground", msg("10u", 6.0)), ("VGRD", "10 m above ground", msg("10v", 8.0))])
    Handler.files[gefs_path("gec00", 0)] = blob
    Handler.files[gefs_path("gec00", 0) + ".idx"] = idx.encode()
    fam, h, plan = GefsFamily(cfg), mk(), cfg.source.products[0]
    out = fam.fetch_wind(h, RUN, plan, "gec00", 0, tmp_path)
    assert set(out) == {"wind"}                                                            # no GUST message in this file: wind only
    n = len(Handler.seen)
    fam.fetch_wind(h, RUN, plan, "gec00", 0, tmp_path)                                    # cached on disk: no request at all
    assert len(Handler.seen) == n
    assert {p.name for p in (tmp_path / "2026111800").iterdir()} >= {"gec00.pgrb2sp25.f000.uv.grib2"}   # the hour is part of the name


def test_aigefs_has_wind_but_no_gust(server):
    cfg = load_config()
    cfg.aigefs.base_url = server + "/EAGLE_ensemble"
    blob, idx = idx_file([("UGRD", "10 m above ground", msg("10u", 3.0)), ("VGRD", "10 m above ground", msg("10v", 4.0)),
                          ("PRMSL", "mean sea level", msg("prmsl", 100000.0))])
    p = "/EAGLE_ensemble/aigefs.20261118/00/mem004/model/atmos/grib2/aigefs.t00z.sfc.f024.grib2"
    Handler.files[p], Handler.files[p + ".idx"] = blob, idx.encode()
    fam = AigefsFamily(cfg)
    out = fam.fetch_wind(mk(), RUN, None, "ai04", 24, None)
    assert set(out) == {"wind"} and float(out["wind"].values[3, 3]) == pytest.approx(5.0 * MPH, abs=1e-3)
    assert fam.has_wind and not fam.has_gust


# ---------------------------------------------------------------------------------------------------
def ifs_files(step_param_gust):
    """enfo + oper files whose gust parameter is named `step_param_gust` ('10fg' / '10fg3')."""
    def build(entries):
        blob, rows = b"", []
        for typ, num, param, data in entries:
            row = {"type": typ, "step": "24", "param": param, "_offset": len(blob), "_length": len(data)}
            if num is not None:
                row["number"] = str(num)
            rows.append(json.dumps(row))
            blob += data
        return blob, "\n".join(rows) + "\n"
    ens = []
    for n in range(1, 4):
        ens += [("pf", n, "msl", msg("msl", 100000.0)), ("pf", n, "10u", msg("10u", 3.0 * n)), ("pf", n, "10v", msg("10v", 4.0 * n)),
                ("pf", n, step_param_gust, msg("10fg", 10.0 * n))]
    oper = [("fc", None, "msl", msg("msl", 100000.0)), ("fc", None, "10u", msg("10u", 3.0)), ("fc", None, "10v", msg("10v", 4.0)),
            ("fc", None, step_param_gust, msg("10fg", 10.0))]
    return build(ens), build(oper)


def ifs_cfg(server):
    cfg = load_config()
    cfg.ifs.base_url = server
    return cfg


def ifs_paths(step):
    base = f"/20261118/00z/ifs/0p25"
    return (f"{base}/enfo/20261118000000-{step}h-enfo-ef.grib2", f"{base}/oper/20261118000000-{step}h-oper-fc.grib2")


@pytest.mark.parametrize("gust_param", ["10fg", "10fg3"])
def test_ifs_wind_and_gust_whatever_the_gust_parameter_is_called(server, gust_param):
    cfg = ifs_cfg(server)
    (eb, ei), (ob, oi) = ifs_files(gust_param)
    pe, po = ifs_paths(24)
    Handler.files.update({pe: eb, pe[:-5] + "index": ei.encode(), po: ob, po[:-5] + "index": oi.encode()})
    fam, h = IfsFamily(cfg), mk()
    plan = fam.plan(h, RUN, 24)
    out = fam.fetch_wind(h, RUN, plan, "ep02", 24, None)
    assert float(out["wind"].values[4, 4]) == pytest.approx(10.0 * MPH, abs=1e-3)       # hypot(6, 8)
    assert float(out["gust"].values[4, 4]) == pytest.approx(20.0 * MPH, abs=1e-3)
    ctl = fam.fetch_wind(h, RUN, plan, "ec00", 24, None)                                  # control comes from the oper file
    assert float(ctl["wind"].values[4, 4]) == pytest.approx(5.0 * MPH, abs=1e-3)
    # the index was downloaded once per file for MSLP + wind together
    assert len([1 for m, p, r in Handler.seen if p.endswith(".index")]) == 3              # exists() probe + enfo index + oper index


def test_ifs_gust_is_skipped_at_step_zero_and_when_absent(server):
    cfg = ifs_cfg(server)
    (eb, ei), (ob, oi) = ifs_files("10fg")
    pe, po = ifs_paths(0)
    Handler.files.update({pe: eb, pe[:-5] + "index": ei.encode(), po: ob, po[:-5] + "index": oi.encode()})
    fam, h = IfsFamily(cfg), mk()
    out = fam.fetch_wind(h, RUN, fam.plan(h, RUN, 0), "ep01", 0, None)
    assert set(out) == {"wind"}                                                           # step 0 gust is all zeros: not offered


# ---------------------------------------------------------------------------------------------------
def test_geps_wind_speed_from_the_all_member_file(server):
    cfg = load_config()
    cfg.geps.base_url = server + "/{date}/WXO-DD/ensemble/geps/grib2/raw"
    order = [(1, 0)] + [(3, i) for i in range(1, 21)]
    blob = b"".join(msg("10si", 2.0 + 0.5 * n, members=(t, n)) for t, n in order[::-1])      # control LAST, as in the MSLP test
    Handler.files["/20261118/WXO-DD/ensemble/geps/grib2/raw/00/024/CMC_geps-raw_WIND_TGL_10_latlon0p5x0p5_2026111800_P024_allmbrs.grib2"] = blob
    fam, h = GepsFamily(cfg), mk()
    out = fam.fetch_wind(h, RUN, "x", "cp05", 24, None)
    assert float(out["wind"].values[2, 2]) == pytest.approx((2.0 + 0.5 * 5) * MPH, abs=1e-3)
    assert float(fam.fetch_wind(h, RUN, "x", "cc00", 24, None)["wind"].values[2, 2]) == pytest.approx(2.0 * MPH, abs=1e-3)
    assert len([1 for m, p, r in Handler.seen if m == "GET" and "WIND_TGL_10" in p]) == 1   # whole file downloaded once for all members
    assert fam.has_wind and not fam.has_gust


# ---------------------------------------------------------------------------------------------------
class _Fut:
    def __init__(self, v=None, exc=None):
        self.v, self.exc = v, exc

    def result(self):
        if self.exc:
            raise self.exc
        return self.v


def grid(v, n=4):
    lats, lons = np.linspace(30, 33, n), np.linspace(-80, -77, n)
    return Grid(lats, lons, np.full((n, n), v, np.float32), "2026-11-18T06:00:00Z", 6)


def test_collect_wind_tolerates_failed_members_and_thin_variables():
    cfg = load_config()
    fam = SimpleNamespace(key="gefs", members=[f"m{i}" for i in range(10)])
    futs = {f"m{i}": _Fut({"wind": grid(10 + i), "gust": grid(20 + i)}) for i in range(10)}
    futs["m3"] = _Fut(exc=NotFound("404"))
    futs["m4"] = _Fut(exc=FetchError("gave up"))
    for i in range(5, 10):                                                                # 5 of 9 survivors lack gust
        futs[f"m{i}"] = _Fut({"wind": grid(10 + i)})
    out = _collect_wind(fam, futs, 24, need=8)
    assert set(out) == {"wind"} and len(out["wind"]) == 8                                 # gust has 3 members < 8: dropped; wind has 8
    assert _collect_wind(fam, {"m0": _Fut(exc=KeyError("x"))}, 24, need=1) is None


def test_build_frame_writes_wind_and_gust_grids_for_the_browser(tmp_path):
    cfg = load_config()
    fam = SimpleNamespace(key="gefs", members=["gec00", "gep01", "gep02"], plan_tag=lambda p: "pgrb2sp25")
    n = 6
    lats, lons = np.linspace(30, 33, n), np.linspace(-80, -77, n)
    mslp = {m: Grid(lats, lons, np.full((n, n), 1000.0 + i, np.float32), "2026-11-18T06:00:00Z", 6) for i, m in enumerate(fam.members)}
    wind = {m: Grid(lats, lons, np.full((n, n), 20.0 * (i + 1), np.float32), "2026-11-18T06:00:00Z", 6) for i, m in enumerate(fam.members)}
    wind["gep02"].values[2, 3] = 80.0                                                     # a localised peak in the last member
    gust = {m: Grid(lats, lons, np.full((n, n), 30.0 * (i + 1), np.float32), "2026-11-18T06:00:00Z", 6) for i, m in enumerate(fam.members)}
    ff = FamilyFrame(fam, cfg.source.products[0], mslp, list(fam.members), {"wind": wind, "gust": gust})
    entry = build_frame(cfg, {"gefs": ff}, ["gefs"], 6, tmp_path, "r")
    assert entry["avail"]["gefs"]["wind"]["members"] == 3 and entry["avail"]["gefs"]["gust"]["members"] == 3
    base = tmp_path / "r" / "gefs"
    hdr, mean, _ = gridio.decode((base / "wmean" / "f006.bin.gz").read_bytes())
    assert hdr["scale"] == 2 and hdr["unit"] == "mph" and hdr["members"] == 3
    assert mean[0, 0] == pytest.approx(40.0) and mean[2, 3] == pytest.approx((20 + 40 + 80) / 3, abs=0.5)      # mean of member speeds
    _, mx, owner = gridio.decode((base / "wmax" / "f006.bin.gz").read_bytes())
    assert mx[0, 0] == 60.0 and int(owner[0, 0]) == 2 and mx[2, 3] == 80.0 and int(owner[2, 3]) == 2
    ph, planes = gridio.decode_counts((base / "wprob" / "f006.bin.gz").read_bytes())
    lv = ph["levels"]
    assert lv == [float(x) for x in cfg.wind.speed_levels]
    # 20, 40, 60 mph members: at or above 25 -> 2 members; at or above 39 -> 2; 50 -> 1; 74 -> 0 (but 80 at one cell -> 1)
    got = {l: int(planes[i][0, 0]) for i, l in enumerate(lv)}
    assert got[25.0] == 2 and got[39.0] == 2 and got[50.0] == 1 and got[74.0] == 0 and int(planes[lv.index(74.0)][2, 3]) == 1
    _, gm, _ = gridio.decode((base / "gmean" / "f006.bin.gz").read_bytes())
    assert gm[0, 0] == pytest.approx(60.0)
    assert (base / "gprob" / "f006.bin.gz").exists() and (base / "gmax" / "f006.bin.gz").exists()
    # MSLP files are unaffected
    assert (base / "mean" / "f006.bin.gz").exists() and (base / "min" / "f006.bin.gz").exists()


def test_gridio_scale_round_trip_and_clip():
    f = np.array([[0.0, 12.5, 60.25, np.nan], [100.0, 101.0, 99.5, 20000.0]], np.float32)
    blob = gridio.encode({"nx": 4, "ny": 2}, f, scale=2)
    hdr, got, _ = gridio.decode(blob)
    assert hdr["scale"] == 2 and got[0, 1] == 12.5 and got[0, 2] == 60.0 and np.isnan(got[0, 3]) and got[1, 2] == 99.5
    assert got[1, 3] == pytest.approx(16383.5, abs=1)                                     # clipped to the Int16 range, not wrapped
    hdr2, got2, _ = gridio.decode(gridio.encode({"nx": 4, "ny": 2}, f))
    assert "scale" not in hdr2 and got2[0, 1] == 12.5                                      # default tenths: files unchanged


# ---- shared requests: MSLP + wind + gust of one member-hour --------------------------------------------
def test_gefs_member_is_idx_plus_three_ranges_and_reads_each_file_once(server, tmp_path):
    cfg = gefs_cfg(server)
    blob, idx = idx_file([("VIS", "surface", b"V" * 300), ("GUST", "surface", msg("gust", 20.0)), ("CAPE", "surface", b"C" * 100),
                          ("PRMSL", "mean sea level", msg("prmsl", 100000.0)), ("TMP", "2 m above ground", b"T" * 200),
                          ("UGRD", "10 m above ground", msg("10u", 6.0)), ("VGRD", "10 m above ground", msg("10v", 8.0))])
    Handler.files[gefs_path("gep03", 24)] = blob
    Handler.files[gefs_path("gep03", 24) + ".idx"] = idx.encode()
    cfg.fetch.merge_gap_kb = 0                                                           # real GEFS gaps are tens of MB: nothing merges
    fam, h, plan = GefsFamily(cfg), mk(), cfg.source.products[0]
    r = fam.fetch_member(h, RUN, plan, "gep03", 24, tmp_path, True)
    assert r.wind_error is None and set(r.wind) == {"wind", "gust"} and r.grid is not None
    assert float(r.wind["wind"].values[5, 5]) == pytest.approx(10.0 * MPH, abs=1e-3)
    assert [m for m, p, rg in Handler.seen if rg] == ["GET"] * 3                         # MSLP, u+v (adjacent), gust
    assert len([1 for m, p, rg in Handler.seen if p.endswith(".idx")]) == 1
    n = len(Handler.seen)
    fam.fetch_member(h, RUN, plan, "gep03", 24, tmp_path, True)                          # all three parts cached on disk
    assert len(Handler.seen) == n


def test_gefs_member_without_wind_wanted_does_not_touch_wind_messages(server):
    cfg = gefs_cfg(server)
    blob, idx = idx_file([("PRMSL", "mean sea level", msg("prmsl", 100000.0)),
                          ("UGRD", "10 m above ground", msg("10u", 6.0)), ("VGRD", "10 m above ground", msg("10v", 8.0))])
    Handler.files[gefs_path("gec00", 0)] = blob
    Handler.files[gefs_path("gec00", 0) + ".idx"] = idx.encode()
    r = GefsFamily(cfg).fetch_member(mk(), RUN, cfg.source.products[0], "gec00", 0, None, False)
    assert r.wind is None and r.wind_error is None
    assert len([1 for m, p, rg in Handler.seen if rg]) == 1


def test_aigefs_member_shares_one_range_for_wind_and_mslp(server):
    cfg = load_config()
    cfg.aigefs.base_url = server + "/EAGLE_ensemble"
    blob, idx = idx_file([("HGT", "500 mb", b"H" * 400), ("UGRD", "10 m above ground", msg("10u", 3.0)),
                          ("VGRD", "10 m above ground", msg("10v", 4.0)), ("TMP", "2 m above ground", b"T" * 300),
                          ("PRMSL", "mean sea level", msg("prmsl", 100000.0)), ("SPFH", "2 m above ground", b"S" * 100)])
    p = "/EAGLE_ensemble/aigefs.20261118/00/mem004/model/atmos/grib2/aigefs.t00z.sfc.f024.grib2"
    Handler.files[p], Handler.files[p + ".idx"] = blob, idx.encode()
    r = AigefsFamily(cfg).fetch_member(mk(), RUN, None, "ai04", 24, None, True)
    assert set(r.wind) == {"wind"} and float(r.wind["wind"].values[3, 3]) == pytest.approx(5.0 * MPH, abs=1e-3)
    assert [rg for m, pp, rg in Handler.seen if rg]                                        # ranged
    assert len([1 for m, pp, rg in Handler.seen if rg]) == 1                              # ONE read for u, v and MSLP
    assert len([1 for m, pp, rg in Handler.seen if pp.endswith(".idx")]) == 1


def test_member_keeps_its_mslp_when_the_wind_messages_are_missing(server):
    cfg = gefs_cfg(server)
    blob, idx = idx_file([("PRMSL", "mean sea level", msg("prmsl", 100000.0)), ("TMP", "2 m above ground", b"T" * 200)])
    Handler.files[gefs_path("gec00", 6)] = blob
    Handler.files[gefs_path("gec00", 6) + ".idx"] = idx.encode()
    r = GefsFamily(cfg).fetch_member(mk(), RUN, cfg.source.products[0], "gec00", 6, None, True)
    assert r.grid is not None and r.wind is None and isinstance(r.wind_error, KeyError)


def test_small_gaps_are_bridged_into_one_request_by_default(server):
    cfg = gefs_cfg(server)                                                                # default merge_gap_kb = 1024
    blob, idx = idx_file([("GUST", "surface", msg("gust", 20.0)), ("PRMSL", "mean sea level", msg("prmsl", 100000.0)),
                          ("TMP", "2 m above ground", b"T" * 200),
                          ("UGRD", "10 m above ground", msg("10u", 6.0)), ("VGRD", "10 m above ground", msg("10v", 8.0))])
    Handler.files[gefs_path("gep01", 12)] = blob
    Handler.files[gefs_path("gep01", 12) + ".idx"] = idx.encode()
    r = GefsFamily(cfg).fetch_member(mk(), RUN, cfg.source.products[0], "gep01", 12, None, True)
    assert set(r.wind) == {"wind", "gust"}
    assert len([1 for m, p, rg in Handler.seen if rg]) == 1                               # gust, MSLP, u, v all inside 1 MB


# ---- estimated gust (GEPS, AIGEFS) ---------------------------------------------------------------------
def _coast_grid(speed):
    """0.5-degree grid over the US East Coast: land (Ohio valley) at the north-west, open Atlantic at the south-east."""
    lats, lons = np.arange(30.0, 45.01, 0.5), np.arange(-90.0, -55.01, 0.5)
    return Grid(lats, lons, np.full((len(lats), len(lons)), speed, np.float32), "2026-11-18T06:00:00Z", 6)


def test_estimated_gust_is_larger_over_land_than_water_and_never_below_the_wind():
    from gefs_pipeline.gustest import estimate_gust
    est = load_config().wind.estimate
    for sp in (5.0, 15.0, 30.0, 60.0):
        g = estimate_gust(_coast_grid(sp), est)
        land, sea = g.values[int((40 - 30) / 0.5), int((-85 + 90) / 0.5)], g.values[int((32 - 30) / 0.5), int((-60 + 90) / 0.5)]
        assert land > sea >= sp                                                         # Ohio vs open Atlantic
        assert 1.2 <= sea / sp <= 1.5 and 1.3 <= land / sp <= 2.1
    assert g.short == "gust_est" and g.values.dtype == np.float32
    z = estimate_gust(_coast_grid(0.0), est)
    assert float(z.values.max()) == 0.0                                                 # calm stays calm


def test_estimate_applied_only_to_listed_systems_without_gust_and_flagged_in_the_manifest_entry(tmp_path):
    from gefs_pipeline.pipeline import _with_estimated_gust
    cfg = load_config()
    members = ["c0", "p1", "p2"]
    wind = {"wind": {m: _coast_grid(10.0 + 10 * i) for i, m in enumerate(members)}}
    geps = SimpleNamespace(key="geps", has_gust=False)
    out, flagged = _with_estimated_gust(cfg, geps, wind)
    assert flagged and set(out["gust"]) == set(members) and out["wind"] is wind["wind"]
    assert float(out["gust"]["p2"].values.max()) > float(out["gust"]["c0"].values.max())      # per member, before any statistics
    assert _with_estimated_gust(cfg, SimpleNamespace(key="gefs", has_gust=True), wind) == (wind, False)           # real gust wins
    assert _with_estimated_gust(cfg, SimpleNamespace(key="ifs", has_gust=False), wind) == (wind, False)           # not in [wind.estimate]
    assert _with_estimated_gust(cfg, geps, {"wind": wind["wind"], "gust": wind["wind"]})[1] is False            # never overwrite a gust
    assert _with_estimated_gust(cfg, geps, None) == (None, False)
    # the frame entry says so, and the gust files are written like any other gust
    ff = FamilyFrame(SimpleNamespace(key="geps", members=members, plan_tag=lambda p: "x"), cfg.source.products[0],
                     {m: _coast_grid(1000.0) for m in members}, members, out, gust_estimated=True)
    entry = build_frame(cfg, {"geps": ff}, ["geps"], 6, tmp_path, "r")
    assert entry["avail"]["geps"]["gust"]["estimated"] is True and "estimated" not in entry["avail"]["geps"]["wind"]
    assert (tmp_path / "r" / "geps" / "gmax" / "f006.bin.gz").exists()


def test_manifest_gives_wind_and_gust_their_own_legend_colours(tmp_path):
    """End to end through build(): wind products carry the wind legend, gust products the gust legend, probability its own % scale."""
    import json
    from datetime import datetime, timezone
    from gefs_pipeline.fetcher import Http
    from gefs_pipeline.pipeline import build
    from gefs_pipeline.sources import Family, MemberResult
    from gefs_pipeline.palette import explicit_scale

    cfg = load_config()
    cfg.wind.families = ["ff"]                                                            # wind is only fetched for the systems listed in [wind]
    run = datetime(2026, 11, 19, 0, tzinfo=timezone.utc)
    lats, lons = np.arange(30.0, 40.01, 2.5), np.arange(-80.0, -69.99, 2.5)

    class Fake(Family):
        key, label, name, org, note = "ff", "FF", "FF", "T", ""
        has_wind, has_gust = True, True

        def __init__(self, c):
            super().__init__(c)
            self.members = ["ff00", "ff01", "ff02"]

        def max_fh(self, r):
            return 240

        def plan(self, http, r, fh):
            return "p"

        def plan_tag(self, plan):
            return "fake"

        def fetch_member(self, http, r, plan, member, fh, cache_dir, want_wind=False):
            g = lambda v: Grid(lats, lons, np.full((len(lats), len(lons)), v, np.float32), f"2026-11-19T{fh % 24:02d}:00:00Z", fh)
            return MemberResult(g(1010.0), {"wind": g(30.0), "gust": g(45.0)} if want_wind else None)

    out = tmp_path / "site"
    build(cfg, run=run, out=out, cache_dir=None, http=Http(user_agent="t", rps=0), workers=2, families=[Fake(cfg)], fhs=[0, 6])
    prods = json.loads((out / "data" / "manifest.json").read_text())["products"]
    we, wc, _ = explicit_scale(cfg.wind_scale)
    ge, gc, _ = explicit_scale(cfg.gust_scale)
    for k in ("wmean", "wmax", "wswath"):
        assert prods[k]["edges"] == we and prods[k]["colors"] == wc and prods[k]["units"] == "mph"
    for k in ("gmean", "gmax", "gswath"):
        assert prods[k]["edges"] == ge and prods[k]["colors"] == gc and prods[k]["units"] == "mph"
    assert prods["wprob"]["units"] == "%" and prods["wprob"]["colors"] == prods["gprob"]["colors"]
    assert prods["wmean"]["colors"][0] == "transparent" and prods["gmean"]["colors"][0] == "#1c4577"
