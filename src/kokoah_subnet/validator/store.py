"""The validator's store: the schema in `schema.py` on PostgreSQL (production) or SQLite (tests, quick runs).

Reads used by the mechanism (returns, equity, positions, activity, states, bindings) and the record of everything
else (fills, per-miner figures with their parts, chain samples, commitments, weights, burns, errors).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, create_engine, delete, func, insert, select, text
from sqlalchemy.engine import Connection, Engine

from kokoah_subnet.binding import AcceptedBinding, Unlink
from kokoah_subnet.mechanism import MinerState
from kokoah_subnet.mechanism.skill import daily_return

from . import schema as S
from .migrations import apply_migrations, current_version, latest_version


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _growth(returns: Sequence[float]) -> tuple[float | None, float | None]:
    """The compounded return over daily returns, and the largest fall from a peak of that curve (0 to 1)."""
    if not returns:
        return None, None
    level = peak = 1.0
    drop = 0.0
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        drop = max(drop, 1 - level / peak)
    return level - 1, drop


class Store:
    """`Store(url)` for any SQLAlchemy URL, or `Store(Path)` for a SQLite file. The validator creates and migrates
    the schema; readers (the interface, the health check) open it with `create=False`, which needs no rights to
    change it and refuses a record that is not set up and migrated yet."""

    def __init__(self, target: str | Path, create: bool = True) -> None:
        if isinstance(target, Path):
            if not create and not target.exists():
                raise RuntimeError(f"no record at {target}: the validator creates it when it first runs")
            target.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{target}"
        else:
            url = target
        self.url = url
        self._engine: Engine = create_engine(url, future=True)
        self.migrations_applied: list[str] = []
        if create:
            S.metadata.create_all(self._engine)
            with self._engine.begin() as c:
                self.migrations_applied = apply_migrations(c, self.is_postgres)
            return
        try:
            with self._engine.connect() as c:
                version = current_version(c)
        except Exception as exc:
            raise RuntimeError(f"the record is not set up yet (the validator creates it): {exc}") from exc
        if version < latest_version():
            raise RuntimeError(
                f"the record is at schema version {version}, this release reads {latest_version()}: "
                "start the validator of this release first, it migrates the record"
            )

    def close(self) -> None:
        self._engine.dispose()

    @property
    def is_postgres(self) -> bool:
        return self.url.startswith("postgresql")

    # ----- runs, meta, errors -----------------------------------------------------------------------------

    def start_run(self, **fields: Any) -> int:
        with self._engine.begin() as c:
            result = c.execute(insert(S.runs).values(started_at=_now(), **fields))
            return int(result.inserted_primary_key[0])

    def last_run_subnet(self) -> tuple[str, int] | None:
        """The network and netuid of the record's latest run."""
        with self._engine.connect() as c:
            row = c.execute(select(S.runs.c.network, S.runs.c.netuid).order_by(S.runs.c.id.desc()).limit(1)).first()
        return (str(row[0]), int(row[1])) if row else None

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._engine.connect() as c:
            row = c.execute(select(S.meta.c.value).where(S.meta.c.key == key)).first()
        return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.meta).where(S.meta.c.key == key))
            c.execute(insert(S.meta).values(key=key, value=value))

    def put_error(
        self,
        day: str | None,
        hotkey: str | None,
        address: str | None,
        stage: str,
        message: str,
        level: str = "error",
    ) -> None:
        """Record a failure (`error`, which the health check raises) or a routine fact (`note`, recorded only)."""
        with self._engine.begin() as c:
            c.execute(
                insert(S.errors).values(
                    at=_now(),
                    day=day,
                    hotkey=hotkey,
                    address=address,
                    stage=stage,
                    message=_no_nul(message[:2000]),
                    level=level,
                )
            )

    def put_note(self, day: str | None, hotkey: str | None, address: str | None, stage: str, message: str) -> None:
        self.put_error(day, hotkey, address, stage, message, level="note")

    # ----- equity, ledger, returns ------------------------------------------------------------------------

    def put_equity(
        self,
        address: str,
        day: str,
        value: float,
        flows: float,
        raw: Any = None,
        offmarket_pnl: float = 0.0,
        known: bool = True,
    ) -> None:
        """One day's account value and money moved; `known` false records a day without a return."""
        a = address.lower()
        with self._engine.begin() as c:
            c.execute(delete(S.equity).where(S.equity.c.address == a, S.equity.c.day == day))
            c.execute(
                insert(S.equity).values(
                    address=a,
                    day=day,
                    value=float(value),
                    flows=float(flows),
                    offmarket_pnl=float(offmarket_pnl),
                    return_known=bool(known),
                    fetched_at=_now(),
                    raw=raw,
                )
            )

    def put_ledger_updates(self, address: str, rows: Iterable[Mapping[str, Any]]) -> None:
        a = address.lower()
        with self._engine.begin() as c:
            existing = {
                (int(t), str(h or ""), str(k or ""))
                for t, h, k in c.execute(
                    select(S.ledger_updates.c.time_ms, S.ledger_updates.c.hash, S.ledger_updates.c.type).where(
                        S.ledger_updates.c.address == a
                    )
                )
            }
            for r in rows:
                key = (int(r.get("time_ms", 0)), str(r.get("hash") or ""), str(r.get("type") or ""))
                if key in existing:
                    continue
                existing.add(key)
                c.execute(
                    insert(S.ledger_updates).values(
                        address=a,
                        time_ms=key[0],
                        hash=key[1] or None,
                        type=key[2] or None,
                        usdc=r.get("usdc"),
                        raw=r.get("raw"),
                    )
                )

    def put_equity_points(self, address: str, window: str, points: Iterable[tuple[int, float]], day: str) -> int:
        """Keep the exchange's recorded account-value points (new ones only: a recorded point never changes)."""
        a = address.lower()
        pts = {int(t): float(v) for t, v in points}
        if not pts:
            return 0
        with self._engine.begin() as c:
            have = {
                int(t)
                for (t,) in c.execute(
                    select(S.equity_points.c.time_ms).where(
                        S.equity_points.c.address == a,
                        S.equity_points.c.source_window == window,
                        S.equity_points.c.time_ms.in_(list(pts)),
                    )
                )
            }
            new = [t for t in sorted(pts) if t not in have]
            for t in new:
                c.execute(
                    insert(S.equity_points).values(
                        address=a, source_window=window, time_ms=t, value=pts[t], first_seen_day=day
                    )
                )
        return len(new)

    def equity_points(self, address: str, window: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        with self._engine.connect() as c:
            rows = c.execute(
                select(S.equity_points.c.time_ms, S.equity_points.c.value)
                .where(
                    S.equity_points.c.address == address.lower(),
                    S.equity_points.c.source_window == window,
                    S.equity_points.c.time_ms >= start_ms,
                    S.equity_points.c.time_ms < end_ms,
                )
                .order_by(S.equity_points.c.time_ms)
            ).all()
        return [(int(t), float(v)) for t, v in rows]

    def put_equity_daily(
        self,
        address: str,
        day: str,
        avg: float | None,
        points: int,
        origin_ms: int | None,
        window: str,
        note: str,
    ) -> None:
        a = address.lower()
        with self._engine.begin() as c:
            c.execute(delete(S.equity_daily).where(S.equity_daily.c.address == a, S.equity_daily.c.day == day))
            c.execute(
                insert(S.equity_daily).values(
                    address=a,
                    day=day,
                    avg_equity_usd=avg,
                    points=points,
                    origin_ms=origin_ms,
                    source_window=window,
                    note=note or None,
                    computed_at=_now(),
                )
            )

    def last_origin(self, address: str) -> int | None:
        """When the address's recorded history begins, as its latest scored day read it (None if never read)."""
        with self._engine.connect() as c:
            v = c.execute(
                select(S.equity_daily.c.origin_ms)
                .where(S.equity_daily.c.address == address.lower(), S.equity_daily.c.origin_ms.is_not(None))
                .order_by(S.equity_daily.c.day.desc())
                .limit(1)
            ).scalar()
        return int(v) if v is not None else None

    def put_swap(self, day: str, uid: int, old_hotkey: str, new_hotkey: str, coldkey: str | None) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.hotkey_swaps).where(S.hotkey_swaps.c.day == day, S.hotkey_swaps.c.uid == uid))
            c.execute(
                insert(S.hotkey_swaps).values(
                    day=day, uid=uid, old_hotkey=old_hotkey, new_hotkey=new_hotkey, coldkey=coldkey
                )
            )

    def clear_swaps(self, day: str) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.hotkey_swaps).where(S.hotkey_swaps.c.day == day))

    def swaps_through(self, day: str) -> list[tuple[str, str, str]]:
        """(day, old hotkey, new hotkey) of every recorded swap up to `day`, oldest first."""
        with self._engine.connect() as c:
            rows = c.execute(
                select(S.hotkey_swaps.c.day, S.hotkey_swaps.c.old_hotkey, S.hotkey_swaps.c.new_hotkey)
                .where(S.hotkey_swaps.c.day <= day)
                .order_by(S.hotkey_swaps.c.day, S.hotkey_swaps.c.uid)
            ).all()
        return [(str(d), str(o), str(n)) for d, o, n in rows]

    def credited_notional(self, hotkey: str, day: str, days: int) -> float:
        """Credited Kokoah notional of the hotkey over the `days` scored days before `day` (calendar window)."""
        start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._engine.connect() as c:
            total = c.execute(
                select(func.coalesce(func.sum(S.miner_days.c.credited_notional), 0.0)).where(
                    S.miner_days.c.hotkey == hotkey, S.miner_days.c.day >= start, S.miner_days.c.day < day
                )
            ).scalar()
        return float(total or 0.0)

    def dated_returns(
        self, address: str, days: int, through: str | None = None, after: str | None = None
    ) -> dict[str, float]:
        """The last `days` known daily returns by day up to `through` (default: all stored days), oldest first (the
        same series as `returns`), only of days after `after` when given. A return needs two consecutive stored days
        whose values are known; a day without one is skipped and the series reaches further back, over at most the
        last 2 x `days` + 1 stored days, so one missing day never shortens the window."""
        series = self._equity_rows(address, 2 * days + 1, through)
        out: dict[str, float] = {}
        for (d0, e0, _, k0), (d1, e1, f1, k1) in zip(series, series[1:], strict=False):
            if k0 and k1 and e0 > 0 and _next_day(d0) == d1 and (after is None or d1 > after):
                out[d1] = daily_return(e0, e1, f1)
        return dict(list(out.items())[-days:]) if days > 0 else {}

    def _equity_rows(self, address: str, days: int, through: str | None = None) -> list[tuple[str, float, float, bool]]:
        query = select(
            S.equity.c.day,
            S.equity.c.value,
            S.equity.c.flows,
            S.equity.c.offmarket_pnl,
            S.equity.c.return_known,
        ).where(S.equity.c.address == address.lower())
        if through is not None:
            query = query.where(S.equity.c.day <= through)
        with self._engine.connect() as c:
            rows = c.execute(query.order_by(S.equity.c.day.desc()).limit(days)).all()
        return [(str(d), float(v), float(f) + float(o or 0.0), k is not False) for d, v, f, o, k in reversed(rows)]

    def equity_value(self, address: str, day: str) -> float | None:
        """The stored account value of one day (None when the day has no row)."""
        with self._engine.connect() as c:
            v = c.execute(
                select(S.equity.c.value).where(S.equity.c.address == address.lower(), S.equity.c.day == day)
            ).scalar()
        return float(v) if v is not None else None

    @staticmethod
    def day_before(day: str) -> str:
        return _days_before(day, 1)

    @staticmethod
    def day_after(day: str) -> str:
        return _next_day(day)

    def equity_series(self, address: str, days: int) -> list[tuple[str, float, float]]:
        """Up to `days` most recent (day, value, flows + off-market transfers), oldest first."""
        with self._engine.connect() as c:
            rows = c.execute(
                select(S.equity.c.day, S.equity.c.value, S.equity.c.flows, S.equity.c.offmarket_pnl)
                .where(S.equity.c.address == address.lower())
                .order_by(S.equity.c.day.desc())
                .limit(days)
            ).all()
        return [(str(d), float(v), float(f) + float(o or 0.0)) for d, v, f, o in reversed(rows)]

    def returns(self, address: str, days: int, through: str | None = None, after: str | None = None) -> list[float]:
        """The last `days` known daily returns up to `through`, oldest first, with deposits, withdrawals and money
        moved in through off-market fills removed; only of days after `after` when given (the skill record counts
        the days after the address joined)."""
        return list(self.dated_returns(address, days, through, after).values())

    def account_record(self, address: str, with_days: bool = True) -> dict[str, Any] | None:
        """An address's record as this validator scored it (None if never scored): its daily returns (with money moved
        in or out and off-market profit removed; the skill test reads those after the address joined), the growth of 1
        over them (carried over days the exchange did not record), and each scored day's figures. The "90d" figures
        are over the last 90 known returns, the entry test's longest window; `joined_day` is the day after which the
        skill test counts."""
        a = address.lower()
        with self._engine.connect() as c:
            scored = (
                c.execute(
                    select(
                        S.miner_days.c.day,
                        S.miner_days.c.hotkey,
                        S.miner_days.c.notional,
                        S.miner_days.c.fills,
                        S.miner_days.c.pay_alpha,
                        S.miner_days.c.eligible,
                        S.miner_days.c.agent_verified,
                        S.miner_days.c.avg_equity_usd,
                        S.miner_days.c.joined_day,
                    )
                    .where(S.miner_days.c.address == a)
                    .order_by(S.miner_days.c.day)
                )
                .mappings()
                .all()
            )
            if not scored:
                return None
            stored = [str(d) for (d,) in c.execute(select(S.equity.c.day).where(S.equity.c.address == a)).all()]
            averages: dict[str, float | None] = {
                str(d): v
                for d, v in c.execute(
                    select(S.equity_daily.c.day, S.equity_daily.c.avg_equity_usd).where(S.equity_daily.c.address == a)
                ).all()
            }
            linked = c.execute(select(S.bindings.c.hotkey).where(S.bindings.c.address == a)).first() is not None
        returns = self.dated_returns(a, len(stored))
        # what the chain paid, only for the whole record (the leaderboard's rows read the record's figures alone)
        earned_days = self.earned_by_address_day(a, {str(m["hotkey"]) for m in scored}) if with_days else {}
        window = list(self.dated_returns(a, 90).values())
        every = list(returns.values())
        by_day = {str(m["day"]): m for m in scored}
        days = sorted(set(stored) | set(by_day))
        ret_90, drop_90 = _growth(window)
        ret_all, drop_all = _growth(every)
        record = {
            "record_days": len(every),
            "return_90d": ret_90,
            "worst_drop_90d": drop_90,
            "losing_days_90d": sum(1 for r in window if r < 0) if window else None,
            "return_days_90d": len(window) if window else None,
            "return_all": ret_all,
            "worst_drop_all": drop_all,
            "agent_days": sum(1 for m in scored if m["agent_verified"]),
            "first_day": days[0],
            "last_day": days[-1],
            # the skill record counts the days after this one (the latest scored day's link)
            "joined_day": scored[-1]["joined_day"],
        }
        out: dict[str, Any] = {"address": a, "hotkey": str(scored[-1]["hotkey"]), "linked": linked, "record": record}
        if with_days:
            # what the chain paid the address's hotkeys while it was scored with them, and the scored days' plan
            record["earned_alpha"] = sum(earned_days.values())
            record["planned_alpha"] = float(sum(float(m["pay_alpha"] or 0.0) for m in scored))
            index, rows = 1.0, []
            for d in days:
                r = returns.get(d)
                if r is not None:
                    index *= 1 + r
                m = by_day.get(d)
                avg = m["avg_equity_usd"] if m is not None else averages.get(d)
                rows.append(
                    {
                        "day": d,
                        "scored": m is not None,
                        "hotkey": str(m["hotkey"]) if m is not None else None,
                        "return": r,
                        "index": index,
                        "avg_equity_usd": float(avg) if avg is not None else None,
                        "volume_usd": float(m["notional"] or 0.0) if m is not None else 0.0,
                        "fills": int(m["fills"] or 0) if m is not None else 0,
                        # the scored day's planned allocation (also as `pay_alpha`), and what the chain paid that day
                        "pay_alpha": float(m["pay_alpha"] or 0.0) if m is not None else 0.0,
                        "planned_alpha": float(m["pay_alpha"] or 0.0) if m is not None else 0.0,
                        "earned_alpha": earned_days.get(d, 0.0),
                        "eligible": bool(m["eligible"]) if m is not None else False,
                        "agent_verified": bool(m["agent_verified"]) if m is not None else False,
                    }
                )
            out["days"] = rows
        return out

    def mean_equity(self, address: str, days: int, through: str) -> float:
        """The mean account value over the `days` calendar days ending with `through`, from the days whose end value
        is the exchange's recorded one (0 without any)."""
        e = S.equity
        with self._engine.connect() as c:
            mean = c.execute(
                select(func.avg(e.c.value)).where(
                    e.c.address == address.lower(),
                    e.c.day >= _days_before(through, days - 1),
                    e.c.day <= through,
                    e.c.return_known.is_not(False),
                )
            ).scalar()
        return float(mean) if mean is not None else 0.0

    # ----- positions (mirror rule and record) -------------------------------------------------------------

    def put_positions(self, address: str, day: str, by_coin: Mapping[str, Any]) -> None:
        """`by_coin[coin]`: a signed notional, or a dict with notional, szi, entry_px, unrealized_pnl, leverage, raw."""
        a = address.lower()
        with self._engine.begin() as c:
            c.execute(delete(S.positions).where(S.positions.c.address == a, S.positions.c.day == day))
            for coin, v in by_coin.items():
                d = v if isinstance(v, Mapping) else {"notional": v}
                if float(d.get("notional", 0.0)) == 0:
                    continue
                c.execute(
                    insert(S.positions).values(
                        address=a,
                        day=day,
                        coin=str(coin),
                        notional=float(d["notional"]),
                        szi=d.get("szi"),
                        entry_px=d.get("entry_px"),
                        unrealized_pnl=d.get("unrealized_pnl"),
                        leverage=d.get("leverage"),
                        raw=d.get("raw"),
                    )
                )

    def position_series(self, addresses: Iterable[str], days: Sequence[str]) -> dict[str, dict[str, list[float]]]:
        """`positions[address][coin]` aligned over `days` (0 where flat), for the position mirror rule."""
        out: dict[str, dict[str, list[float]]] = {}
        index = {d: i for i, d in enumerate(days)}
        with self._engine.connect() as c:
            for address in addresses:
                a = address.lower()
                coins: dict[str, list[float]] = {}
                rows = c.execute(
                    select(S.positions.c.day, S.positions.c.coin, S.positions.c.notional).where(
                        S.positions.c.address == a, S.positions.c.day >= days[0], S.positions.c.day <= days[-1]
                    )
                ).all()
                for day, coin, notional in rows:
                    coins.setdefault(str(coin), [0.0] * len(days))[index[str(day)]] = float(notional)
                out[a] = coins
        return out

    # ----- activity, fee rates ----------------------------------------------------------------------------

    def put_activity(self, address: str, day: str, round_trips: int, fills: int, notional: float) -> None:
        a = address.lower()
        with self._engine.begin() as c:
            c.execute(delete(S.activity).where(S.activity.c.address == a, S.activity.c.day == day))
            c.execute(
                insert(S.activity).values(
                    address=a, day=day, round_trips=int(round_trips), fills=int(fills), notional=float(notional)
                )
            )

    def activity_window(self, address: str, days: int, through: str) -> tuple[int, int, float]:
        """(round trips, active days, notional) over the `days` calendar days ending with `through`."""
        with self._engine.connect() as c:
            rows = c.execute(
                select(S.activity.c.round_trips, S.activity.c.fills, S.activity.c.notional).where(
                    S.activity.c.address == address.lower(),
                    S.activity.c.day >= _days_before(through, days - 1),
                    S.activity.c.day <= through,
                )
            ).all()
        return (
            sum(int(r) for r, _, _ in rows),
            sum(1 for _, f, _ in rows if int(f) > 0),
            sum(float(n) for _, _, n in rows),
        )

    def put_fee_rates(self, address: str, day: str, taker_bps: float, maker_bps: float, raw: Any = None) -> None:
        a = address.lower()
        with self._engine.begin() as c:
            c.execute(delete(S.fee_rates).where(S.fee_rates.c.address == a, S.fee_rates.c.day == day))
            c.execute(insert(S.fee_rates).values(address=a, day=day, taker_bps=taker_bps, maker_bps=maker_bps, raw=raw))

    # ----- mechanism state --------------------------------------------------------------------------------

    def states_before(self, day: str) -> dict[str, MinerState]:
        """The states a scoring run of `day` must start from: the history as of the latest scored day before `day`
        (so a re-score never smooths twice); the current states when no history exists yet."""
        with self._engine.connect() as c:
            prev = c.execute(select(func.max(S.state_history.c.day)).where(S.state_history.c.day < day)).scalar()
            any_history = c.execute(select(func.count()).select_from(S.state_history)).scalar()
            if prev is None:
                return {} if any_history else self.states()
            rows = c.execute(select(S.state_history).where(S.state_history.c.day == prev)).mappings().all()
        out: dict[str, MinerState] = {}
        for r in rows:
            if r.get("json"):
                out[str(r["hotkey"])] = _state_from_json(str(r["json"]))
                continue
            paid_days = tuple((int(d), float(a)) for d, a in (r["paid"] or []) if isinstance(d, int | float))
            out[str(r["hotkey"])] = MinerState(
                phi_hat=float(r["phi_hat"] or 0.0),
                fee_hat=float(r["fee_hat"] or 0.0),
                g=float(r["g"] or 0.0),
                paid=(),
                zero_until_day=r["zero_until_day"],
                paid_days=paid_days,
            )
        return out

    def states(self) -> dict[str, MinerState]:
        with self._engine.connect() as c:
            rows = c.execute(select(S.states.c.hotkey, S.states.c.json)).all()
        return {str(hk): _state_from_json(str(js)) for hk, js in rows}

    def put_states(self, states: Mapping[str, MinerState], day: str | None = None) -> None:
        with self._engine.begin() as c:
            for hk, s in states.items():
                c.execute(delete(S.states).where(S.states.c.hotkey == hk))
                c.execute(insert(S.states).values(hotkey=hk, json=json.dumps(asdict(s))))
                if day is not None:
                    c.execute(
                        delete(S.state_history).where(S.state_history.c.day == day, S.state_history.c.hotkey == hk)
                    )
                    c.execute(
                        insert(S.state_history).values(
                            day=day,
                            hotkey=hk,
                            phi_hat=s.phi_hat,
                            fee_hat=s.fee_hat,
                            g=s.g,
                            paid=[[d, a] for d, a in s.paid_days],
                            zero_until_day=s.zero_until_day,
                            json=json.dumps(asdict(s)),
                        )
                    )

    # ----- bindings, commitments, clusters ----------------------------------------------------------------

    def bindings(self) -> dict[str, AcceptedBinding]:
        with self._engine.connect() as c:
            rows = c.execute(
                select(S.bindings.c.hotkey, S.bindings.c.address, S.bindings.c.accepted_block, S.bindings.c.first_block)
            ).all()
        return {str(hk): AcceptedBinding(str(hk), str(a), int(ab), int(fb)) for hk, a, ab, fb in rows}

    def put_bindings(self, accepted: Mapping[str, AcceptedBinding], day: str | None = None) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.bindings))
            for b in accepted.values():
                c.execute(
                    insert(S.bindings).values(
                        hotkey=b.hotkey,
                        address=b.address,
                        accepted_block=b.accepted_block,
                        first_block=b.first_block,
                        updated_day=day,
                    )
                )

    def put_unlinks(self, day: str, found: Iterable[Unlink]) -> None:
        """Keep every unlink seen (the first one per hotkey and address counts; they are never removed)."""
        with self._engine.begin() as c:
            known = {(hk, a) for hk, a in c.execute(select(S.unlinks.c.hotkey, S.unlinks.c.address)).all()}
            for u in found:
                if (u.hotkey, u.address) in known:
                    continue
                known.add((u.hotkey, u.address))
                c.execute(
                    insert(S.unlinks).values(
                        hotkey=u.hotkey,
                        address=u.address,
                        time_ms=u.time_ms,
                        signature=u.signature,
                        effective_day=u.effective_day,
                        first_seen_day=day,
                    )
                )

    def unlinks(self) -> list[Unlink]:
        with self._engine.connect() as c:
            rows = c.execute(
                select(S.unlinks.c.hotkey, S.unlinks.c.address, S.unlinks.c.time_ms, S.unlinks.c.signature)
            ).all()
        return [Unlink(str(hk), str(a), int(t), str(sig)) for hk, a, t, sig in rows]

    def put_commitments(self, day: str, rows: Iterable[Mapping[str, Any]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.commitments).where(S.commitments.c.day == day))
            for r in rows:
                row = dict(r)
                if isinstance(row.get("data"), str):
                    row["data"] = _commitment_text(row["data"])
                c.execute(insert(S.commitments).values(day=day, **row))

    def put_binding_events(self, day: str, rows: Iterable[Mapping[str, Any]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.binding_events).where(S.binding_events.c.day == day))
            for r in rows:
                c.execute(insert(S.binding_events).values(day=day, **dict(r)))

    def put_clusters(self, day: str, cluster_of: Mapping[str, str], edges: Iterable[tuple[str, str, str]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.clusters).where(S.clusters.c.day == day))
            c.execute(delete(S.cluster_edges).where(S.cluster_edges.c.day == day))
            for a, cid in cluster_of.items():
                c.execute(insert(S.clusters).values(day=day, address=a, cluster_id=cid))
            for a, b, source in edges:
                c.execute(insert(S.cluster_edges).values(day=day, a=a.lower(), b=b.lower(), source=source))

    # ----- penalties, hotkey swaps -----------------------------------------------------------------------------

    def put_penalty(self, address: str, until_day: int, day: str, hotkey: str, reason: str) -> None:
        """One mirror report made on `day`. Every report is kept by its day (a longer one the same day replaces a
        shorter one), so a renewal never hides the report that was in force before it."""
        with self._engine.begin() as c:
            _put_penalty(c, address, until_day, day, hotkey, reason)

    def put_penalties(self, day: str, reports: Iterable[tuple[str, int, str, str]]) -> None:
        """The mirror reports made on `day` as (address, until day index, hotkey, reason), replacing the ones an
        earlier scoring of the day made."""
        with self._engine.begin() as c:
            c.execute(delete(S.penalties).where(S.penalties.c.day == day))
            for address, until_day, hotkey, reason in reports:
                _put_penalty(c, address, until_day, day, hotkey, reason)

    def penalties(self, day_index: int, before_day: str | None = None) -> dict[str, int]:
        """Addresses still penalized at `day_index` -> the day index the longest of their reports ends; with
        `before_day`, only the reports made on an earlier day (a re-score of a day applies the reports made before it,
        never its own or later ones)."""
        until = func.max(S.penalties.c.until_day)
        query = select(S.penalties.c.address, until).group_by(S.penalties.c.address).having(until > day_index)
        if before_day is not None:
            query = query.where(S.penalties.c.day < before_day)
        with self._engine.connect() as c:
            rows = c.execute(query).all()
        return {str(a): int(u) for a, u in rows}

    def latest_neurons(self, before_day: str | None = None) -> list[dict[str, Any]]:
        """The most recent metagraph snapshot recorded by a scoring run, or the latest one of a day before
        `before_day` (uid, hotkey, coldkey, registration block)."""
        n = S.neurons
        with self._engine.connect() as c:
            latest = select(func.max(n.c.day))
            if before_day is not None:
                latest = latest.where(n.c.day < before_day)
            day = c.execute(latest).scalar()
            if day is None:
                return []
            rows = c.execute(
                select(n.c.uid, n.c.hotkey, n.c.coldkey, n.c.block_at_registration).where(n.c.day == day)
            ).all()
        return [
            {"uid": int(u), "hotkey": str(h), "coldkey": str(k), "block_at_registration": int(b) if b else None}
            for u, h, k, b in rows
        ]

    # ----- chain samples ----------------------------------------------------------------------------------

    def put_price_samples(self, day: str, samples: Iterable[tuple[int, float]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.price_samples).where(S.price_samples.c.day == day))
            for block, price in samples:
                c.execute(insert(S.price_samples).values(day=day, block=int(block), price_tao=float(price)))

    def put_tao_mark(self, day: str, source: str, mark: float) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.tao_marks).where(S.tao_marks.c.day == day, S.tao_marks.c.source == source))
            c.execute(insert(S.tao_marks).values(day=day, source=source, mark=float(mark)))

    def put_stake_samples(self, day: str, hotkey: str, samples: Iterable[tuple[int, float]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.stake_samples).where(S.stake_samples.c.day == day, S.stake_samples.c.hotkey == hotkey))
            for block, alpha in samples:
                c.execute(insert(S.stake_samples).values(day=day, hotkey=hotkey, block=int(block), alpha=float(alpha)))

    def put_neurons(self, day: str, block: int, rows: Iterable[Mapping[str, Any]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.neurons).where(S.neurons.c.day == day))
            for r in rows:
                c.execute(insert(S.neurons).values(day=day, block=block, **dict(r)))

    # ----- fills and per-miner figures ----------------------------------------------------------------

    def put_fills(self, day: str, rows: Iterable[Mapping[str, Any]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.fills).where(S.fills.c.day == day))
            batch = [dict(day=day, **dict(r)) for r in rows]
            for i in range(0, len(batch), 500):
                c.execute(insert(S.fills), batch[i : i + 500])

    def put_miner_days(self, day: str, rows: Iterable[Mapping[str, Any]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.miner_days).where(S.miner_days.c.day == day))
            for r in rows:
                c.execute(insert(S.miner_days).values(day=day, **dict(r)))

    def miner_days(self, hotkey: str, limit: int = 90) -> list[dict[str, Any]]:
        with self._engine.connect() as c:
            rows = (
                c.execute(
                    select(S.miner_days)
                    .where(S.miner_days.c.hotkey == hotkey)
                    .order_by(S.miner_days.c.day.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in reversed(rows)]

    # ----- days and files ---------------------------------------------------------------------------------

    def put_day(self, day: str, day_index: int, result_json: str, **fields: Any) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.days).where(S.days.c.day == day))
            c.execute(
                insert(S.days).values(
                    day=day, day_index=int(day_index), scored_at=_now(), result_json=result_json, **fields
                )
            )

    def day(self, day: str) -> str | None:
        with self._engine.connect() as c:
            row = c.execute(select(S.days.c.result_json).where(S.days.c.day == day)).first()
        return str(row[0]) if row else None

    def latest_day(self) -> tuple[str, int, str] | None:
        with self._engine.connect() as c:
            row = c.execute(
                select(S.days.c.day, S.days.c.day_index, S.days.c.result_json).order_by(S.days.c.day.desc()).limit(1)
            ).first()
        return (str(row[0]), int(row[1]), str(row[2])) if row else None

    def days_list(self) -> list[str]:
        with self._engine.connect() as c:
            return [str(r[0]) for r in c.execute(select(S.days.c.day).order_by(S.days.c.day)).all()]

    def put_agent_checks(self, day: str, rows: Iterable[Mapping[str, Any]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.agent_checks).where(S.agent_checks.c.day == day))
            for r in rows:
                row = {k: _no_nul(v) if isinstance(v, str) else v for k, v in r.items()}
                c.execute(insert(S.agent_checks).values(day=day, checked_at=_now(), **row))

    def put_builder_rewards(self, builder: str, rewards: Mapping[str, float]) -> None:
        with self._engine.begin() as c:
            c.execute(
                insert(S.builder_rewards).values(
                    at=_now(),
                    builder=builder.lower(),
                    builder_rewards=rewards.get("builderRewards"),
                    claimed_rewards=rewards.get("claimedRewards"),
                    unclaimed_rewards=rewards.get("unclaimedRewards"),
                )
            )

    def latest_builder_rewards(self) -> float | None:
        """The builder's total earnings as the exchange last reported them (the larger of its total and claimed plus
        unclaimed, whichever way the exchange counts)."""
        with self._engine.connect() as c:
            query = select(S.builder_rewards).order_by(S.builder_rewards.c.id.desc()).limit(1)
            row = c.execute(query).mappings().first()
        if row is None:
            return None
        parts = (row["claimed_rewards"] or 0.0) + (row["unclaimed_rewards"] or 0.0)
        return max(float(row["builder_rewards"] or 0.0), float(parts))

    def credited_builder_fees(self) -> float:
        """Builder fees of every counted fill of every bound address over all scored days, USD."""
        with self._engine.connect() as c:
            total = c.execute(
                select(func.coalesce(func.sum(S.fills.c.builder_fee), 0.0)).where(
                    S.fills.c.hotkey.is_not(None), S.fills.c.excluded.is_(None)
                )
            ).scalar()
        return float(total or 0.0)

    def put_file(self, day: str, sha256: str, fills: int, **fields: Any) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.builder_files).where(S.builder_files.c.day == day))
            c.execute(
                insert(S.builder_files).values(day=day, sha256=sha256, fills=int(fills), downloaded_at=_now(), **fields)
            )

    def put_file_recheck(self, day: str, sha256: str) -> None:
        with self._engine.begin() as c:
            c.execute(
                S.builder_files.update()
                .where(S.builder_files.c.day == day)
                .values(sha256_recheck=sha256, rechecked_at=_now())
            )

    def file_recheck(self, day: str) -> str | None:
        with self._engine.connect() as c:
            row = c.execute(select(S.builder_files.c.sha256_recheck).where(S.builder_files.c.day == day)).first()
        return str(row[0]) if row and row[0] else None

    def file(self, day: str) -> tuple[str, int] | None:
        with self._engine.connect() as c:
            row = c.execute(
                select(S.builder_files.c.sha256, S.builder_files.c.fills).where(S.builder_files.c.day == day)
            ).first()
        return (str(row[0]), int(row[1])) if row else None

    # ----- weights and burns ------------------------------------------------------------------------------

    def put_weights_submission(
        self, day: str | None, block: int, uids: Sequence[int], values: Sequence[int], ok: bool, message: str
    ) -> None:
        with self._engine.begin() as c:
            c.execute(
                insert(S.weights_submitted).values(
                    submitted_at=_now(),
                    day=day,
                    block=int(block),
                    uids=list(uids),
                    values=list(values),
                    ok=ok,
                    message=message[:2000],
                )
            )

    def put_burn(self, **fields: Any) -> None:
        with self._engine.begin() as c:
            c.execute(insert(S.burns).values(submitted_at=_now(), **fields))

    # ----- epochs (what the chain paid each tempo) and the miners dimension ----------------------------------

    def put_epoch(
        self,
        block: int,
        step: int,
        tempo: int,
        price_tao: float | None,
        rows: Iterable[Mapping[str, Any]],
        at: datetime | None = None,
    ) -> bool:
        """Record one epoch's metagraph read at `block` (`step`: the epoch's own block, else the tempo window; `at`:
        when the epoch ran, default now), and add what it paid each hotkey to its UTC day's earned (`earned_days`);
        False when that block was recorded already."""
        rows = list(rows)
        when = at or _now()
        with self._engine.begin() as c:
            if c.execute(select(S.epochs.c.block).where(S.epochs.c.block == int(block))).first():
                return False
            c.execute(
                insert(S.epochs).values(
                    block=int(block),
                    at=when,
                    step=int(step),
                    tempo=int(tempo),
                    price_tao=price_tao,
                    num_uids=len(rows),
                )
            )
            for r in rows:
                c.execute(insert(S.epoch_neurons).values(block=int(block), **dict(r)))
            _add_earned(c, _utc_day(when), [(str(r.get("hotkey")), float(r.get("emission") or 0.0)) for r in rows])
        return True

    def put_subnet_epoch(self, block: int, **fields: Any) -> None:
        """The subnet-level emission counters at an epoch (TAO in, alpha in and out, pool reserves, MinerBurned)."""
        with self._engine.begin() as c:
            c.execute(delete(S.subnet_epochs).where(S.subnet_epochs.c.block == int(block)))
            c.execute(insert(S.subnet_epochs).values(block=int(block), at=_now(), **fields))

    def last_epoch_block(self) -> int | None:
        with self._engine.connect() as c:
            row = c.execute(select(func.max(S.epochs.c.block))).first()
        return int(row[0]) if row and row[0] is not None else None

    def upsert_miners(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Merge per-hotkey facts: fields given replace stored ones; first-seen, first-paid, registration and
        binding times are kept once set."""
        keep_first = {"first_seen_at", "first_paid_at", "registered_at", "bound_at", "bound_block"}
        with self._engine.begin() as c:
            for r in rows:
                r = dict(r)
                hk = str(r.pop("hotkey"))
                current = c.execute(select(S.miners).where(S.miners.c.hotkey == hk)).mappings().first()
                if current is None:
                    c.execute(insert(S.miners).values(hotkey=hk, **r))
                    continue
                values = {
                    k: v for k, v in r.items() if v is not None and not (k in keep_first and current.get(k) is not None)
                }
                if values:
                    c.execute(S.miners.update().where(S.miners.c.hotkey == hk).values(**values))

    def miners(self) -> list[dict[str, Any]]:
        with self._engine.connect() as c:
            return [dict(r) for r in c.execute(select(S.miners).order_by(S.miners.c.uid)).mappings().all()]

    def emission_totals(self) -> dict[str, tuple[float, datetime | None, datetime | None]]:
        """Per hotkey from the epochs: total emission (alpha) and the first and last epoch time with emission."""
        e, n = S.epochs, S.epoch_neurons
        with self._engine.connect() as c:
            rows = c.execute(
                select(n.c.hotkey, func.sum(n.c.emission), func.min(e.c.at), func.max(e.c.at))
                .select_from(n.join(e, e.c.block == n.c.block))
                .where(n.c.emission > 0)
                .group_by(n.c.hotkey)
            ).all()
        return {str(hk): (float(total or 0.0), first, last) for hk, total, first, last in rows}

    def earned_by_day(self, hotkeys: Iterable[str] | None = None) -> dict[str, dict[str, float]]:
        """Per hotkey, the alpha the chain actually paid it each UTC day: the `emission` of every epoch this validator
        recorded (once per epoch of the chain), summed by the UTC day the epoch ran (`earned_days`). What a miner
        received, as opposed to a scored day's planned allocation (`miner_days.pay_alpha`): that plan is paid over
        the following tempos, and only in proportion to the stake behind the validators that set it. Epochs this
        validator did not record (while it was down) are missing. Only `hotkeys` when given."""
        query = select(S.earned_days.c.hotkey, S.earned_days.c.day, S.earned_days.c.alpha)
        if hotkeys is not None:
            wanted = sorted(set(hotkeys))
            if not wanted:
                return {}
            query = query.where(S.earned_days.c.hotkey.in_(wanted))
        out: dict[str, dict[str, float]] = {}
        with self._engine.connect() as c:
            for hk, day, alpha in c.execute(query).all():
                out.setdefault(str(hk), {})[str(day)] = float(alpha or 0.0)
        return out

    def earned_by_address_day(self, address: str, hotkeys: Iterable[str]) -> dict[str, float]:
        """The alpha the chain paid `address`'s miners each UTC day (`earned_by_day` of its hotkeys): a day's payment
        counts for the address its hotkey was scored with on the latest scored day before it, whose weights paid it
        (a hotkey that moved to another address earns for that one from then on)."""
        a = address.lower()
        hks = sorted(set(hotkeys))
        if not hks:
            return {}
        md = S.miner_days
        with self._engine.connect() as c:
            rows = c.execute(
                select(md.c.hotkey, md.c.day, md.c.address).where(md.c.hotkey.in_(hks)).order_by(md.c.day)
            ).all()
        scored: dict[str, list[tuple[str, str]]] = {}
        for hk, day, addr in rows:
            scored.setdefault(str(hk), []).append((str(day), str(addr or "").lower()))
        out: dict[str, float] = {}
        for hk, paid in self.earned_by_day(hks).items():
            history = scored.get(hk, [])  # by day
            i, owner = 0, None
            for day in sorted(paid):
                while i < len(history) and history[i][0] < day:
                    owner = history[i][1]
                    i += 1
                if owner == a:
                    out[day] = out.get(day, 0.0) + paid[day]
        return out

    def miner_hotkeys(self) -> list[str]:
        """Every hotkey ever scored as a miner."""
        with self._engine.connect() as c:
            return [str(hk) for (hk,) in c.execute(select(S.miner_days.c.hotkey).distinct()).all()]

    # ----- totals for the ranking -------------------------------------------------------------------------

    def miner_totals(self) -> dict[str, dict[str, float]]:
        """Per hotkey over every scored day: days, active days, alpha paid, builder fees, volume, closed PnL."""
        md = S.miner_days
        with self._engine.connect() as c:
            rows = c.execute(
                select(
                    md.c.hotkey,
                    func.count().label("days"),
                    func.sum(case((md.c.fills > 0, 1), else_=0)).label("active"),
                    func.sum(md.c.pay_alpha),
                    func.sum(md.c.builder_fee),
                    func.sum(md.c.notional),
                    func.sum(md.c.closed_pnl),
                ).group_by(md.c.hotkey)
            ).all()
        return {
            str(hk): {
                "days": float(days or 0),
                "active_days": float(active or 0),
                "pay_alpha": float(pay or 0.0),
                "builder_fee_usd": float(fee or 0.0),
                "volume_usd": float(vol or 0.0),
                "profit_usd": float(pnl or 0.0),
            }
            for hk, days, active, pay, fee, vol, pnl in rows
        }

    def subnet_totals(self) -> dict[str, float]:
        """Over every scored day: alpha paid and burned, their USD value at each day's rate, builder fees, volume."""
        d = S.days
        with self._engine.connect() as c:
            row = c.execute(
                select(
                    func.count(),
                    func.sum(d.c.paid_alpha),
                    func.sum(d.c.burn_alpha),
                    func.sum(d.c.paid_alpha * d.c.price_usd),
                    func.sum(d.c.builder_fee_usd),
                    func.sum(d.c.volume_usd),
                )
            ).first()
            prices = {str(day): float(p) for day, p in c.execute(select(d.c.day, d.c.price_usd)).all() if p is not None}
        n, paid, burn, paid_usd, fees, vol = row if row else (0, 0, 0, 0, 0, 0)
        # what the chain actually paid the miners, each day valued at that day's price (a day not scored yet, such as
        # today, at the latest one)
        earned, earned_usd = 0.0, 0.0
        latest_price = prices[max(prices)] if prices else 0.0
        for days in self.earned_by_day(self.miner_hotkeys()).values():
            for day, alpha in days.items():
                earned += alpha
                earned_usd += alpha * prices.get(day, latest_price)
        return {
            "days": float(n or 0),
            # the scored days' planned allocations (what the validators set the weights for), kept under their old
            # names; `earned_*` is what the chain actually paid
            "paid_alpha": float(paid or 0.0),
            "planned_alpha": float(paid or 0.0),
            "burn_alpha": float(burn or 0.0),
            "paid_usd": float(paid_usd or 0.0),
            "planned_usd": float(paid_usd or 0.0),
            "earned_alpha": earned,
            "earned_usd": earned_usd,
            "builder_fee_usd": float(fees or 0.0),
            "volume_usd": float(vol or 0.0),
        }

    def execute_sql(self, sql: str) -> list[dict[str, Any]]:
        """Read-only helper for ad-hoc analysis (SELECT only)."""
        if not sql.strip().lower().startswith("select"):
            raise ValueError("SELECT only")
        with self._engine.connect() as c:
            return [dict(r) for r in c.execute(text(sql)).mappings().all()]


def _commitment_text(data: str) -> str:
    """A commitment's data as stored. Any hotkey can commit any bytes, and Postgres text can't hold a NUL character,
    so data with one is kept as 0x-hex of its UTF-8 bytes: the form the chain client already gives data that isn't
    UTF-8."""
    return "0x" + data.encode().hex() if "\x00" in data else data


def _no_nul(text: str) -> str:
    """Text for a message or an agent check, which can quote what a miner's machine or another outside source sent:
    Postgres text can't hold a NUL character, so each one becomes U+FFFD (the replacement character)."""
    return text.replace("\x00", "\ufffd")


def _put_penalty(c: Connection, address: str, until_day: int, day: str, hotkey: str, reason: str) -> None:
    a = address.lower()
    key = (S.penalties.c.address == a, S.penalties.c.day == day)
    row = c.execute(select(S.penalties.c.until_day).where(*key)).first()
    if row is not None and int(row[0]) >= until_day:
        return
    c.execute(delete(S.penalties).where(*key))
    c.execute(insert(S.penalties).values(address=a, until_day=int(until_day), day=day, hotkey=hotkey, reason=reason))


def _state_from_json(s: str) -> MinerState:
    raw = json.loads(s)
    raw["paid"] = tuple(raw.get("paid", ()))
    raw["paid_days"] = tuple((int(d), float(a)) for d, a in raw.get("paid_days", ()))
    known = set(MinerState.__dataclass_fields__)
    return MinerState(**{k: v for k, v in raw.items() if k in known})


def _utc_day(when: datetime) -> str:
    """The UTC day (YYYY-MM-DD) of a time (a naive one is UTC, as SQLite gives it back)."""
    return (when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)).strftime("%Y-%m-%d")


def _add_earned(c: Connection, day: str, paid: Iterable[tuple[str, float]]) -> None:
    """Add each hotkey's alpha paid in one epoch to its `earned_days` row for `day`."""
    e = S.earned_days
    for hotkey, alpha in paid:
        if alpha <= 0:
            continue
        row = c.execute(select(e.c.alpha, e.c.epochs).where(e.c.day == day, e.c.hotkey == hotkey)).first()
        if row is None:
            c.execute(insert(e).values(day=day, hotkey=hotkey, alpha=alpha, epochs=1))
        else:
            c.execute(
                e.update()
                .where(e.c.day == day, e.c.hotkey == hotkey)
                .values(alpha=float(row[0]) + alpha, epochs=int(row[1]) + 1)
            )


def _next_day(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _days_before(day: str, days: int) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


__all__ = ["Store"]
