"""Blend maths: equal-weight mean of family means, lowest-of-all-members minimum, member-index offsets, per-system tooltip grids."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from gefs_pipeline.config import load_config
from gefs_pipeline.decode import Grid
from gefs_pipeline.pipeline import FamilyFrame, build_frame

LATS = np.arange(20.0, 50.01, 0.5)
LONS = np.arange(-110.0, -69.99, 0.5)
LON, LAT = np.meshgrid(LONS, LAT := LATS)


def fam(key, members):
    return SimpleNamespace(key=key, members=members, plan_tag=lambda plan, k=key: k + "-src")


def grid(level, low=None):
    f = np.full(LON.shape, level, dtype="f4")
    if low:
        lon, lat, depth = low
        f -= depth * np.exp(-(((LON - lon) / 4) ** 2 + ((LAT - lat) / 3) ** 2))
    return Grid(LATS, LONS, f.astype("f4"), "2026-11-18T12:00:00Z", 12)


def frame(key, levels, lows=None):
    names = [f"{key}{i:02d}" for i in range(len(levels))]
    f = fam(key, names)
    grids = {n: grid(l, (lows or {}).get(i)) for i, (n, l) in enumerate(zip(names, levels))}
    return FamilyFrame(f, None, grids, names)


def read(out, view, prod):
    return json.loads((out / "r" / view / prod / "f012.json").read_text())


def test_blend_equal_weight_min_owner_and_offsets(tmp_path):
    cfg = load_config()
    cfg.blend.weighting = "equal"
    a = frame("g", [1010.0, 1010.0, 1010.0], {1: (-90, 35, 25)})            # member g01 has a deep low (~985)
    b = frame("i", [1008.0, 1012.0], {0: (-80, 30, 30)})                    # member i00 has a deeper low (~978)
    scales = {"mean": ([960 + 4 * i for i in range(20)], ["#000000"] * 21),
              "min": ([960 + 4 * i for i in range(20)], ["#000000"] * 21)}
    entry = build_frame(cfg, {"gefs": a, "ifs": b}, ["gefs", "ifs"], 12, scales, tmp_path, "r")
    assert set(entry["avail"]) == {"gefs", "ifs", "gefs_ifs"} and entry["avail"]["gefs_ifs"]["members"] == 5

    bm = read(tmp_path, "gefs_ifs", "mean")
    gv = np.array(bm["grid"]["v"], float) / 10
    corner = gv[0]                                                          # (20N, -110) is far from both lows
    # equal weight: (mean of GEFS = 1010 + mean(1008,1012)=1010) / 2 = 1010, whatever the member counts are
    assert corner == pytest.approx(1010.0, abs=0.2)
    fam_g = np.array(bm["grid"]["fam"]["gefs"], float) / 10
    fam_i = np.array(bm["grid"]["fam"]["ifs"], float) / 10
    assert fam_g[0] == pytest.approx(1010.0, abs=0.2) and fam_i[0] == pytest.approx(1010.0, abs=0.2)
    assert np.nanmin(np.array(bm["grid"]["v"], float) / 10) > 990        # means smooth the lows out
    # blended mean == average of the per-system grids everywhere
    assert np.allclose(gv, (fam_g + fam_i) / 2, atol=0.15, equal_nan=True)

    mn = read(tmp_path, "gefs_ifs", "min")
    mv = np.array(mn["grid"]["v"], float) / 10
    assert mv[0] == pytest.approx(1008.0, abs=0.2)                          # lowest of the five members
    assert np.nanmin(mv) < 985                                              # deep lows survive in the envelope
    # member lows: g01 keeps index 1, i00 is offset by the 3 GEFS members
    planted = [l for l in bm["lows"] if abs(l[0] + 90) < 2 and abs(l[1] - 35) < 2 or abs(l[0] + 80) < 2 and abs(l[1] - 30) < 2]
    assert {l[3] for l in planted} == {1, 3 + 0}
    # the envelope's deepest low is credited to the member that owns it (IFS member 0 -> blend index 3)
    deepest = min(mn["lows"], key=lambda l: l[2])
    assert deepest[3] == 3 and deepest[2] < 980


def test_pooled_weighting_counts_every_member_once(tmp_path):
    cfg = load_config()
    cfg.blend.weighting = "members"
    a = frame("g", [1000.0, 1000.0, 1000.0])
    b = frame("i", [1010.0])
    scales = {"mean": ([960 + 4 * i for i in range(20)], ["#000000"] * 21)}
    scales["min"] = scales["mean"]
    build_frame(cfg, {"gefs": a, "ifs": b}, ["gefs", "ifs"], 12, scales, tmp_path, "r")
    v = np.array(read(tmp_path, "gefs_ifs", "mean")["grid"]["v"], float) / 10
    assert v[0] == pytest.approx((3 * 1000 + 1010) / 4, abs=0.2)           # 1002.5


def test_no_blend_when_a_family_is_missing_the_hour(tmp_path):
    cfg = load_config()
    scales = {"mean": ([960 + 4 * i for i in range(20)], ["#000000"] * 21)}
    scales["min"] = scales["mean"]
    entry = build_frame(cfg, {"gefs": frame("g", [1000.0, 1004.0])}, ["gefs", "ifs"], 12, scales, tmp_path, "r")
    assert set(entry["avail"]) == {"gefs"}
    assert not (tmp_path / "r" / "gefs_ifs").exists()


def coarse_frame(key, levels, lows=None):
    """Same domain on a 1-degree grid (like GEPS next to a finer family): exercises regridding in the blend."""
    la, lo = np.arange(20.0, 50.01, 1.0), np.arange(-110.0, -69.99, 1.0)
    LO, LA = np.meshgrid(lo, la)
    names = [f"{key}{i:02d}" for i in range(len(levels))]
    grids = {}
    for i, (n, l) in enumerate(zip(names, levels)):
        f = np.full(LO.shape, l, dtype="f4")
        if lows and i in lows:
            x, y, d = lows[i]
            f -= d * np.exp(-(((LO - x) / 4) ** 2 + ((LA - y) / 3) ** 2))
        grids[n] = Grid(la, lo, f.astype("f4"), "2026-11-18T12:00:00Z", 12)
    return FamilyFrame(fam(key, names), None, grids, names)


def test_three_way_blend_all_combinations_offsets_and_regrid(tmp_path):
    cfg = load_config()
    cfg.blend.weighting = "equal"
    cfg.blend.subsets = True
    a = frame("g", [1010.0, 1010.0, 1010.0])                              # 0.5 deg, 3 members
    b = frame("i", [1008.0, 1012.0])                                      # 0.5 deg, 2 members
    c = coarse_frame("c", [1020.0, 1004.0], {1: (-85, 33, 30)})           # 1.0 deg, 2 members; member c01 has a deep low
    scales = {"mean": ([960 + 4 * i for i in range(20)], ["#000000"] * 21)}
    scales["min"] = scales["mean"]
    entry = build_frame(cfg, {"gefs": a, "ifs": b, "geps": c}, ["gefs", "ifs", "geps"], 12, scales, tmp_path, "r")
    assert set(entry["avail"]) == {"gefs", "ifs", "geps", "gefs_ifs_geps", "gefs_ifs", "gefs_geps", "ifs_geps"}
    assert entry["avail"]["gefs_ifs_geps"]["members"] == 7 and entry["avail"]["ifs_geps"]["members"] == 4

    full = read(tmp_path, "gefs_ifs_geps", "mean")
    v = np.array(full["grid"]["v"], float) / 10
    fg, fi, fc = (np.array(full["grid"]["fam"][k], float) / 10 for k in ("gefs", "ifs", "geps"))
    assert v[0] == pytest.approx((1010 + 1010 + 1012) / 3, abs=0.3)       # equal weight: each of the 3 systems counts once
    assert np.allclose(v, (fg + fi + fc) / 3, atol=0.2, equal_nan=True)
    assert fc[0] == pytest.approx(1012.0, abs=0.2)                         # GEPS mean (1020+1004)/2 on the finer grid

    # offsets are relative to THIS blend's member list (gefs 0-2, ifs 3-4, geps 5-6)
    deepest = min(read(tmp_path, "gefs_ifs_geps", "min")["lows"], key=lambda l: l[2])
    assert deepest[3] == 3 + 2 + 1 and deepest[2] < 985
    # ... and a different one in the pair that has no GEFS (ifs 0-1, geps 2-3)
    deepest2 = min(read(tmp_path, "ifs_geps", "min")["lows"], key=lambda l: l[2])
    assert deepest2[3] == 2 + 1

    cfg.blend.subsets = False
    entry = build_frame(cfg, {"gefs": a, "ifs": b, "geps": c}, ["gefs", "ifs", "geps"], 12, scales, tmp_path / "x", "r")
    assert set(entry["avail"]) == {"gefs", "ifs", "geps", "gefs_ifs_geps"}


def test_blends_only_include_families_that_have_the_hour(tmp_path):
    cfg = load_config()
    cfg.blend.subsets = True
    scales = {"mean": ([960 + 4 * i for i in range(20)], ["#000000"] * 21)}
    scales["min"] = scales["mean"]
    entry = build_frame(cfg, {"gefs": frame("g", [1010.0, 1012.0]), "geps": coarse_frame("c", [1008.0, 1009.0])},
                        ["gefs", "ifs", "geps"], 12, scales, tmp_path, "r")
    assert set(entry["avail"]) == {"gefs", "geps", "gefs_geps"}           # IFS missing -> only the GEFS+GEPS blend
