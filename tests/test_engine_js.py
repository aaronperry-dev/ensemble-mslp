"""The browser engine (site/engine.js) against the Python reference, run under Node on files the pipeline wrote:
lows detection identical to products.detect_lows, swath = exact running minimum, blend = lowest over regridded systems."""
import json
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.interpolate import RegularGridInterpolator

from gefs_pipeline import gridio
from gefs_pipeline.config import load_config
from gefs_pipeline.decode import Grid
from gefs_pipeline.pipeline import FamilyFrame, build_frame
from gefs_pipeline.products import detect_lows
from tests.test_gridio import LATS, LONS, field, fam

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def mkframe(key, levels, lats, lons, lows):
    names = [f"{key}{i:02d}" for i in range(len(levels))]
    lon, lat = np.meshgrid(lons, lats)
    grids = {}
    for i, (n, l) in enumerate(zip(names, levels)):
        f = l + 8 * np.sin(lon / 11) * np.cos(lat / 9)
        if i in lows:
            x, y, d = lows[i]
            f = f - d * np.exp(-(((lon - x) / 5) ** 2 + ((lat - y) / 4) ** 2))
        grids[n] = Grid(lats, lons, f.astype("f4"), "2026-11-18T12:00:00Z", 12)
    return FamilyFrame(fam(key, names), None, grids, names)


