import re
from types import SimpleNamespace as NS

import pytest

from gefs_pipeline.config import load_config
from gefs_pipeline.palette import RAMPS, build_scale, color_at, edges_from_spec, hex_to_oklab, oklab_to_hex


def test_ramps_are_valid_hex_and_official_lengths():
    for name, ramp in RAMPS.items():
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in ramp), name
        assert len(ramp) in (9, 11)


def test_ramps_match_colorbrewer_reference():
    pal = pytest.importorskip("palettable.colorbrewer")
    for name, ref in {"YlOrRd": pal.sequential.YlOrRd_9, "YlOrBr": pal.sequential.YlOrBr_9, "YlGnBu": pal.sequential.YlGnBu_9,
                      "PuBu": pal.sequential.PuBu_9, "RdPu": pal.sequential.RdPu_9, "Blues": pal.sequential.Blues_9, "RdBu": pal.diverging.RdBu_11,
                      "RdYlBu": pal.diverging.RdYlBu_9, "PuOr": pal.diverging.PuOr_11}.items():
        assert [h.lower() for h in ref.hex_colors] == RAMPS[name], name


def test_oklab_roundtrip():
    for h in RAMPS["RdBu"]:
        assert oklab_to_hex(*hex_to_oklab(h)) == h


def test_color_at_endpoints():
    assert color_at(RAMPS["YlOrRd"], 0) == RAMPS["YlOrRd"][0] and color_at(RAMPS["YlOrRd"], 1) == RAMPS["YlOrRd"][-1]
    assert color_at(RAMPS["RdYlBu"], 0.5) == RAMPS["RdYlBu"][4]            # exact anchor, no drift


def test_shared_scale_layout():
    cfg = load_config()
    edges, colors, segs = build_scale(cfg.scale)
    assert edges[0] == 948 and edges[-1] == 1032 and len(colors) == len(edges) + 1 == 23
    assert [s["classes"] for s in segs] == [9, 5, 9]
    assert [s["ramp"] for s in segs] == ["RdPu", "Blues", "RdYlBu"]
    assert (segs[0]["to_hpa"], segs[1]["to_hpa"], segs[2]["from_hpa"]) == (980, 1000, 1000)


def test_at_or_above_1000_is_exactly_colorbrewer_rdylbu9_blue_to_red():
    cfg = load_config()
    edges, colors, _ = build_scale(cfg.scale)
    first_ge_1000 = edges.index(1000) + 1                      # class [1000, 1004)
    assert colors[first_ge_1000:] == RAMPS["RdYlBu"][::-1]     # blue (just above 1000) ... red (>= 1032)
    assert colors[edges.index(1016) + 1] == "#ffffbf"          # neutral pale yellow at 1016-1020 hPa


def test_below_1000_blues_then_rdpu_getting_more_intense_downwards():
    cfg = load_config()
    edges, colors, _ = build_scale(cfg.scale)
    blues = colors[edges.index(980) + 1: edges.index(1000) + 1]   # [980,984) .. [996,1000)
    assert blues[0] == RAMPS["Blues"][8] and blues[-1] == RAMPS["Blues"][6]   # navy at 980, ColorBrewer #2171b5 at ~1000
    # blues get darker (lower OKLab lightness) as pressure falls
    L = [hex_to_oklab(c)[0] for c in blues]
    assert L == sorted(L)
    rdpu = colors[: edges.index(980) + 1]
    assert rdpu[0] == RAMPS["RdPu"][8] and rdpu[-1] == RAMPS["RdPu"][4]      # darkest purple lowest, bright pink at 976-980


def test_edges_inclusive():
    assert edges_from_spec(NS(start=988, stop=1004, step=4)) == [988, 992, 996, 1000, 1004]
