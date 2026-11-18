"""Round-trip a synthetic GRIB2 message (0..360 longitudes, north->south) through decode_message."""
import numpy as np
import pytest

eccodes = pytest.importorskip("eccodes")

from gefs_pipeline.decode import DecodeError, decode_message


def make_grib(res=0.5, short="prmsl"):
    nj, ni = int(180 / res) + 1, int(360 / res)
    g = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    for k, v in dict(Ni=ni, Nj=nj, latitudeOfFirstGridPointInDegrees=90.0, latitudeOfLastGridPointInDegrees=-90.0,
                     longitudeOfFirstGridPointInDegrees=0.0, longitudeOfLastGridPointInDegrees=360.0 - res,
                     iDirectionIncrementInDegrees=res, jDirectionIncrementInDegrees=res, dataDate=20261118,
                     dataTime=600, shortName=short, bitsPerValue=16).items():
        eccodes.codes_set(g, k, v)
    lat = np.linspace(90, -90, nj)[:, None]
    lon = np.linspace(0, 360 - res, ni)[None, :]
    vals = 101300.0 + 10 * lat + 5 * (lon - 180)                 # Pa; strictly increasing north & east
    eccodes.codes_set_values(g, vals.ravel())
    raw = eccodes.codes_get_message(g)
    eccodes.codes_release(g)
    return raw


def test_decode_rolls_longitudes_flips_latitudes_and_converts_units():
    grid = decode_message(make_grib(), west=-100, east=-60, south=20, north=50)
    assert grid.res == 0.5
    assert grid.lons[0] == -100 and grid.lons[-1] == -60 and grid.lats[0] == 20 and grid.lats[-1] == 50
    assert np.all(np.diff(grid.lats) > 0) and np.all(np.diff(grid.lons) > 0)
    # value at (lat 20, lon -100 == 260E): 1013 + 0.1*20 + 0.05*(260-180) = 1013 + 2 + 4 = 1019 hPa
    assert grid.values[0, 0] == pytest.approx(1019.0, abs=0.05)
    assert grid.valid_time.startswith("2026-11-18T06:00")


def test_truncated_or_wrong_variable_rejected():
    raw = make_grib()
    with pytest.raises(DecodeError):
        decode_message(raw[:-10], -100, -60, 20, 50)
    with pytest.raises(DecodeError):
        decode_message(make_grib(short="2t"), -100, -60, 20, 50)


def make_ecmwf_grib(res=0.5):
    """ECMWF open-data style grid: starts at 180 E, wraps through 0 (180 -> 359.5 -> 0 -> 179.5), shortName msl."""
    nj, ni = int(180 / res) + 1, int(360 / res)
    g = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    for k, v in dict(Ni=ni, Nj=nj, latitudeOfFirstGridPointInDegrees=90.0, latitudeOfLastGridPointInDegrees=-90.0,
                     longitudeOfFirstGridPointInDegrees=180.0, longitudeOfLastGridPointInDegrees=180.0 - res,
                     iDirectionIncrementInDegrees=res, jDirectionIncrementInDegrees=res, dataDate=20261118,
                     dataTime=0, shortName="msl", bitsPerValue=16).items():
        eccodes.codes_set(g, k, v)
    lon_true = (180.0 + res * np.arange(ni)) % 360.0                 # the real longitude of every column
    lon_pm = ((lon_true + 180.0) % 360.0) - 180.0                    # -180..180
    lat = np.linspace(90, -90, nj)[:, None]
    vals = 101300.0 + 10 * lat + 5 * lon_pm[None, :]                 # Pa; encodes (lat, true longitude)
    eccodes.codes_set_values(g, vals.ravel())
    raw = eccodes.codes_get_message(g)
    eccodes.codes_release(g)
    return raw


def test_ecmwf_grid_starting_at_180_is_placed_correctly():
    grid = decode_message(make_ecmwf_grib(), west=-100, east=-60, south=20, north=50)
    assert grid.lons[0] == -100 and grid.lons[-1] == -60
    assert np.all(np.diff(grid.lons) > 0)
    # value at (lat 20, lon -100): 1013 + 0.1*20 + 0.05*(-100) = 1010 hPa  (would be badly wrong with linspace(first, last))
    assert grid.values[0, 0] == pytest.approx(1010.0, abs=0.05)
    # and at (lat 50, lon -60): 1013 + 5 - 3 = 1015
    assert grid.values[-1, -1] == pytest.approx(1015.0, abs=0.05)
