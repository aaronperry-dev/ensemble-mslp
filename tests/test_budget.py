"""Time budget: a system that is much too slow is skipped (hour or rest of build) and the data that was available is published."""
import json
import time
from datetime import datetime, timezone

import numpy as np

from gefs_pipeline.config import load_config
from gefs_pipeline.decode import Grid
from gefs_pipeline.fetcher import Http
from gefs_pipeline.pipeline import build
from gefs_pipeline.sources import Family, MemberResult

RUN = datetime(2026, 11, 19, 0, tzinfo=timezone.utc)
LATS = np.arange(0.0, 90.0, 2.5)
LONS = np.arange(-180.0, 180.0, 2.5)


class Fake(Family):
    """behave(member, fh) -> 'ok' | 'hang' (blocks until abandoned) | seconds (a float: slow but arrives)."""
    has_wind = False

    def __init__(self, cfg, key, n=10, behave=lambda m, fh: "ok"):
        super().__init__(cfg)
        self.key, self.label, self.name, self.org, self.note = key, key.upper(), key.upper(), "T", ""
        self.members = [f"{key}{i:02d}" for i in range(n)]
        self.behave = behave

    def max_fh(self, run):
        return 240

    def plan(self, http, run, fh):
        return "p"

    def plan_tag(self, plan):
        return "fake"

    def fetch_grid(self, http, run, plan, member, fh, cache_dir):
        how = self.behave(member, fh)
        if how == "hang":
            while True:
                http._check_abort()               # what a real request does before every attempt
                time.sleep(0.02)
        if how != "ok":
            time.sleep(how)
        i = self.members.index(member)
        vals = np.full((len(LATS), len(LONS)), 1015.0 - i * 0.2, dtype="f4")
        return Grid(LATS, LONS, vals, f"2026-11-19T{fh % 24:02d}:00:00Z", fh)

    def fetch_member(self, http, run, plan, member, fh, cache_dir, want_wind=False):
        return MemberResult(self.fetch_grid(http, run, plan, member, fh, cache_dir))


def run_build(tmp_path, fams, fhs, **budget):
    cfg = load_config()
    for k, v in {"frame_timeout_minutes": 0.03, "strikes": 2, "family_minutes": 0, "build_minutes": 0, **budget}.items():
        setattr(cfg.budget, k, v)
    cfg.wind.enabled = False
    out = tmp_path / "site"
    build(cfg, run=RUN, out=out, cache_dir=None, http=Http(user_agent="t", rps=0), workers=4,
          families=[f(cfg) if callable(f) else f for f in fams], fhs=fhs)
    return json.loads((out / "data" / "manifest.json").read_text())


def test_everything_fast_changes_nothing(tmp_path):
    cfg = load_config()
    m = run_build(tmp_path, [Fake(cfg, "aa"), Fake(cfg, "bb")], [0, 6, 12])
    assert [f["fh"] for f in m["frames"]] == [0, 6, 12]
    assert set(m["families"]) == set(m["views"]) == {"aa", "bb"}
    assert "incomplete" not in m


def test_a_stalled_system_is_dropped_after_two_slow_hours_and_the_rest_is_published(tmp_path):
    cfg = load_config()
    slow = Fake(cfg, "slow", behave=lambda m, fh: "hang")
    t = time.time()
    m = run_build(tmp_path, [Fake(cfg, "fast"), slow], [0, 6, 12, 18, 24])
    took = time.time() - t
    assert [f["fh"] for f in m["frames"]] == [0, 6, 12, 18, 24]            # the fast system has every hour
    assert all(list(f["avail"]) == ["fast"] for f in m["frames"])
    assert list(m["families"]) == ["fast"] and m["views"] == ["fast"]       # nothing of the stalled system is offered
    assert m["incomplete"]["dropped"]["slow"]["after_fh"] is None
    assert took < 12                                                        # 2 x 1.8 s timeouts, never 5 x


def test_a_system_with_a_few_stuck_members_still_delivers_its_hour_and_recovers(tmp_path):
    cfg = load_config()
    f = Fake(cfg, "part", behave=lambda m, fh: "hang" if (m.endswith("09") and fh == 0) else "ok")
    m = run_build(tmp_path, [f], [0, 6, 12])
    by = {fr["fh"]: fr["avail"]["part"]["members"] for fr in m["frames"]}
    assert by == {0: 9, 6: 10, 12: 10}                                      # 9 of 10 members used at the slow hour, then back to normal
    assert "incomplete" not in m                                            # one strike is not a drop


def test_family_minutes_drops_a_system_that_is_still_downloading_late(tmp_path):
    cfg = load_config()
    f = Fake(cfg, "late", behave=lambda m, fh: 0.4 if m.endswith("00") else "ok")      # each hour takes >= 0.4 s
    m = run_build(tmp_path, [Fake(cfg, "fast"), f], [0, 6, 12, 18, 24, 30], frame_timeout_minutes=5, family_minutes=0.02)
    late_hours = [fr["fh"] for fr in m["frames"] if "late" in fr["avail"]]
    assert 0 < len(late_hours) < 6 and list(m["incomplete"]["dropped"]) == ["late"]         # "fast" is never penalised for waiting
    assert m["incomplete"]["dropped"]["late"]["after_fh"] == late_hours[-1]
    assert [fr["fh"] for fr in m["frames"]] == [0, 6, 12, 18, 24, 30] and all("fast" in fr["avail"] for fr in m["frames"])


def test_build_minutes_publishes_the_hours_done_so_far(tmp_path):
    cfg = load_config()
    f = Fake(cfg, "one", behave=lambda m, fh: 0.5 if m.endswith("00") else "ok")
    m = run_build(tmp_path, [f], list(range(0, 120, 6)), frame_timeout_minutes=5, build_minutes=0.04)
    got = [fr["fh"] for fr in m["frames"]]
    assert 0 < len(got) < 20 and got == list(range(0, 6 * len(got), 6))
    assert m["incomplete"]["truncated"]["after_fh"] == got[-1]
