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
    entry = build_frame(cfg, {"gefs": a, "ifs": b}, ["gefs", "ifs"], 12, scales, tmp_path, "r", {"gefs": 0, "ifs": 3})
    assert set(entry["avail"]) == {"gefs", "ifs", "blend"} and entry["avail"]["blend"]["members"] == 5

    bm = read(tmp_path, "blend", "mean")
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

    mn = read(tmp_path, "blend", "min")
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
    cfg.blend.weighting = "pooled"
    a = frame("g", [1000.0, 1000.0, 1000.0])
    b = frame("i", [1010.0])
    scales = {"mean": ([960 + 4 * i for i in range(20)], ["#000000"] * 21)}
    scales["min"] = scales["mean"]
    build_frame(cfg, {"gefs": a, "ifs": b}, ["gefs", "ifs"], 12, scales, tmp_path, "r", {"gefs": 0, "ifs": 3})
    v = np.array(read(tmp_path, "blend", "mean")["grid"]["v"], float) / 10
    assert v[0] == pytest.approx((3 * 1000 + 1010) / 4, abs=0.2)           # 1002.5


def test_no_blend_when_a_family_is_missing_the_hour(tmp_path):
    cfg = load_config()
    scales = {"mean": ([960 + 4 * i for i in range(20)], ["#000000"] * 21)}
    scales["min"] = scales["mean"]
    entry = build_frame(cfg, {"gefs": frame("g", [1000.0, 1004.0])}, ["gefs", "ifs"], 12, scales, tmp_path, "r",
                        {"gefs": 0, "ifs": 2})
    assert set(entry["avail"]) == {"gefs"}
    assert not (tmp_path / "r" / "blend").exists()
