"""Decode ONE GRIB2 message held in memory (no temp files) and cut it to the map domain."""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

# ecCodes is not guaranteed thread-safe in every build; decoding is fast, so serialise it.
_ECC_LOCK = threading.Lock()


MSLP_SHORT_NAMES = {"prmsl", "msl"}     # NCEP GEFS: PRMSL, ECMWF: msl

# 10 m wind fields are reported in whatever unit the centre uses; the viewer works in miles per hour
MPH_PER = {"m s**-1": 2.2369363, "m s-1": 2.2369363, "m/s": 2.2369363, "knot": 1.1507794, "kn": 1.1507794, "kt": 1.1507794,
           "km h**-1": 0.6213712, "km h-1": 0.6213712, "km/h": 0.6213712, "mph": 1.0}


class DecodeError(Exception):
    pass


@dataclass
class Grid:
    lats: np.ndarray      # ascending, degrees north
    lons: np.ndarray      # ascending, degrees east in [-180, 180)
    values: np.ndarray    # (nlat, nlon) float32, hPa
    valid_time: str       # ISO-8601 UTC
    step_hours: int
    short: str = ""       # GRIB shortName of the message

    @property
    def res(self) -> float:
        return float(abs(self.lats[1] - self.lats[0]))


def split_messages(raw: bytes) -> list[bytes]:
    """Split a multi-message GRIB2 file (e.g. the GEPS all-member file) into its messages, using the section-0 lengths."""
    out, pos, n = [], 0, len(raw)
    while pos < n:
        j = raw.find(b"GRIB", pos)
        if j < 0 or j + 16 > n:
            break
        length = int.from_bytes(raw[j + 8:j + 16], "big")
        if length < 16 or j + length > n or raw[j + length - 4:j + length] != b"7777":
            raise DecodeError(f"truncated or corrupt GRIB message at byte {j}")
        out.append(raw[j:j + length])
        pos = j + length
    return out


def member_key(raw: bytes) -> tuple[int, int]:
    """(is_perturbed, perturbationNumber) of one ensemble message; control forecasts sort first.

    GRIB2 code table 4.6: 0/1 = unperturbed control (high/low resolution), 2/3 = negatively/positively perturbed.
    Messages that carry no ensemble keys sort by their position (the caller falls back to file order)."""
    import eccodes as ec
    with _ECC_LOCK:
        gid = ec.codes_new_from_message(raw)
        try:
            def get(k, default):
                try:
                    return int(ec.codes_get(gid, k))
                except Exception:       # key absent for this product definition
                    return default
            typ = get("typeOfEnsembleForecast", -1)
            num = get("perturbationNumber", 0)
        finally:
            ec.codes_release(gid)
    return (0 if typ in (0, 1) else 1, num)


def decode_message(raw: bytes, west: float, east: float, south: float, north: float, *, field: str = "mslp") -> Grid:
    """Decode one message and cut it to the domain.  field="mslp" (default): the shortName must be a pressure and the values
    are returned in hPa; field="speed": any wind speed / gust message, values returned in mph (units read from the file)."""
    import eccodes as ec  # imported lazily so unit tests that do not need GRIB work without ecCodes

    if raw[:4] != b"GRIB" or raw[-4:] != b"7777":
        raise DecodeError("not a complete GRIB message")
    with _ECC_LOCK:
        gid = ec.codes_new_from_message(raw)
        try:
            ni, nj = ec.codes_get(gid, "Ni"), ec.codes_get(gid, "Nj")
            la1, la2 = ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees"), ec.codes_get(gid, "latitudeOfLastGridPointInDegrees")
            lo1 = ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
            di = abs(ec.codes_get(gid, "iDirectionIncrementInDegrees"))
            if ec.codes_get(gid, "iScansNegatively"):
                di = -di
            units = ec.codes_get(gid, "units")
            short = ec.codes_get(gid, "shortName")
            ec.codes_set(gid, "missingValue", 9.999e20)
            vals = ec.codes_get_values(gid).astype(np.float32)
            valid = ec.codes_get(gid, "validityDate"), ec.codes_get(gid, "validityTime")
            step = int(ec.codes_get(gid, "endStep"))
        finally:
            ec.codes_release(gid)
    if field == "mslp" and short not in MSLP_SHORT_NAMES:
        raise DecodeError(f"unexpected shortName {short!r}")
    if vals.size != ni * nj:
        raise DecodeError("value count does not match grid")

    vals = vals.reshape(nj, ni)
    vals[vals > 1e20] = np.nan
    if field == "speed":
        try:
            vals *= np.float32(MPH_PER[str(units).strip()])
        except KeyError:
            raise DecodeError(f"unexpected wind unit {units!r}") from None
    elif units == "Pa":
        vals /= 100.0
    lats = np.linspace(la1, la2, nj)
    # Longitudes come from the increment, NOT from first/last point: ECMWF's global grid starts at 180 E and wraps
    # through 0 (180 -> 359.75 -> 0 -> 179.75), which linspace(first, last) would get wrong.
    lons = lo1 + di * np.arange(ni)

    lons = ((lons + 180.0) % 360.0) - 180.0          # 0..360 -> -180..180
    order = np.argsort(lons)
    lons, vals = lons[order], vals[:, order]
    if lats[0] > lats[-1]:                            # north->south scan -> ascending
        lats, vals = lats[::-1], vals[::-1]

    jm = (lats >= south - 1e-6) & (lats <= north + 1e-6)
    im = (lons >= west - 1e-6) & (lons <= east + 1e-6)
    if jm.sum() < 3 or im.sum() < 3:
        raise DecodeError("domain does not intersect the grid")
    d = str(valid[0]).zfill(8)
    t = str(valid[1]).zfill(4)
    iso = f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:]}:00Z"
    return Grid(lats[jm].copy(), lons[im].copy(), np.ascontiguousarray(vals[np.ix_(jm, im)]), iso, step, short)


def speed_from_uv(u: Grid, v: Grid) -> Grid:
    """Wind speed (mph) from the two 10 m components (already in mph), on their common grid."""
    if u.values.shape != v.values.shape or not (np.allclose(u.lats, v.lats) and np.allclose(u.lons, v.lons)):
        raise DecodeError("u and v components are on different grids")
    return Grid(u.lats, u.lons, np.hypot(u.values, v.values).astype(np.float32), u.valid_time, u.step_hours, "ws")
