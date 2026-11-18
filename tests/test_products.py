import numpy as np
import pytest

from gefs_pipeline.products import contour_bands, detect_lows, ensemble_mean, ensemble_min, upsample

LATS = np.arange(15, 65.01, 0.25)
LONS = np.arange(-140, -49.99, 0.25)
LON, LAT = np.meshgrid(LONS, LAT := LATS)


def field(*lows, base=1018.0):
    f = np.full(LON.shape, base)
    for lon, lat, depth in lows:
        f -= depth * np.exp(-(((LON - lon) / 4) ** 2 + ((LAT - lat) / 3) ** 2))
    return f.astype("f4")


def test_mean_and_min():
    a, b = np.full((3, 3), 1000.0), np.full((3, 3), 1010.0)
    b[1, 1] = np.nan
    s = np.stack([a, b])
    assert ensemble_mean(s)[0, 0] == 1005 and ensemble_mean(s)[1, 1] == 1000   # NaN ignored
    assert ensemble_min(s)[0, 0] == 1000


def test_detect_lows_finds_centres_and_respects_threshold():
    f = field((-90, 40, 30), (-70, 45, 12))        # 988 and 1006 hPa
    kw = dict(radius_deg=2, smooth_deg=0.5, min_sep_deg=1.5, domain=(-140, -50, 15, 65))
    lows = detect_lows(LATS, LONS, f, threshold_hpa=1005, **kw)
    assert len(lows) == 1                                           # the 1006 hPa low is above the threshold
    lon, lat, p = lows[0]
    assert (lon, lat) == (-90.0, 40.0) and 987 < p < 989
    assert len(detect_lows(LATS, LONS, f, threshold_hpa=1010, **kw)) == 2


def test_detect_lows_ignores_border_and_outside_domain():
    f = field((-139.9, 40, 30))                     # centred on the grid edge -> not provably a minimum
    assert detect_lows(LATS, LONS, f, threshold_hpa=1005, radius_deg=2, smooth_deg=0.5, min_sep_deg=1.5,
                       domain=(-140, -50, 15, 65)) == []
    f = field((-90, 40, 30))
    assert detect_lows(LATS, LONS, f, threshold_hpa=1005, radius_deg=2, smooth_deg=0.5, min_sep_deg=1.5,
                       domain=(-80, -50, 15, 65)) == []   # outside requested domain


def test_contour_bands_partition_the_domain():
    f = field((-90, 40, 36))                                        # 982 hPa centre -> below the 988 first edge
    edges = list(range(988, 1045, 4))
    feats = contour_bands(LATS, LONS, f, edges)
    idx = [x["properties"]["i"] for x in feats]
    assert idx == sorted(idx) and 0 in idx                          # open-ended lowest class exists
    assert feats[-1]["properties"]["hi"] is None or feats[-1]["properties"]["hi"] > 1000
    # polygons carry [lon, lat] pairs inside the domain
    pts = np.array([c for ft in feats for poly in ft["geometry"]["coordinates"] for ring in poly for c in ring])
    assert pts[:, 0].min() >= -140 and pts[:, 0].max() <= -50 and pts[:, 1].min() >= 15 and pts[:, 1].max() <= 65
    # every ring is closed
    for ft in feats:
        for poly in ft["geometry"]["coordinates"]:
            for ring in poly:
                assert ring[0] == ring[-1]


def test_contour_area_adds_up():
    """Sum of band polygon areas (shoelace, outer minus holes) equals the grid area."""
    f = field((-90, 40, 30), (-110, 30, -8))
    feats = contour_bands(LATS, LONS, f, list(range(988, 1045, 4)), precision=4)

    def area(ring):
        r = np.array(ring)
        return 0.5 * (np.dot(r[:-1, 0], r[1:, 1]) - np.dot(r[1:, 0], r[:-1, 1]))
    total = sum(abs(area(poly[0])) - sum(abs(area(h)) for h in poly[1:])
                for ft in feats for poly in ft["geometry"]["coordinates"])
    assert total == pytest.approx((LONS[-1] - LONS[0]) * (LATS[-1] - LATS[0]), rel=2e-3)


def test_upsample_doubles_half_degree_grid_only():
    la, lo = LATS[::2], LONS[::2]
    f = field((-90, 40, 30))[::2, ::2]
    la2, lo2, f2 = upsample(la, lo, f, 0.25)
    assert f2.shape == (2 * len(la) - 1, 2 * len(lo) - 1) and la2[0] == la[0] and lo2[-1] == lo[-1]
    la3, lo3, f3 = upsample(LATS, LONS, field(), 0.25)              # already 0.25 -> untouched
    assert f3.shape == (len(LATS), len(LONS))
