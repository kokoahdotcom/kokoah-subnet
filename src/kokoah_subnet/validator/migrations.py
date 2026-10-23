"""Schema migrations: `create_all` adds new tables but never changes existing columns, so every change to a column
is a numbered step here, applied once per database (the step number is kept in `meta` under `schema_version`).
Steps must be idempotent on both PostgreSQL and SQLite (SQLite ignores what it cannot do; it is only for tests)."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

Step = Callable[[Connection, bool], None]


def _pg(sql: str) -> Step:
    def run(c: Connection, is_postgres: bool) -> None:
        if is_postgres:
            c.execute(text(sql))

    return run


STEPS: list[tuple[int, str, Step]] = [
    (
        1,
        "fills.side widened to 8 chars (Bid/Ask in the builder file)",
        _pg("ALTER TABLE fills ALTER COLUMN side TYPE varchar(8)"),
    ),
    (2, "fills.counterparty widened to 64 chars", _pg("ALTER TABLE fills ALTER COLUMN counterparty TYPE varchar(64)")),
    (
        3,
        "fills: no uniqueness (identical rows occur), index on day + address",
        _pg(
            "ALTER TABLE fills DROP CONSTRAINT IF EXISTS uq_fill; "
            "CREATE INDEX IF NOT EXISTS ix_fills_day_address ON fills (day, address)"
        ),
    ),
    (4, "equity.offmarket_pnl", _pg("ALTER TABLE equity ADD COLUMN IF NOT EXISTS offmarket_pnl double precision")),
    (
        5,
        "builder_files: recheck columns",
        _pg(
            "ALTER TABLE builder_files ADD COLUMN IF NOT EXISTS sha256_recheck varchar(64); "
            "ALTER TABLE builder_files ADD COLUMN IF NOT EXISTS rechecked_at timestamptz"
        ),
    ),
    (
        6,
        "eligibility: miner_days columns, full state in state_history",
        _pg(
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS eligible boolean; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS ineligible_reason varchar(64); "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS avg_equity_usd double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS equity_points integer; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS qualified boolean; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS qual_trades integer; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS qual_notional double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS qualified_at_ms bigint; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS credited_credit double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS credited_notional double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS credited_fills integer; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS identity_reset varchar(32); "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS registered_block bigint; "
            "ALTER TABLE state_history ADD COLUMN IF NOT EXISTS json text"
        ),
    ),
    (
        7,
        "fills: source, trade id and client order id",
        _pg(
            "ALTER TABLE fills ADD COLUMN IF NOT EXISTS source varchar(8); "
            "ALTER TABLE fills ADD COLUMN IF NOT EXISTS tid bigint; "
            "ALTER TABLE fills ADD COLUMN IF NOT EXISTS cloid varchar(34)"
        ),
    ),
    (
        8,
        "miner_days: the two pools",
        _pg(
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS volume_pay_alpha double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS skill_pay_alpha double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS skill_weight double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS skill_adjusted double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS volume_window_usd double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS volume_factor double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS entry_psr double precision; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS entry_pass boolean; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS agent_verified boolean; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS copy_group text; "
            "ALTER TABLE miner_days ADD COLUMN IF NOT EXISTS skill_pool_reason varchar(64)"
        ),
    ),
]


def current_version(c: Connection) -> int:
    row = c.execute(text("SELECT value FROM meta WHERE key = 'schema_version'")).first()
    return int(row[0]) if row else 0


def apply_migrations(c: Connection, is_postgres: bool) -> list[str]:
    """Apply the steps above the stored version, in order; returns what was applied."""
    done: list[str] = []
    version = current_version(c)
    for number, title, step in STEPS:
        if number <= version:
            continue
        step(c, is_postgres)
        c.execute(text("DELETE FROM meta WHERE key = 'schema_version'"))
        c.execute(text("INSERT INTO meta (key, value) VALUES ('schema_version', :v)"), {"v": str(number)})
        version = number
        done.append(f"{number}: {title}")
    return done
