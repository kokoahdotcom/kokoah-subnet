"""The health check over the record."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from kokoah_subnet.validator.health import check, main_report
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
    s.put_note(yesterday, "hk", "0xa", "builder", "3 history fills not confirmed as Kokoah orders")
    r = check(s, now=now)
    assert r["ok"] and r["notes_24h"] == 1 and r["errors_24h"] == 0  # a routine note is no problem
    s.put_weights_submission(yesterday, 200, [1], [65535], False, "rate limited")
    r = check(s, now=now)
    assert any("failed" in p for p in r["problems"])
    s.put_error(yesterday, "hk", "0xa", "collect", "boom")
    s.put_weights_submission(yesterday, 300, [1], [65535], True, "Success")
    late = check(s, now=now + timedelta(hours=5))
    assert any("errors recorded" in p for p in late["problems"])
    assert any("weight submission was" in p for p in late["problems"])
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


def test_a_watching_validator_is_not_asked_for_weights(tmp_path: Path) -> None:
    s = Store(tmp_path / "v.sqlite")
    now = datetime.now(tz=UTC)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    s.put_day(yesterday, 2, "{}", paid_alpha=1.0, burn_alpha=1.0)
    s.put_epoch(100, 1, 360, 1.0, [])
    s.start_run(network="finney", netuid=39, config={"watch": "True"})
    r = check(s, now=now)
    assert r["ok"] and r["watching"] is True, r["problems"]
    s.start_run(network="finney", netuid=39, config={"watch": "False"})  # voting now: weights are expected again
    r = check(s, now=now)
    assert r["watching"] is False and "no weights submitted yet" in r["problems"]


def test_a_refused_alert_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused(url: str, **_: Any) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "post", refused)
    assert main_report(Store(tmp_path / "h.sqlite"), "https://hooks.example/x", only_problems=True) == 1
    assert "did not accept the report" in capsys.readouterr().err
