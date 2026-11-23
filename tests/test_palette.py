import numpy as np
import re
from types import SimpleNamespace as NS

import pytest

from gefs_pipeline.config import load_config
from gefs_pipeline.palette import RAMPS, build_scale, color_at, edges_from_spec, explicit_scale, hex_to_oklab, oklab_to_hex


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
    assert edges[0] == 925 and edges[-1] == 1030 and len(colors) == len(edges) + 1 == 23
    assert [s["label"] for s in segs] == ["Cat 5", "Cat 4", "Cat 3", "Cat 2", "Cat 1", "Storm", "Gale", ""]
    assert [s["classes"] for s in segs] == [1, 5, 3, 2, 3, 2, 1, 6]
    assert [s["ramp"] for s in segs] == ["RdPu"] * 5 + ["Blues", "Blues", "RdYlBu"]


def test_intensity_thresholds_are_class_boundaries():
    """Wind thresholds (Cat 5 >=137 kt ... gale 34 kt) mapped through NOAA's Dvorak table, rounded to 5 hPa."""
    cfg = load_config()
    edges, colors, segs = build_scale(cfg.scale)
    assert [(s["from_hpa"], s["to_hpa"]) for s in segs[:7]] == [
        (None, 925), (925, 950), (950, 965), (965, 975), (975, 990), (990, 1000), (1000, 1005)]
    for thr in (925, 950, 965, 975, 990, 1000, 1005):
        i = edges.index(thr)
        assert colors[i] != colors[i + 1]                       # the colour changes exactly at the threshold
    assert segs[7]["from_hpa"] == 1005
    # the five hurricane categories and the storm band all get darker with falling pressure
    L = [hex_to_oklab(c)[0] for c in colors[: edges.index(990) + 1]]
    assert L == sorted(L)


def test_dvorak_interpolation_gives_the_thresholds():
    """The edges are not arbitrary: reproduce them from the NOAA Dvorak (Atlantic) wind-pressure table."""
    kt = [30, 35, 45, 55, 65, 77, 90, 102, 115, 127, 140]
    mb = [1009, 1005, 1000, 994, 987, 979, 970, 960, 948, 935, 921]
    want = {34: 1005, 48: 1000, 64: 990, 83: 975, 96: 965, 113: 950, 137: 925}
    for wind, edge in want.items():
        p = float(np.interp(wind, kt, mb))
        assert abs(5 * round(p / 5) - edge) == 0, (wind, p, edge)


def test_below_gale_is_diverging_blue_to_red_with_pale_yellow_in_the_middle():
    cfg = load_config()
    edges, colors, _ = build_scale(cfg.scale)
    top = colors[edges.index(1005) + 1:]                         # [1005,1010) ... >= 1030
    assert len(top) == 6
    L = [hex_to_oklab(c)[0] for c in top]
    assert max(L) > L[0] and max(L) > L[-1]                      # lightest in the middle (pale yellow), darker at both ends
    assert top[-1] == RAMPS["RdYlBu"][0]                         # red for >= 1030


def test_storm_and_gale_blues_get_darker_downwards():
    cfg = load_config()
    edges, colors, _ = build_scale(cfg.scale)
    blues = colors[edges.index(990) + 1: edges.index(1005) + 1]  # [990,995) .. [1000,1005)
    assert len(blues) == 3 and blues[0] == RAMPS["Blues"][8]     # navy at 990-995
    L = [hex_to_oklab(c)[0] for c in blues]
    assert L == sorted(L)
    assert colors[0] == RAMPS["RdPu"][8]                         # darkest purple: Cat 5


def test_edges_inclusive():
    assert edges_from_spec(NS(start=988, stop=1004, step=4)) == [988, 992, 996, 1000, 1004]


# ---- wind speed and gust follow the supplied legends, class for class ---------------------------------------
# (class label as printed on the legend image, lower edge in mph, sampled colour)
WIND_LEGEND = [("5 to 10", 5, "#225ea8"), ("10 to 15", 10, "#2193c1"), ("15 to 20", 15, "#41b6c4"), ("20 to 25", 20, "#7fcdbb"),
               ("25 to 30", 25, "#b4d79e"), ("30 to 35", 30, "#e0ffa2"), ("35 to 40", 35, "#ffffbb"), ("40 to 45", 40, "#ffe97b"),
               ("45 to 50", 45, "#ffc400"), ("50 to 60", 50, "#ffaa00"), ("60 to 70", 60, "#ff600a")]
