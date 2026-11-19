"""Link each member's low-pressure centres across forecast hours into tracks.

Input is what the pipeline already detects per frame: rows ``[lon, lat, hPa, member]``.  Within one member, a low at
hour *h* continues a track from hour *h - dt* when it lies close to where the track was heading (previous position plus
the last motion) and no farther than ``max_speed_kmh * dt`` from the last position.  Matches are made greedily from the
smallest miss distance; unmatched lows start new tracks, unmatched tracks end.  Short tracks are dropped.
"""
from __future__ import annotations

import math

EARTH_KM = 6371.0


def km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def link_member(frames: list[tuple[int, list[tuple[float, float, float]]]], *, max_speed_kmh: float = 100.0,
                min_points: int = 3) -> list[list[list[float]]]:
    """frames = [(fh, [(lon, lat, p), ...]), ...] sorted by fh  ->  tracks [[fh, lon, lat, p], ...]."""
    done: list[list[list[float]]] = []
    active: list[list[list[float]]] = []           # tracks whose last point is in the previous frame
    prev_fh: int | None = None
    for fh, lows in frames:
        dt = (fh - prev_fh) if prev_fh is not None else 0
        if prev_fh is not None and dt <= 0:
            raise ValueError("frames must be sorted by forecast hour")
        cands = []
        for ti, tr in enumerate(active):
            lf, lo, la, _ = tr[-1]
            if len(tr) >= 2:                        # extrapolate with the last motion (per hour)
                f0, o0, a0, _ = tr[-2]
                span = max(1, lf - f0)
                plon, plat = lo + (lo - o0) / span * dt, la + (la - a0) / span * dt
            else:
                plon, plat = lo, la
            reach = max_speed_kmh * dt
            for li, (x, y, p) in enumerate(lows):
                if km(lo, la, x, y) > reach:
                    continue
                miss = km(plon, plat, x, y)
                if miss > reach:
                    continue
                cands.append((miss, ti, li))
        cands.sort()
        used_t, used_l = set(), set()
        for miss, ti, li in cands:
            if ti in used_t or li in used_l:
                continue
            used_t.add(ti); used_l.add(li)
            x, y, p = lows[li]
            active[ti].append([fh, x, y, p])
        nxt = []
        for ti, tr in enumerate(active):
            if ti in used_t:
                nxt.append(tr)
            else:
                done.append(tr)
        for li, (x, y, p) in enumerate(lows):
            if li not in used_l:
                nxt.append([[fh, x, y, p]])
        active = nxt
        prev_fh = fh
    done.extend(active)
    return [t for t in done if len(t) >= min_points]


def link_tracks(lows_by_fh: dict[int, list[list[float]]], *, max_speed_kmh: float = 100.0, min_points: int = 3) -> list[dict]:
    """lows_by_fh = {fh: [[lon, lat, hPa, member], ...]}  ->  [{"m": member, "pts": [[fh, lon, lat, hPa], ...]}, ...]."""
    fhs = sorted(lows_by_fh)
    members = sorted({int(r[3]) for rows in lows_by_fh.values() for r in rows})
    out = []
    for m in members:
        frames = [(fh, [(r[0], r[1], r[2]) for r in lows_by_fh[fh] if int(r[3]) == m]) for fh in fhs]
        for tr in link_member(frames, max_speed_kmh=max_speed_kmh, min_points=min_points):
            out.append({"m": m, "pts": [[int(a), round(b, 2), round(c, 2), round(d, 1)] for a, b, c, d in tr]})
    return out
