"""Health check over the record: is the validator scoring, submitting and burning as it should?

Used by `kokoah-subnet validator check` (exit code 1 on a problem) and by the hourly timer, which can post the
report to a webhook (`KOKOAH_SUBNET_ALERT_WEBHOOK`, a Discord- or Slack-style JSON webhook).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import desc, func, select

from kokoah_subnet.mechanism.params import BEST_REBATE_BPS

from . import schema as S
from .store import Store


def check(
    store: Store,
    now: datetime | None = None,
    max_day_age_h: float = 30.0,
    max_weights_age_h: float = 3.0,
    expected_by_hour: int = 16,  # the exchange publishes the daily file with a delay of several hours
) -> dict[str, Any]:
    """A report with `ok` and a list of `problems` (strings)."""
    now = now or datetime.now(tz=UTC)
    problems: list[str] = []
    with store._engine.connect() as c:
        latest = c.execute(select(S.days.c.day, S.days.c.scored_at).order_by(desc(S.days.c.day)).limit(1)).first()
        last_weights = c.execute(
            select(S.weights_submitted.c.submitted_at, S.weights_submitted.c.ok, S.weights_submitted.c.message)
            .order_by(desc(S.weights_submitted.c.id))
            .limit(1)
        ).first()
        last_epoch = c.execute(select(func.max(S.epochs.c.at))).scalar()
        errors_24h = c.execute(
            select(func.count()).select_from(S.errors).where(S.errors.c.at >= now - timedelta(hours=24))
        ).scalar()
        last_burn = c.execute(
            select(S.burns.c.submitted_at, S.burns.c.ok, S.burns.c.burn_alpha, S.burns.c.message, S.burns.c.dry_run)
            .order_by(desc(S.burns.c.id))
            .limit(1)
        ).first()
    expected_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if latest is None:
        problems.append("no day scored yet")
    else:
        scored_at = latest[1] if latest[1] is None or latest[1].tzinfo else latest[1].replace(tzinfo=UTC)
        if latest[0] < expected_day and now.hour >= expected_by_hour:
            problems.append(
                f"latest scored day is {latest[0]}, expected {expected_day} by {expected_by_hour:02d}:00 UTC"
            )
        if scored_at is not None and now - scored_at > timedelta(hours=max_day_age_h):
            problems.append(f"last scoring run was {(now - scored_at).total_seconds() / 3600:.0f} h ago")
    if last_weights is None:
        problems.append("no weights submitted yet")
    else:
        at = last_weights[0] if last_weights[0].tzinfo else last_weights[0].replace(tzinfo=UTC)
        if not last_weights[1]:
            problems.append(f"last weight submission failed: {last_weights[2]}")
        elif now - at > timedelta(hours=max_weights_age_h):
            problems.append(f"last weight submission was {(now - at).total_seconds() / 3600:.1f} h ago")
    if last_epoch is not None:
        at = last_epoch if last_epoch.tzinfo else last_epoch.replace(tzinfo=UTC)
        if now - at > timedelta(hours=3):
            problems.append(f"no epoch recorded for {(now - at).total_seconds() / 3600:.1f} h (is the loop running?)")
    if errors_24h:
        problems.append(f"{errors_24h} errors recorded in the last 24 h")
    if last_burn is not None and not last_burn[4] and not last_burn[1]:
        problems.append(f"last burn failed: {last_burn[3]}")
    with store._engine.connect() as c:
        deepest = c.execute(select(func.min(S.fee_rates.c.maker_bps))).scalar()
    if deepest is not None and float(deepest) < -BEST_REBATE_BPS - 1e-9:
        problems.append(
            f"an address shows a maker rebate of {float(deepest):.2f} bps, deeper than the {BEST_REBATE_BPS} bps the "
            "one law assumes: re-check the caps"
        )
    # the fees credited to traders must all have reached the builder: never more than the exchange says it earned
    credited, earned = store.credited_builder_fees(), store.latest_builder_rewards()
    if earned is not None and credited > earned + 0.01:
        problems.append(
            f"traders were credited ${credited:,.2f} of builder fees but the exchange shows the builder earned "
            f"${earned:,.2f}: some credited fees did not reach the builder"
        )
    return {
        "ok": not problems,
        "credited_builder_fees_usd": credited,
        "builder_earned_usd": earned,
        "checked_at": now.isoformat(),
        "latest_day": latest[0] if latest else None,
        "last_weights_ok": bool(last_weights[1]) if last_weights else None,
        "last_epoch_at": last_epoch.isoformat() if last_epoch else None,
        "errors_24h": int(errors_24h or 0),
        "last_burn": {"at": last_burn[0].isoformat(), "ok": last_burn[1], "alpha": last_burn[2]} if last_burn else None,
        "problems": problems,
    }


def post_webhook(url: str, report: dict[str, Any], name: str = "kokoah-subnet validator") -> bool:
    """Send the report to a JSON webhook (Discord accepts `content`, Slack accepts `text`)."""
    status = "OK" if report["ok"] else "PROBLEM"
    lines = [f"{name}: {status}", f"latest day {report['latest_day']}, errors 24h {report['errors_24h']}"]
    lines += [f"- {p}" for p in report["problems"]]
    text = "\n".join(lines)
    try:
        r = httpx.post(url, json={"content": text, "text": text}, timeout=15.0)
        return r.status_code < 300
    except httpx.HTTPError:
        return False


def main_report(store: Store, webhook: str | None, only_problems: bool) -> int:
    report = check(store)
    print(json.dumps(report, indent=1, default=str))
    if webhook and (report["problems"] or not only_problems):
        post_webhook(webhook, report)
    return 0 if report["ok"] else 1
