import numpy as np
import pytest

from gefs_pipeline.products import detect_lows, ensemble_mean, ensemble_min

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
