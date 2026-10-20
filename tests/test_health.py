"""The health check over the record."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kokoah_subnet.validator.health import check
from kokoah_subnet.validator.store import Store


def test_check_reports_missing_and_stale_things(tmp_path: Path) -> None:
    s = Store(tmp_path / "v.sqlite")
    now = datetime.now(tz=UTC)  # the store stamps real time, so the check runs against real time
    r = check(s, now=now)
    assert not r["ok"] and "no day scored yet" in r["problems"] and "no weights submitted yet" in r["problems"]
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    s.put_day(yesterday, 2, "{}", paid_alpha=1.0, burn_alpha=1.0)
    s.put_weights_submission(yesterday, 100, [1], [65535], True, "Success")
    s.put_epoch(100, 1, 360, 1.0, [])
    r = check(s, now=now)
    assert r["ok"], r["problems"]
    s.put_weights_submission(yesterday, 200, [1], [65535], False, "rate limited")
    r = check(s, now=now)
    assert any("failed" in p for p in r["problems"])
    s.put_error(yesterday, "hk", "0xa", "collect", "boom")
    late = check(s, now=now + timedelta(hours=5))
    assert any("errors recorded" in p for p in late["problems"]) and any("h ago" in p for p in late["problems"])
    s.put_burn(
        network="test",
        netuid=1,
        hotkey="hk",
        staked_alpha=1.0,
        locked_alpha=0.0,
        burn_alpha=1.0,
        ok=False,
        message="no",
        dry_run=False,
    )
    assert any("burn failed" in p for p in check(s, now=now)["problems"])
