"""Adversarial replay through the real scoring code (specification section 15, item 2).

Run: uv run python scripts/replay.py [--days 60] [--pool-usd 12667] [--real DIR]

Builds a subnet of synthetic traders and attackers, generates their fills day by day, runs every day through
`score_day` (the same code path as the validator) and reports, per strategy, what it paid in exchange and builder
fees, what the subnet paid it, and the net. Honest profiles are expected to be paid their proportional share;
every zero-edge strategy is expected to net negative at every pool size.

With `--real DIR`, JSON files of public Hyperliquid fills (one address per file, as returned by `userFillsByTime`)
are replayed as honest traders alongside the synthetic ones, re-labelled as builder-code fills at the 0.025% fee.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kokoah_subnet.mechanism import DEFAULT, Fill, MinerState  # noqa: E402
from kokoah_subnet.mechanism.day import DayInputs, MinerInputs, score_day  # noqa: E402

DAY_MS = 86_400_000
T0 = 1792454400000  # 2026-10-20 00:00 UTC
TAKER, MAKER = 4.5, 1.5  # Hyperliquid base tier, bps a side
BUILDER = DEFAULT.builder_fee_rate


@dataclass
class Actor:
    name: str
    hotkey: str
    address: str
    volume_usd: float  # notional per day on the builder leg
    taker_share: float  # of the builder-leg fills
    hl_rate_scale: float = 1.0  # fee tier: 1.0 base, 0.32 = tier 6 + Diamond, negative maker = rebate
    maker_rate: float = MAKER  # bps a side for maker fills (negative = rebate)
    hidden_bps: float = 0.0  # cost of the other leg per round trip (negative = rebate)
    drift: float = 0.0  # daily return drift (edge)
    vol: float = 0.0  # daily return volatility
    holds: float = 0.0  # share of pay kept as stake
    mirror_of: str | None = None  # address this actor mirrors (opposite side, seconds later)
    coin: str = "BTC"
    equity: float = 50_000.0
    # tallies
    paid_hl: float = 0.0
    paid_builder: float = 0.0
    paid_hidden: float = 0.0
    received_usd: float = 0.0
    staked: float = 0.0
    paid_alpha: list[float] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)


def actors() -> list[Actor]:
    a = [
        Actor("honest swing trader", "h1", "0x" + "11" * 20, 2_000_000, 0.9, drift=0.003, vol=0.02, holds=1.0),
        Actor("honest scalper", "h2", "0x" + "12" * 20, 8_000_000, 1.0, drift=0.001, vol=0.01, holds=0.5),
        Actor("honest market maker", "h3", "0x" + "13" * 20, 20_000_000, 0.1, maker_rate=-0.3, drift=0.001, vol=0.004),
        Actor("newcomer, no skill", "h4", "0x" + "14" * 20, 500_000, 1.0, drift=0.0, vol=0.02),
        Actor("wash: taker vs own maker elsewhere", "c1", "0x" + "21" * 20, 10_000_000, 1.0, hidden_bps=2 * MAKER),
        Actor(
            "hedge: tier-6 Diamond taker, rebated leg",
            "c2",
            "0x" + "22" * 20,
            50_000_000,
            1.0,
            hl_rate_scale=0.32,
            hidden_bps=-0.6,
        ),
        Actor("rebated MM, maker both legs", "c3", "0x" + "23" * 20, 50_000_000, 0.0, maker_rate=-0.3, hidden_bps=-0.6),
        Actor("instant round trips", "c4", "0x" + "24" * 20, 5_000_000, 1.0),
        Actor("mirror pair A", "c5", "0x" + "25" * 20, 5_000_000, 1.0, coin="ETH", mirror_of="0x" + "26" * 20),
        Actor(
            "mirror pair B (crosses A)", "c6", "0x" + "26" * 20, 5_000_000, 0.0, coin="ETH", mirror_of="0x" + "25" * 20
        ),
    ]
    return a


def gen_fills(actor: Actor, day: int, rng: random.Random) -> list[Fill]:
    """A day's builder-leg fills: round trips of equal size spread over the day."""
    n_round_trips = 20
    notional = actor.volume_usd / (2 * n_round_trips)
    px = 100_000.0 if actor.coin == "BTC" else 4_000.0
    fills: list[Fill] = []
    for i in range(n_round_trips):
        for leg in (0, 1):
            t = T0 + day * DAY_MS + i * (DAY_MS // n_round_trips) + leg * 30_000
            taker = rng.random() < actor.taker_share
            side = ("B", "A")[leg] if not actor.mirror_of else ("A", "B")[leg]
            rate = TAKER * actor.hl_rate_scale if taker else actor.maker_rate
            builder_fee = notional * BUILDER
            hl_fee = notional * rate / 1e4
            fills.append(
                Fill(
                    t,
                    actor.address,
                    actor.coin,
                    side,
                    px,
                    notional / px,
                    taker,
                    builder_fee,
                    counterparty=actor.mirror_of or "0xmarket",
                    total_fee=hl_fee + builder_fee,
                )
            )
            actor.paid_hl += hl_fee
            actor.paid_builder += builder_fee
    actor.paid_hidden += actor.volume_usd / 2 * actor.hidden_bps / 1e4
    return fills


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--pool-usd", type=float, default=12_667.0, help="USD value of the daily miner pool")
    ap.add_argument("--real", type=Path, default=None, help="directory of public fills JSON files to replay")
    args = ap.parse_args()
    rng = random.Random(5)
    acts = actors()
    real: dict[str, list[dict]] = {}
    if args.real and args.real.is_dir():
        for f in sorted(args.real.glob("*.json")):
            real[f.stem.lower()] = json.loads(f.read_text())
            acts.append(Actor(f"real {f.stem[:10]}", f"r{len(real)}", f.stem.lower(), 0.0, 1.0, holds=0.5))
    price_usd = args.pool_usd / DEFAULT.miner_pool_alpha
    states: dict[str, MinerState] = {}
    activity: dict[str, list[tuple[int, int]]] = {a.address: [] for a in acts}
    for day in range(args.days):
        fills: list[Fill] = []
        for a in acts:
            if a.address in real:
                day_start = T0 + day * DAY_MS
                recs = [r for r in real[a.address] if day_start <= int(r["time"]) < day_start + DAY_MS]
                own = [
                    Fill(
                        int(r["time"]),
                        a.address,
                        str(r["coin"]),
                        str(r["side"]),
                        float(r["px"]),
                        float(r["sz"]),
                        bool(r.get("crossed")),
                        float(r["px"]) * float(r["sz"]) * BUILDER,
                        counterparty="0xmarket",
                        total_fee=float(r["fee"]) + float(r["px"]) * float(r["sz"]) * BUILDER,
                    )
                    for r in recs
                ]
                a.paid_builder += sum(f.builder_fee for f in own)
                a.paid_hl += sum(float(r["fee"]) for r in recs)
            else:
                own = gen_fills(a, day, rng)
            fills.extend(own)
            activity[a.address].append((len(own) // 2, 1 if own else 0))
            r = rng.gauss(a.drift, a.vol) if a.vol > 0 else 0.0
            a.returns.append(r)
            a.equity *= 1 + r
        miners = []
        for a in acts:
            window = activity[a.address][-30:]
            miners.append(
                MinerInputs(
                    hotkey=a.hotkey,
                    address=a.address,
                    returns=a.returns[-60:],
                    mean_equity_usd=a.equity,
                    staked_min_alpha=a.staked,
                    collateral_min_alpha=10.0,
                    round_trips_window=sum(r for r, _ in window),
                    active_days_window=sum(d for _, d in window),
                )
            )
        inputs = DayInputs(day_index=day, price_usd=price_usd, fills=fills, miners=miners, states=states)
        result = score_day(inputs, DEFAULT)
        states = result.states
        for rec in result.records:
            a = next(x for x in acts if x.hotkey == rec.hotkey)
            a.received_usd += rec.pay_alpha * price_usd
            a.paid_alpha.append(rec.pay_alpha)
            a.staked += rec.pay_alpha * a.holds
    print(f"{args.days} days, pool ${args.pool_usd:,.0f}/day, alpha ${price_usd:.3f}")
    print(f"{'actor':44} {'HL fees':>10} {'builder':>10} {'other leg':>10} {'received':>10} {'net':>10} {'M':>5}")
    worst_cheat = -1e9
    for a in acts:
        net = a.received_usd - a.paid_hl - a.paid_builder - a.paid_hidden
        last_m = next(r.multiplier for r in result.records if r.hotkey == a.hotkey)
        print(
            f"{a.name:44} {a.paid_hl:10,.0f} {a.paid_builder:10,.0f} {a.paid_hidden:10,.0f} "
            f"{a.received_usd:10,.0f} {net:10,.0f} {last_m:5.3f}"
        )
        if a.hotkey.startswith("c"):
            worst_cheat = max(worst_cheat, net)
    print()
    print(
        "every zero-edge strategy nets negative:", "PASS" if worst_cheat < 0 else "FAIL", f"(worst {worst_cheat:,.0f})"
    )
    return 0 if worst_cheat < 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
