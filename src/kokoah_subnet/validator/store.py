"""The validator's local store (SQLite, one file): daily equity and flows per address, daily positions, per-miner
mechanism state, accepted bindings, activity counts for the gates, and every day's published result."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from kokoah_subnet.binding import AcceptedBinding
from kokoah_subnet.mechanism import MinerState

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity (address TEXT, day TEXT, value REAL, flows REAL, PRIMARY KEY (address, day));
CREATE TABLE IF NOT EXISTS positions (address TEXT, day TEXT, coin TEXT, notional REAL,
    PRIMARY KEY (address, day, coin));
CREATE TABLE IF NOT EXISTS activity (address TEXT, day TEXT, round_trips INTEGER, fills INTEGER, notional REAL,
    PRIMARY KEY (address, day));
CREATE TABLE IF NOT EXISTS states (hotkey TEXT PRIMARY KEY, json TEXT);
CREATE TABLE IF NOT EXISTS bindings (hotkey TEXT PRIMARY KEY, address TEXT, accepted_block INTEGER,
    first_block INTEGER);
CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, day_index INTEGER, json TEXT);
CREATE TABLE IF NOT EXISTS files (day TEXT PRIMARY KEY, sha256 TEXT, fills INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)  # the HTTP view reads from worker threads
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # equity and returns -------------------------------------------------------------------------------------

    def put_equity(self, address: str, day: str, value: float, flows: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO equity VALUES (?, ?, ?, ?)", (address.lower(), day, float(value), float(flows))
            )

    def equity_series(self, address: str, days: int) -> list[tuple[str, float, float]]:
        """Up to `days` most recent (day, value, flows), oldest first."""
        rows = self._db.execute(
            "SELECT day, value, flows FROM equity WHERE address = ? ORDER BY day DESC LIMIT ?", (address.lower(), days)
        ).fetchall()
        return [(str(d), float(v), float(f)) for d, v, f in reversed(rows)]

    def returns(self, address: str, days: int) -> list[float]:
        """Daily returns (flows removed) over consecutive stored days, oldest first."""
        series = self.equity_series(address, days + 1)
        out: list[float] = []
        for (d0, v0, _), (d1, v1, f1) in zip(series, series[1:], strict=False):
            if _next_day(d0) != d1 or v0 <= 0:
                continue
            out.append((v1 - v0 - f1) / v0)
        return out

    def mean_equity(self, address: str, days: int) -> float:
        series = self.equity_series(address, days)
        return sum(v for _, v, _ in series) / len(series) if series else 0.0

    # positions (mirror rule) --------------------------------------------------------------------------------

    def put_positions(self, address: str, day: str, notional_by_coin: Mapping[str, float]) -> None:
        with self._db:
            self._db.execute("DELETE FROM positions WHERE address = ? AND day = ?", (address.lower(), day))
            self._db.executemany(
                "INSERT INTO positions VALUES (?, ?, ?, ?)",
                [(address.lower(), day, coin, float(n)) for coin, n in notional_by_coin.items() if n != 0],
            )

    def position_series(self, addresses: Iterable[str], days: Sequence[str]) -> dict[str, dict[str, list[float]]]:
        """`positions[address][coin]` aligned over `days` (0 where flat), for the position mirror rule."""
        out: dict[str, dict[str, list[float]]] = {}
        index = {d: i for i, d in enumerate(days)}
        for address in addresses:
            a = address.lower()
            coins: dict[str, list[float]] = {}
            rows = self._db.execute(
                "SELECT day, coin, notional FROM positions WHERE address = ? AND day >= ? AND day <= ?",
                (a, days[0], days[-1]),
            ).fetchall()
            for day, coin, notional in rows:
                coins.setdefault(str(coin), [0.0] * len(days))[index[str(day)]] = float(notional)
            out[a] = coins
        return out

    # activity (gates) ---------------------------------------------------------------------------------------

    def put_activity(self, address: str, day: str, round_trips: int, fills: int, notional: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO activity VALUES (?, ?, ?, ?, ?)",
                (address.lower(), day, int(round_trips), int(fills), float(notional)),
            )

    def activity_window(self, address: str, days: int) -> tuple[int, int, float]:
        """(round trips, active days, notional) over the most recent `days` stored days."""
        rows = self._db.execute(
            "SELECT round_trips, fills, notional FROM activity WHERE address = ? ORDER BY day DESC LIMIT ?",
            (address.lower(), days),
        ).fetchall()
        return (
            sum(int(r) for r, _, _ in rows),
            sum(1 for _, f, _ in rows if int(f) > 0),
            sum(float(n) for _, _, n in rows),
        )

    # mechanism state ----------------------------------------------------------------------------------------

    def states(self) -> dict[str, MinerState]:
        rows = self._db.execute("SELECT hotkey, json FROM states").fetchall()
        return {str(hk): _state_from_json(str(js)) for hk, js in rows}

    def put_states(self, states: Mapping[str, MinerState]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO states VALUES (?, ?)",
                [(hk, json.dumps(asdict(s))) for hk, s in states.items()],
            )

    # bindings -----------------------------------------------------------------------------------------------

    def bindings(self) -> dict[str, AcceptedBinding]:
        rows = self._db.execute("SELECT hotkey, address, accepted_block, first_block FROM bindings").fetchall()
        return {str(hk): AcceptedBinding(str(hk), str(a), int(ab), int(fb)) for hk, a, ab, fb in rows}

    def put_bindings(self, accepted: Mapping[str, AcceptedBinding]) -> None:
        with self._db:
            self._db.execute("DELETE FROM bindings")
            self._db.executemany(
                "INSERT INTO bindings VALUES (?, ?, ?, ?)",
                [(b.hotkey, b.address, b.accepted_block, b.first_block) for b in accepted.values()],
            )

    # days and files -----------------------------------------------------------------------------------------

    def put_day(self, day: str, day_index: int, result_json: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?)", (day, int(day_index), result_json))

    def day(self, day: str) -> str | None:
        row = self._db.execute("SELECT json FROM days WHERE day = ?", (day,)).fetchone()
        return str(row[0]) if row else None

    def latest_day(self) -> tuple[str, int, str] | None:
        row = self._db.execute("SELECT day, day_index, json FROM days ORDER BY day DESC LIMIT 1").fetchone()
        return (str(row[0]), int(row[1]), str(row[2])) if row else None

    def days_list(self) -> list[str]:
        return [str(r[0]) for r in self._db.execute("SELECT day FROM days ORDER BY day").fetchall()]

    def put_file(self, day: str, sha256: str, fills: int) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (day, sha256, int(fills)))

    def file(self, day: str) -> tuple[str, int] | None:
        row = self._db.execute("SELECT sha256, fills FROM files WHERE day = ?", (day,)).fetchone()
        return (str(row[0]), int(row[1])) if row else None

    # meta -----------------------------------------------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def _state_from_json(text: str) -> MinerState:
    raw = json.loads(text)
    raw["paid"] = tuple(raw.get("paid", ()))
    return MinerState(**raw)


def _next_day(day: str) -> str:
    from datetime import UTC, datetime, timedelta

    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return d.strftime("%Y-%m-%d")
