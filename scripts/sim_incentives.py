"""Compare incentive formulas against honest traders and cheats, with Monte Carlo for luck.

Run: uv run python scripts/sim_incentives.py

Part 1 estimates how often a zero-edge account (a hedge leg, a coin flip) passes the skill tests, versus a trader
with a real edge. Part 2 prices the cheats under the candidate formulas: cost per round trip in basis points of
notional versus the most the subnet can pay, with Hyperliquid's fees (taker 4.5 bps, maker 1.5 bps, top-tier maker
rebate 0.3 bps) and a 2.5 bps builder fee per side. Everything is per unit of notional, so volume cancels out.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

TAKER, MAKER, REBATE = 4.5, 1.5, -0.3  # Hyperliquid, bps per side
BUILDER = 2.5  # Kokoah builder fee, bps per side
F = 2 * BUILDER  # builder fee per round trip, bps


# ----- Part 1: how often does luck pass the skill test? ------------------------------------------------------------


def equity_curve(days: int, drift: float, vol: float, rng: random.Random) -> list[float]:
    e = [1.0]
    for _ in range(days):
        e.append(e[-1] * (1 + rng.gauss(drift, vol)))
    return e


def calmar_g(curve: list[float], floor: float = 0.05, full: float = 2.0) -> float:
    r = curve[-1] / curve[0] - 1
    peak, mdd = curve[0], 0.0
    for x in curve:
        peak = max(peak, x)
        mdd = max(mdd, 1 - x / peak)
    if r <= 0 or mdd > 0.5:
        return 0.0
    return min(1.0, (r / max(mdd, floor)) / full)


def persistence_g(curve: list[float]) -> float:
    """Test F: the mean of the 30-day and 90-day Calmar scores, and 2 of the last 3 months positive, else 0."""
    g30 = calmar_g(curve[-31:])
    g90 = calmar_g(curve[-91:], full=3.0)
    months = [curve[-1 - 30 * i] / curve[-31 - 30 * i] - 1 for i in range(3)]
    return 0.5 * (g30 + g90) if sum(m > 0 for m in months) >= 2 else 0.0


@dataclass
class Profile:
    name: str
    drift: float
    vol: float


def part1(trials: int = 20000) -> dict[str, tuple[float, float, float, float]]:
    rng = random.Random(7)
    out: dict[str, tuple[float, float, float, float]] = {}
    for p in (
        Profile("zero edge, calm (hedge leg, 2% daily vol)", 0.0, 0.02),
        Profile("zero edge, wild (hedge leg, 5% daily vol)", 0.0, 0.05),
        Profile("real edge (+0.3%/day, 2% vol)", 0.003, 0.02),
        Profile("strong edge (+0.6%/day, 2.5% vol)", 0.006, 0.025),
    ):
        pass30 = g30 = pass90 = g90 = 0.0
        for _ in range(trials):
            c = equity_curve(90, p.drift, p.vol, rng)
            a = calmar_g(c[-31:])
            b = persistence_g(c)
            pass30 += a > 0
            g30 += a
            pass90 += b > 0
            g90 += b
        out[p.name] = (pass30 / trials, g30 / trials, pass90 / trials, g90 / trials)
    return out


# ----- Part 2: cheats priced per round trip of notional ------------------------------------------------------------


@dataclass
class Formula:
    name: str
    k_taker: float  # base rebate on taker builder fees, as a multiple
    k_maker: float  # base rebate on maker builder fees
    bonus: float  # extra multiple of rebate-weighted fees for a miner with full skill multiplier


def expected_payout(fm: Formula, taker_share: float, g_expected: float, legs_paid: float = 1.0) -> float:
    """Most the subnet pays per round trip (bps) to a zero-edge account: base plus the bonus times how often luck
    passes the test. `legs_paid` is the number of bound legs that collect the base (2 for a pair, both bound)."""
    phi = F * (taker_share * fm.k_taker + (1 - taker_share) * fm.k_maker)
    return legs_paid * phi + fm.bonus * phi * g_expected


@dataclass
class Attack:
    name: str
    cost: float  # bps per round trip, both legs
    taker_share: float  # share of the Kokoah leg's fills that are taker
    legs_paid: float


ATTACKS = [
    Attack("wash: taker through Kokoah against own maker elsewhere", 2 * TAKER + F + 2 * MAKER, 1.0, 1.0),
    Attack("hedge: normal maker both legs, other leg hidden", 2 * MAKER + F + 2 * MAKER, 0.0, 1.0),
    Attack("hedge: top-tier market maker both legs, other leg hidden", 2 * REBATE + F + 2 * REBATE, 0.0, 1.0),
    Attack("hedge: top-tier MM, taker on the Kokoah leg, hidden maker leg", 2 * TAKER + F + 2 * REBATE, 1.0, 1.0),
    Attack("bound pair: both legs through Kokoah, taker vs maker", 2 * TAKER + F + 2 * MAKER + F, 0.5, 2.0),
    Attack("lottery farm: N taker accounts, keep winners", 2 * TAKER + F, 1.0, 1.0),
]

FORMULAS = [
    Formula("v0: volume x skill, cap 1.5x fees, no base", 0.0, 0.0, 1.5),
    Formula("dYdX-style: 90% fee rebate to all fills, no bonus", 0.9, 0.9, 0.0),
    Formula("v1: 100% taker / 40% maker base + 3x skill bonus", 1.0, 0.4, 3.0),
    Formula("too generous: 100% / 60% base + 3x bonus", 1.0, 0.6, 3.0),
]


def main() -> None:
    print("Part 1: how often luck passes the skill tests (20,000 simulated 90-day accounts each)")
    print(f"{'profile':48} {'pass 30d':>9} {'E[g] 30d':>9} {'pass 90d':>9} {'E[g] 90d':>9}")
    res = part1()
    for name, (p30, g30, p90, g90) in res.items():
        print(f"{name:48} {p30:9.1%} {g30:9.2f} {p90:9.1%} {g90:9.2f}")
    g_zero_30 = max(res[k][1] for k in res if k.startswith("zero"))
    g_zero_90 = max(res[k][3] for k in res if k.startswith("zero"))
    print()
    print("Part 2: cheats, cost vs most the subnet pays, bps per round trip of notional (negative net = cheat loses)")
    for test_name, g_zero in (("30-day test only", g_zero_30), ("test F: 30/90-day mean + 2 of 3 months", g_zero_90)):
        print(f"\n  skill test: {test_name}, luck's expected multiplier {g_zero:.2f}")
        for fm in FORMULAS:
            print(f"  {fm.name}")
            for a in ATTACKS:
                pay = expected_payout(fm, a.taker_share, g_zero, a.legs_paid)
                net = pay - a.cost
                flag = "CHEAT WINS" if net > 0 else "ok"
                print(f"    {a.name:62} cost {a.cost:5.1f}  pays {pay:5.1f}  net {net:+5.1f}  {flag}")
    print()
    print("Honest traders under v1 (100%/40% base, 3x bonus), per round trip:")
    honest = (("retail taker, losing", 1.0, 0.0), ("day trader, decent", 0.8, 0.5), ("pro, persistent", 0.5, 1.0))
    for name, taker, g in honest:
        fm = FORMULAS[2]
        phi = F * (taker * fm.k_taker + (1 - taker) * fm.k_maker)
        pay = phi + fm.bonus * phi * g
        hl = 2 * (taker * TAKER + (1 - taker) * MAKER)
        net = hl + F - pay
        print(f"  {name:24} HL {hl:4.1f} + builder {F:3.1f}, back {pay:4.1f} in alpha -> net cost {net:+5.1f}")


if __name__ == "__main__":
    main()
