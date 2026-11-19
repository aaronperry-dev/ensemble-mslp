import math

from gefs_pipeline.tracks import km, link_member, link_tracks


def moving(fh0, n, lon, lat, dlon, dlat, p0=1000.0, dp=-2.0, dt=6):
    return {fh0 + i * dt: (lon + i * dlon, lat + i * dlat, p0 + i * dp) for i in range(n)}


def frames_of(*tracks, fhs=None):
    fhs = fhs or sorted({fh for t in tracks for fh in t})
    return [(fh, [t[fh] for t in tracks if fh in t]) for fh in fhs]


def test_km_basic():
    assert abs(km(0, 0, 1, 0) - 111.19) < 0.1 and km(10, 20, 10, 20) == 0
    assert abs(km(0, 60, 1, 60) - 55.6) < 0.2


def test_a_single_moving_low_becomes_one_track():
    t = moving(0, 8, -100, 35, 2.0, 0.8)                      # ~190 km per 6 h
    out = link_member(frames_of(t))
    assert len(out) == 1 and [pt[0] for pt in out[0]] == list(range(0, 48, 6))
    assert out[0][0][1:] == [-100, 35, 1000.0] and out[0][-1][3] == 1000.0 - 14


def test_two_nearby_lows_are_not_swapped():
    a = moving(0, 8, -100, 35, 2.0, 0.0)
    b = moving(0, 8, -100, 38, 2.0, 0.0)                      # 330 km to the north, moving the same way
    out = link_member(frames_of(a, b))
    assert len(out) == 2
    for tr in out:
        lats = {pt[2] for pt in tr}
        assert len(lats) == 1 and len(tr) == 8


def test_jump_larger_than_max_speed_starts_a_new_track():
    a = moving(0, 5, -100, 35, 2.0, 0.0)
    b = moving(30, 5, -60, 35, 2.0, 0.0)                      # 5 deg = 450 km in one step is fine; but a 4000 km jump is not
    out = link_member(frames_of(a, b), max_speed_kmh=100)
    assert len(out) == 2


def test_short_tracks_are_dropped_and_a_gap_ends_a_track():
    t = {0: (-100, 35, 1000.0), 6: (-98, 35, 999.0), 24: (-90, 35, 990.0), 30: (-88, 35, 989.0), 36: (-86, 35, 988.0)}
    out = link_member(frames_of(t, fhs=[0, 6, 12, 18, 24, 30, 36]), min_points=3)
    assert len(out) == 1 and [pt[0] for pt in out[0]] == [24, 30, 36]       # the 2-point start was too short


def test_prediction_prefers_the_low_ahead_of_the_motion():
    a = moving(0, 4, -100, 35, 3.0, 0.0)                       # heading east at ~270 km / 6 h
    # at 24 h two candidates: one on the path (-88), one behind the track (-94.5) that is nearer to the last point
    frames = frames_of(a) + [(24, [(-88.0, 35.0, 990.0), (-94.5, 35.0, 995.0)])]
    frames = [f for f in frames if f[0] != 24] + [(24, [(-88.0, 35.0, 990.0), (-94.5, 35.0, 995.0)])]
    frames.sort()
    out = link_member(frames, min_points=3)
    longest = max(out, key=len)
    assert longest[-1][1] == -88.0 and len(longest) == 5


def test_link_tracks_splits_by_member_and_rounds():
    lows = {}
    for i in range(6):
        lows[i * 6] = [[-100 + 2 * i + 0.004, 35.0, 1000.04 - i, 0], [-80 - i, 40.0, 990.0, 3]]
    out = link_tracks(lows)
    assert sorted(t["m"] for t in out) == [0, 3]
    t0 = next(t for t in out if t["m"] == 0)
    assert t0["pts"][0] == [0, -100.0, 35.0, 1000.0] and len(t0["pts"]) == 6