GUST_LEGEND = [("0-5", None, "#1c4577"), ("6-10", 5, "#225ea8"), ("11-15", 10, "#258db8"), ("16-20", 15, "#41b6c4"), ("21-25", 20, "#7fcdbb"),
               ("26-30", 25, "#afd09b"), ("31-35", 30, "#dfff9e"), ("36-40", 35, "#f9f9a4"), ("41-45", 40, "#fae473"), ("46-50", 45, "#ffc400"),
               ("51-60", 50, "#feaa01"), ("61-70", 60, "#ff5900"), ("71-80", 70, "#ff0000"), ("81-100", 80, "#a80000"), ("101-120", 100, "#6e0000"),
               ("121-140", 120, "#ffbee8"), ("141-150", 140, "#ff73df")]


def _class_of(edges, v):
    k = 0
    while k < len(edges) and v >= edges[k]:
        k += 1
    return k


def test_wind_scale_is_the_supplied_wind_legend():
    edges, colors, _ = explicit_scale(load_config().wind_scale)
    assert colors[0] == "transparent" and edges[0] == 5                         # the legend starts at 5 mph: calm stays see-through
    assert colors[1:] == [c for _, _, c in WIND_LEGEND]
    for _, lo, col in WIND_LEGEND:
        assert colors[_class_of(edges, lo)] == col and colors[_class_of(edges, lo + 4.9)] == col
    assert colors[_class_of(edges, 4.9)] == "transparent" and colors[_class_of(edges, 71)] == "#ff600a"   # above the top class: top colour stays


def test_gust_scale_is_the_supplied_gust_legend():
    edges, colors, _ = explicit_scale(load_config().gust_scale)
    assert colors == [c for _, _, c in GUST_LEGEND] and len(edges) == 16
    assert colors[_class_of(edges, 0)] == "#1c4577" and colors[_class_of(edges, 4.9)] == "#1c4577"        # 0-5 is drawn (dark navy)
    for _, lo, col in GUST_LEGEND[1:]:
        assert colors[_class_of(edges, lo)] == col
    assert colors[_class_of(edges, 150)] == colors[_class_of(edges, 200)] == "#ff73df"
    assert colors[_class_of(edges, 49.9)] == "#ffc400" and colors[_class_of(edges, 50)] == "#feaa01"     # the unequal classes: 51-60, 61-70, 71-80, 81-100 ...
    assert [e for e in edges if e >= 50] == [50, 60, 70, 80, 100, 120, 140]


def test_wind_and_gust_scales_are_different_scales():
    cfg = load_config()
    assert explicit_scale(cfg.wind_scale)[:2] != explicit_scale(cfg.gust_scale)[:2]


def test_wind_categories_keep_the_nws_thresholds_for_the_read_out():
    _, _, segs = explicit_scale(load_config().wind_scale)
    bands = [(g["phrase"], g["from_hpa"], g["to_hpa"]) for g in segs]
    assert bands == [("light to moderate", None, 25), ("Wind Advisory range", 25, 40), ("gale-force / High Wind", 40, 55),
                     ("storm-force", 55, 75), ("hurricane-force", 75, None)]


def test_explicit_scale_rejects_bad_input():
    ok = NS(edges=[5, 10], colors=["transparent", "#112233", "#AABBCC"])
    assert explicit_scale(ok)[1] == ["transparent", "#112233", "#aabbcc"]
    with pytest.raises(ValueError, match="3 classes"):
        explicit_scale(NS(edges=[5, 10], colors=["#112233", "#445566"]))
    with pytest.raises(ValueError, match="ascending"):
        explicit_scale(NS(edges=[10, 5], colors=["#112233", "#445566", "#778899"]))
    with pytest.raises(ValueError, match="rrggbb"):
        explicit_scale(NS(edges=[5], colors=["red", "#445566"]))
