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
from sqlalchemy.engine import Engine

from kokoah_subnet.binding import AcceptedBinding
from kokoah_subnet.mechanism import MinerState

from . import schema as S


def _now() -> datetime:
    return datetime.now(tz=UTC)


class Store:
    """`Store(url)` for any SQLAlchemy URL, or `Store(Path)` for a SQLite file."""

    def __init__(self, target: str | Path) -> None:
        if isinstance(target, Path):
            target.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{target}"
        else:
            url = target
        self.url = url
        self._engine: Engine = create_engine(url, future=True)
        S.metadata.create_all(self._engine)

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

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._engine.connect() as c:
            row = c.execute(select(S.meta.c.value).where(S.meta.c.key == key)).first()
        return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.meta).where(S.meta.c.key == key))
            c.execute(insert(S.meta).values(key=key, value=value))

    def put_error(self, day: str | None, hotkey: str | None, address: str | None, stage: str, message: str) -> None:
        with self._engine.begin() as c:
            c.execute(
                insert(S.errors).values(
                    at=_now(), day=day, hotkey=hotkey, address=address, stage=stage, message=message[:2000]
                )
            )

    # ----- equity, ledger, returns ------------------------------------------------------------------------

    def put_equity(self, address: str, day: str, value: float, flows: float, raw: Any = None) -> None:
        a = address.lower()
        with self._engine.begin() as c:
            c.execute(delete(S.equity).where(S.equity.c.address == a, S.equity.c.day == day))
            c.execute(
                insert(S.equity).values(
                    address=a, day=day, value=float(value), flows=float(flows), fetched_at=_now(), raw=raw
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

    def equity_series(self, address: str, days: int) -> list[tuple[str, float, float]]:
        """Up to `days` most recent (day, value, flows), oldest first."""
        with self._engine.connect() as c:
            rows = c.execute(
                select(S.equity.c.day, S.equity.c.value, S.equity.c.flows)
                .where(S.equity.c.address == address.lower())
                .order_by(S.equity.c.day.desc())
                .limit(days)
            ).all()
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

    def activity_window(self, address: str, days: int) -> tuple[int, int, float]:
        """(round trips, active days, notional) over the most recent `days` stored days."""
        with self._engine.connect() as c:
            rows = c.execute(
                select(S.activity.c.round_trips, S.activity.c.fills, S.activity.c.notional)
                .where(S.activity.c.address == address.lower())
                .order_by(S.activity.c.day.desc())
                .limit(days)
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
                            paid=list(s.paid),
                            zero_until_day=s.zero_until_day,
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

    def put_commitments(self, day: str, rows: Iterable[Mapping[str, Any]]) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.commitments).where(S.commitments.c.day == day))
            for r in rows:
                c.execute(insert(S.commitments).values(day=day, **dict(r)))

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

    def delete_day(self, day: str) -> None:
        """Forget a scored day so it is scored again (its records are rewritten on the next pass)."""
        with self._engine.begin() as c:
            c.execute(delete(S.days).where(S.days.c.day == day))
            c.execute(delete(S.meta).where(S.meta.c.key.in_([f"attempt:{day}", "weights:day", "weights:block"])))

    def put_file(self, day: str, sha256: str, fills: int, **fields: Any) -> None:
        with self._engine.begin() as c:
            c.execute(delete(S.builder_files).where(S.builder_files.c.day == day))
            c.execute(
                insert(S.builder_files).values(day=day, sha256=sha256, fills=int(fills), downloaded_at=_now(), **fields)
            )

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
        self, block: int, step: int, tempo: int, price_tao: float | None, rows: Iterable[Mapping[str, Any]]
    ) -> bool:
        """Record one epoch's metagraph; False when that block was recorded already."""
        rows = list(rows)
        with self._engine.begin() as c:
            if c.execute(select(S.epochs.c.block).where(S.epochs.c.block == int(block))).first():
                return False
            c.execute(
                insert(S.epochs).values(
                    block=int(block),
                    at=_now(),
                    step=int(step),
                    tempo=int(tempo),
                    price_tao=price_tao,
                    num_uids=len(rows),
                )
            )
            for r in rows:
                c.execute(insert(S.epoch_neurons).values(block=int(block), **dict(r)))
        return True

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
        n, paid, burn, paid_usd, fees, vol = row if row else (0, 0, 0, 0, 0, 0)
        return {
            "days": float(n or 0),
            "paid_alpha": float(paid or 0.0),
            "burn_alpha": float(burn or 0.0),
            "paid_usd": float(paid_usd or 0.0),
            "builder_fee_usd": float(fees or 0.0),
            "volume_usd": float(vol or 0.0),
        }

    def execute_sql(self, sql: str) -> list[dict[str, Any]]:
        """Read-only helper for ad-hoc analysis (SELECT only)."""
        if not sql.strip().lower().startswith("select"):
            raise ValueError("SELECT only")
        with self._engine.connect() as c:
            return [dict(r) for r in c.execute(text(sql)).mappings().all()]


def _state_from_json(s: str) -> MinerState:
    raw = json.loads(s)
    raw["paid"] = tuple(raw.get("paid", ()))
    return MinerState(**raw)


def _next_day(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


__all__ = ["Store"]
