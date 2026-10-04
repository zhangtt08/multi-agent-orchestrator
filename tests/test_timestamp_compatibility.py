"""Legacy UTC lease timestamps must never crash status or recovery checks."""
from datetime import datetime, timezone

from mao.scheduler.clock import lease_is_stale, parse_ts


def test_legacy_and_malformed_lease_timestamps():
    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    assert parse_ts("2026-10-04T12:00:00") == now
    assert parse_ts("2026-10-04T20:00:00+08:00") == now
    assert lease_is_stale("2026-10-04T11:59:00", now)
    assert not lease_is_stale("2026-10-04T12:01:00", now)
    for invalid in (None, "", "bad", 123):
        assert parse_ts(invalid) is None
        assert lease_is_stale(invalid, now)
