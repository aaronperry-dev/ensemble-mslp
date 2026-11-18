from datetime import datetime, timezone

from gefs_pipeline.config import load_config, member_names
from gefs_pipeline.gefs import forecast_hours, object_url, parse_cycle, products_for_fh

CFG = load_config()
RUN = datetime(2026, 11, 18, 6, tzinfo=timezone.utc)


def test_members():
    m = member_names(CFG)
    assert m[0] == "gec00" and m[1] == "gep01" and m[-1] == "gep30" and len(m) == 31


def test_object_url_matches_bucket_layout():
    p25, p50 = CFG.source.products
    assert object_url(CFG, RUN, p25, "gep01", 132) == (
        "https://noaa-gefs-pds.s3.amazonaws.com/gefs.20261118/06/atmos/pgrb2sp25/gep01.t06z.pgrb2s.0p25.f132")
    assert object_url(CFG, RUN, p50, "gec00", 270).endswith("/atmos/pgrb2ap5/gec00.t06z.pgrb2a.0p50.f270")


def test_highest_resolution_first_with_fallback():
    assert [p.resolution_deg for p in products_for_fh(CFG, 240)] == [0.25, 0.5]
    assert [p.resolution_deg for p in products_for_fh(CFG, 246)] == [0.5]      # 0.25-deg stops at f240


def test_hours_and_cycle_parse():
    h = forecast_hours(CFG)
    assert h[0] == 0 and h[-1] == 240 and h[1] - h[0] == 6      # 10 days, all inside the 0.25-deg range
    assert all(products_for_fh(CFG, x)[0].resolution_deg == 0.25 for x in h)
    assert forecast_hours(CFG, 24) == [0, 6, 12, 18, 24]
    assert parse_cycle("2026111806") == RUN
