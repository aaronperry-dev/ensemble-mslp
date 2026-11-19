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

    # 1. lows of a minimum field: same centres and values as the Python detector
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