def test_engine_matches_python(tmp_path):
    cfg = load_config()
    coarse_lats, coarse_lons = np.arange(7.0, 68.01, 0.5), np.arange(-153.0, -36.99, 0.5)
    for fh, shift in ((6, -110), (12, -100), (18, -90)):                      # a low moving east
        frames = {"gefs": mkframe("g", [1010.0, 1012.0, 1008.0], LATS, LONS, {1: (shift, 35, 30)}),
                  "geps": mkframe("c", [1011.0, 1009.0], coarse_lats, coarse_lons, {0: (shift + 20, 40, 25)})}
        build_frame(cfg, frames, ["gefs", "geps"], fh, tmp_path, "r")
    run = tmp_path / "r"
    lm = {k: getattr(cfg.lows_min, k) for k in ("threshold_hpa", "radius_deg", "smooth_deg", "min_separation_deg")}
    d = cfg.domain
    r = subprocess.run(["node", str(ROOT / "tests/js/parity.js"), str(run), "18", json.dumps(lm),
                        json.dumps({"west": d.west, "east": d.east, "south": d.south, "north": d.north}), "gefs,geps"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)

    # 1. lows of a minimum field: same centers and values as the Python detector
    for k in ("gefs", "geps"):
        hdr, f, _ = gridio.decode((run / k / "min" / "f018.bin.gz").read_bytes())
        lats = hdr["lat0"] + np.arange(hdr["ny"]) * hdr["dlat"]; lons = hdr["lon0"] + np.arange(hdr["nx"]) * hdr["dlon"]
        py = detect_lows(lats, lons, f.astype("f4"), threshold_hpa=lm["threshold_hpa"], radius_deg=lm["radius_deg"],
                         smooth_deg=lm["smooth_deg"], min_sep_deg=lm["min_separation_deg"],
                         domain=(d.west, d.east, d.south, d.north))
        got = [(round(a, 4), round(b, 4), round(c, 1)) for a, b, c, _, _ in js["systems"][k]["lows"]]
        assert got == [(round(a, 4), round(b, 4), round(c, 1)) for a, b, c in py] and len(got) >= 1

    # 2. swath = exact running minimum, with the hour it was reached
    hours = js["swath"]["hours"]
    assert hours == [6, 12, 18]
    stack = np.stack([gridio.decode((run / "gefs" / "min" / f"f{h:03d}.bin.gz").read_bytes())[1] for h in hours])
    sw = np.array(js["swath"]["tenths"], float).reshape(stack.shape[1:]) / 10
    assert np.array_equal(sw, stack.min(0).astype("f4").round(1)) or np.nanmax(np.abs(sw - stack.min(0))) < 1e-4
    when = np.array(js["swath"]["when"]).reshape(sw.shape)
    assert np.array_equal(when, np.array(hours)[stack.argmin(0)])
    assert set(np.unique(when)) == {6, 12, 18}

    # 3. blend minimum: lowest of GEFS and GEPS (bilinearly regridded onto the GEFS grid); owner offsets by GEFS' 3 members
    _, fg, og = gridio.decode((run / "gefs" / "min" / "f018.bin.gz").read_bytes())
    hc, fc, oc = gridio.decode((run / "geps" / "min" / "f018.bin.gz").read_bytes())
    interp = RegularGridInterpolator((hc["lat0"] + np.arange(hc["ny"]) * hc["dlat"], hc["lon0"] + np.arange(hc["nx"]) * hc["dlon"]),
                                     fc, bounds_error=False, fill_value=np.nan)
    la, lo = np.meshgrid(LATS, LONS, indexing="ij")
    fc_on_g = interp((la, lo))
    want = np.where(np.isfinite(fc_on_g) & (fc_on_g < fg), fc_on_g, fg)
    got = np.array([np.nan if x is None else x for x in js["blend"]["v"]]).reshape(want.shape)
    assert np.nanmax(np.abs(got - want)) < 0.02
    own = np.array(js["blend"]["owner"]).reshape(want.shape)
    assert own.max() >= 3 and own.min() >= 0                                  # GEPS members are indexed after GEFS' three
    deepest = np.unravel_index(np.nanargmin(got), got.shape)
    assert got[deepest] < 990


# ---------------------------------------------------------------------------------------------------
# probability planes and strike probability
# ---------------------------------------------------------------------------------------------------
def py_strike(tracks, geom, radius_km, level, frame_fhs):
    """Independent reference: first frame at which each member has a qualifying low within the radius of each cell."""
    nx, ny = geom["nx"], geom["ny"]
    lons = geom["lon0"] + np.arange(nx) * geom["dlon"]
    lats = geom["lat0"] + np.arange(ny) * geom["dlat"]
    X, Y = np.meshgrid(lons, lats)
    frame_fhs = np.array(frame_fhs)
    nf = len(frame_fhs)
    per_member = {}
    for t in tracks:
        first = per_member.setdefault(t["m"], np.full((ny, nx), nf))
        pts = t["pts"]
        samples = []
        for a, z in zip(pts, pts[1:] + [None]):
            if z is None:
                samples.append((a[0], a[1], a[2], a[3]))
                continue
            for h in range(a[0], z[0]):
                u = (h - a[0]) / (z[0] - a[0])
                samples.append((h, a[1] + (z[1] - a[1]) * u, a[2] + (z[2] - a[2]) * u, a[3] + (z[3] - a[3]) * u))
        for h, lon, lat, p in samples:
            if not p < level:
                continue
            b = int(np.searchsorted(frame_fhs, h, side="left"))            # first frame with fh >= h
            cl = max(0.05, np.cos(np.radians(lat)))
            d2 = ((Y - lat) * 111.195) ** 2 + ((X - lon) * 111.195 * cl) ** 2
            first[d2 <= radius_km ** 2] = np.minimum(first[d2 <= radius_km ** 2], b)
    cum = np.zeros((nf, ny, nx), int)
    for first in per_member.values():
        for f in range(nf):
            cum[f] += first <= f
    return cum


def test_prob_planes_and_strike_probability_match_python(tmp_path):
    cfg = load_config()
    a = mkframe("g", [1010.0, 1004.0, 996.0], LATS, LONS, {2: (-90, 35, 40)})
    build_frame(cfg, {"gefs": a}, ["gefs"], 12, tmp_path, "r")
    geom = {"nx": 61, "ny": 41, "lon0": -110.0, "lat0": 20.0, "dlon": 0.5, "dlat": 0.5}
    tracks = [
        {"m": 0, "pts": [[0, -108.0, 30.0, 1001.0], [6, -104.0, 31.0, 997.0], [12, -100.0, 33.0, 990.0], [18, -96.0, 36.0, 984.0]]},
        {"m": 0, "pts": [[12, -90.0, 25.0, 1003.0], [18, -88.0, 26.0, 1002.0], [24, -86.0, 27.0, 1001.0]]},     # second track, same member
        {"m": 1, "pts": [[0, -106.0, 30.5, 1002.0], [6, -101.0, 33.0, 1000.0], [12, -97.0, 38.0, 993.0]]},
        {"m": 2, "pts": [[6, -95.0, 22.0, 1004.0], [12, -92.0, 24.0, 1004.5], [18, -89.0, 26.0, 1004.2]]},       # never below 1000
    ]
    frame_fhs = [0, 6, 12, 18, 24]
    for radius, level in ((200.0, 1005.0), (300.0, 995.0), (500.0, 1001.0)):
        inp = tmp_path / "in.json"
        inp.write_text(json.dumps({"tracks": tracks, "geom": geom, "radius_km": radius, "level": level,
                                   "frame_fhs": frame_fhs, "n_members": 3}))
        r = subprocess.run(["node", str(ROOT / "tests/js/strike_prob.js"), str(tmp_path / "r/gefs/prob/f012.bin.gz"), str(inp)],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stderr
        js = json.loads(r.stdout)
        # probability planes decode to the exact member counts the Python side wrote
        _, planes = gridio.decode_counts((tmp_path / "r/gefs/prob/f012.bin.gz").read_bytes())
        assert js["levels"] == [float(x) for x in cfg.prob.levels] and js["members"] == 3
        assert np.array_equal(np.array(js["planes"]).reshape(planes.shape), planes)
        # strike counts: identical to the reference, never decreasing in time, bounded by the member count
        ref = py_strike(tracks, geom, radius, level, frame_fhs)
        got = np.array(js["cum"]).reshape(ref.shape)
        assert np.array_equal(got, ref), (radius, level, int((got != ref).sum()))
        assert (np.diff(got, axis=0) >= 0).all() and got.max() <= 3
        if level == 995.0:
            assert got[-1].max() == 2 and got[0].max() == 0          # member 2 never gets below 995; hits start after F000's frame


def test_decoding_one_probability_level_gives_the_same_plane(tmp_path):
    cfg = load_config()
    a = mkframe("g", [1010.0, 1004.0, 996.0], LATS, LONS, {2: (-90, 35, 40)})
    build_frame(cfg, {"gefs": a}, ["gefs"], 12, tmp_path, "r")
    level = float(cfg.prob.levels[2])
    r = subprocess.run(["node", str(ROOT / "tests/js/plane_only.js"), str(tmp_path / "r/gefs/prob/f012.bin.gz"), str(level)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    assert js["filled"] == [2] and js["nfull"] == len(cfg.prob.levels)      # only the requested plane is decoded
    assert js["same"] and js["pct"] and js["members"] == 3


def test_wind_grids_scale_swath_and_highest_of_systems(tmp_path):
    """Wind / gust files use half-mph steps: the engine honours the header scale, the swath keeps the running MAXIMUM (with the hour and
    member that set it) and the highest-of-systems blend picks the larger value; the lowest-of default is unchanged."""
    cfg = load_config()
    rng = np.random.default_rng(7)

    def wframe(key, base, fh):
        ff = mkframe(key, [1010.0, 1004.0, 996.0], LATS, LONS, {})
        lon, lat = np.meshgrid(LONS, LATS)
        wg = {}
        for i, n in enumerate(ff.names):
            sp = base + 5 * i + 3 * np.sin(lon / 9) * np.cos(lat / 7) + rng.normal(0, 1, lon.shape) + (fh == 12) * 12 * np.exp(-(((lon + 90) / 8) ** 2 + ((lat - 35) / 6) ** 2)) * (i == 1)
            wg[n] = Grid(LATS, LONS, np.clip(sp, 0, None).astype("f4"), "2026-11-18T12:00:00Z", fh)
        ff.wind = {"wind": wg}
        return ff

    build_frame(cfg, {"gefs": wframe("g", 12.0, 6)}, ["gefs"], 6, tmp_path, "a")
    build_frame(cfg, {"gefs": wframe("g", 14.0, 12)}, ["gefs"], 12, tmp_path, "a")
    build_frame(cfg, {"gefs": wframe("h", 20.0, 12)}, ["gefs"], 12, tmp_path, "b")
    r = subprocess.run(["node", str(ROOT / "tests/js/wind_swath.js"), str(tmp_path / "a/gefs/wmax/f006.bin.gz"),
                        str(tmp_path / "a/gefs/wmax/f012.bin.gz"), str(tmp_path / "b/gefs/wmax/f012.bin.gz")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    assert js["scale"] == 2 and js["stscale"] == 2
    a6, a12 = np.array(js["a6"]), np.array(js["a12"])
    assert 5 < a6.mean() < 60                                       # mph, not tenths: a wrong scale would give 5x too large / small values
    assert np.allclose(np.array(js["swath"]), np.maximum(a6, a12))   # running MAXIMUM
    w = np.array(js["when"])
    assert set(np.unique(w)) <= {6, 12} and (w[np.array(js["swath"]) > a6 + 1e-6] == 12).all()
    hi, lo = np.array(js["hi"]), np.array(js["lo"])
    assert (hi >= lo).all() and (hi > lo).any()
    assert set(np.unique(np.array(js["hiOwner"]))) <= set(range(0, 13))   # owners of system b are offset by 10
    # quantisation: half-mph steps
    assert np.allclose(a12 * 2, np.round(a12 * 2))


def test_rasterize_calm_skip_is_pixel_identical():
    """Skipping pixels whose four source cells are all in the transparent classes changes no pixel (edges, NaNs, three colour tables)."""
    r = subprocess.run(["node", str(ROOT / "tests/js/raster_calm.js")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    cases = json.loads(r.stdout)["cases"]
    assert len(cases) == 6 and all(c["diff"] == 0 and c["drawn"] > 0 for c in cases)
    calm = {c["name"]: c["calm"] for c in cases}
    assert calm["wind"] == pytest.approx(25 - 0.06) and calm["prob"] == pytest.approx(5 - 0.06) and calm["mslp"] is None   # -Infinity -> null
