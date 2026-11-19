"""Binary grid format shipped to the browser: lossless round trip, gzip, small size, per-system output of build_frame."""
import gzip
import json
from types import SimpleNamespace

import numpy as np

from gefs_pipeline import gridio
from gefs_pipeline.config import load_config
from gefs_pipeline.decode import Grid
from gefs_pipeline.pipeline import FamilyFrame, build_frame

LATS = np.arange(7.0, 68.01, 0.25)
LONS = np.arange(-153.0, -36.99, 0.25)
LON, LAT = np.meshgrid(LONS, LATS)


def field(level=1012.0, low=None):
    f = level + 8 * np.sin(LON / 11) * np.cos(LAT / 9)
    if low:
        lon, lat, depth = low
        f = f - depth * np.exp(-(((LON - lon) / 5) ** 2 + ((LAT - lat) / 4) ** 2))
    return f.astype("f4")


def test_round_trip_is_exact_to_a_tenth_and_handles_missing():
    f = field(low=(-90, 35, 40))
    f[5, 7] = np.nan
    f[:, 0] = 1030.0                                   # a hard edge: large residuals
    hdr = {"kind": "min", "nx": f.shape[1], "ny": f.shape[0], "fh": 6}
    owner = (np.arange(f.size) % 51).reshape(f.shape).astype(np.uint8)
    blob = gridio.encode(hdr, f, owner)
    h, g, o = gridio.decode(blob)
    assert h == hdr
    assert np.isnan(g[5, 7]) and np.isfinite(g).sum() == f.size - 1
    assert np.nanmax(np.abs(g - f)) <= 0.0501          # rounded to 0.1 hPa, nothing else lost
    assert np.array_equal(o, owner)


def test_extreme_values_wrap_correctly():
    f = np.array([[800.0, 1100.0, 800.0], [1100.0, 800.0, 1100.0]], "f4")      # residuals near the 16-bit limits
    h, g, _ = gridio.decode(gridio.encode({"nx": 3, "ny": 2}, f))
    assert np.allclose(g, f, atol=0.05)


def test_smooth_fields_compress_far_below_raw_int16():
    f = field(low=(-80, 30, 30))
    blob = gridio.encode({"nx": f.shape[1], "ny": f.shape[0]}, f)
    assert gzip.decompress(blob)[:4] == b"GMS1"
    assert len(blob) < 0.25 * f.size * 2, (len(blob), f.size * 2)


def fam(key, members):
    return SimpleNamespace(key=key, members=members, plan_tag=lambda plan, k=key: k + "-src")


def frame(key, levels, lows=None):
    names = [f"{key}{i:02d}" for i in range(len(levels))]
    grids = {n: Grid(LATS, LONS, field(l, (lows or {}).get(i)), "2026-11-18T12:00:00Z", 12) for i, (n, l) in enumerate(zip(names, levels))}
    return FamilyFrame(fam(key, names), None, grids, names)


def test_build_frame_writes_only_per_system_grids(tmp_path):
    cfg = load_config()
    a = frame("g", [1010.0, 1008.0, 1012.0], {1: (-90, 35, 25)})
    b = frame("i", [1011.0, 1009.0])
    entry = build_frame(cfg, {"gefs": a, "ifs": b}, ["gefs", "ifs"], 12, tmp_path, "r")
    assert set(entry["avail"]) == {"gefs", "ifs"} and entry["avail"]["gefs"]["members"] == 3
    files = sorted(p.relative_to(tmp_path / "r").as_posix() for p in (tmp_path / "r").rglob("*") if p.is_file())
    assert files == ["gefs/mean/f012.bin.gz", "gefs/min/f012.bin.gz", "gefs/prob/f012.bin.gz",
                     "ifs/mean/f012.bin.gz", "ifs/min/f012.bin.gz", "ifs/prob/f012.bin.gz"]

    h, mean, _ = gridio.decode((tmp_path / "r/gefs/mean/f012.bin.gz").read_bytes())
    assert h["kind"] == "mean" and h["members"] == 3 and h["nx"] == len(LONS) and h["ny"] == len(LATS)
    assert h["lon0"] == -153.0 and h["lat0"] == 7.0 and h["dlon"] == 0.25 and h["dlat"] == 0.25
    stack = np.stack([g.values for g in a.grids.values()])
    assert np.allclose(mean, stack.mean(0), atol=0.06)
    assert any(abs(l[0] + 90) < 3 and abs(l[1] - 35) < 3 and l[3] == 1 for l in h["lows"])   # member g01's planted low

    h, mn, owner = gridio.decode((tmp_path / "r/gefs/min/f012.bin.gz").read_bytes())
    assert np.allclose(mn, stack.min(0), atol=0.06)
    assert np.array_equal(owner, stack.argmin(0).astype(np.uint8))
    assert owner[np.nanargmin(mn) // mn.shape[1], np.nanargmin(mn) % mn.shape[1]] == 1


def test_basemap_json_is_gzipped_in_place(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "a.json").write_text('{"k":1}')
    (tmp_path / "x" / "ATTRIBUTION.txt").write_text("t")
    gridio.gzip_json_tree(tmp_path)
    assert not (tmp_path / "x" / "a.json").exists() and (tmp_path / "x" / "ATTRIBUTION.txt").exists()
    assert json.loads(gzip.decompress((tmp_path / "x" / "a.json.gz").read_bytes())) == {"k": 1}


def test_prob_counts_are_exact_member_counts_below_each_level(tmp_path):
    cfg = load_config()
    a = frame("g", [1010.0, 1004.0, 996.0], {2: (-90, 35, 40)})        # member 2 has a deep low planted
    lows_out = {}
    build_frame(cfg, {"gefs": a}, ["gefs"], 12, tmp_path, "r", lows_out)
    h, planes = gridio.decode_counts((tmp_path / "r/gefs/prob/f012.bin.gz").read_bytes())
    levels = [float(x) for x in cfg.prob.levels]
    assert h["kind"] == "prob" and h["levels"] == levels and h["members"] == 3 and planes.shape == (len(levels), h["ny"], h["nx"])
    stack = np.stack([g.values for g in a.grids.values()])
    for k, lv in enumerate(levels):
        assert np.array_equal(planes[k], (stack < lv).sum(0)), lv
    assert planes[levels.index(1010.0)].max() == 3 and planes[levels.index(975.0)].max() == 1   # only the planted deep low gets below 975
    assert all(planes[k].max() <= planes[k + 1].max() for k in range(len(levels) - 1))          # counts never fall as the level rises
    assert 12 in lows_out["gefs"] and any(r[3] == 2 for r in lows_out["gefs"][12])               # lows are handed to the tracker


def test_count_planes_round_trip_and_are_small():
    rng = np.random.default_rng(1)
    base = (np.add.outer(np.arange(180), np.arange(260)) % 17 > 8).astype(np.int32) * 30
    planes = np.stack([base, base + 1, np.minimum(base + 2, 51)])
    blob = gridio.encode_counts({"nx": 260, "ny": 180, "levels": [1, 2, 3]}, planes)
    h, back = gridio.decode_counts(blob)
    assert np.array_equal(back, planes) and len(blob) < 3 * 260 * 180 / 8
