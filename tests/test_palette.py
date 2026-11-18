import re
from types import SimpleNamespace as NS

import pytest

from gefs_pipeline.config import load_config
from gefs_pipeline.palette import RAMPS, build_scale, edges_from_spec, hex_to_oklab, oklab_to_hex, sample_ramp


def test_ramps_are_valid_hex_and_official_lengths():
    for name, ramp in RAMPS.items():
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in ramp), name
        assert len(ramp) in (9, 11)


def test_ramps_match_colorbrewer_reference():
    pal = pytest.importorskip("palettable.colorbrewer")
    for name, ref in {"YlOrRd": pal.sequential.YlOrRd_9, "YlOrBr": pal.sequential.YlOrBr_9, "YlGnBu": pal.sequential.YlGnBu_9,
                      "PuBu": pal.sequential.PuBu_9, "Blues": pal.sequential.Blues_9, "RdBu": pal.diverging.RdBu_11,
                      "RdYlBu": pal.diverging.RdYlBu_11, "PuOr": pal.diverging.PuOr_11}.items():
        assert [h.lower() for h in ref.hex_colors] == RAMPS[name], name


def test_oklab_roundtrip():
    for h in RAMPS["RdBu"]:
        assert oklab_to_hex(*hex_to_oklab(h)) == h


def test_sample_ramp_endpoints_and_count():
    out = sample_ramp(RAMPS["YlOrRd"], 16)
    assert len(out) == 16 and out[0] == RAMPS["YlOrRd"][0] and out[-1] == RAMPS["YlOrRd"][-1]


def test_diverging_neutral_sits_on_the_centre_class():
    cfg = load_config()
    edges, colors = build_scale(cfg.products.min)
    assert len(colors) == len(edges) + 1
    i = next(k for k in range(len(colors)) if (k == 0 or edges[k - 1] <= cfg.products.min.diverging_center) and
             (k == len(edges) or cfg.products.min.diverging_center < edges[k]))
    assert colors[i] == "#f7f7f7"                       # RdBu neutral on the class holding 1014 hPa
    assert colors[0] == RAMPS["RdBu"][-1]                # reverse=true: lowest pressure = deepest blue
    assert colors[-1] == RAMPS["RdBu"][0]                # highest = deepest red


def test_edges_inclusive():
    assert edges_from_spec(NS(start=988, stop=1004, step=4)) == [988, 992, 996, 1000, 1004]
