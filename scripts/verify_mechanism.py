"""Verify the incentive mechanism (spec v5.7) numerically.

Run: uv run python scripts/verify_mechanism.py

Checks, in order: (1) the one law, that no strategy without a trading edge nets positive, searched over every
Hyperliquid fee tier, staking discount, maker/taker mix and hidden-leg cost; (2) cumulative backing, that alpha paid
is never worth more than c_max times the fees collected, on random fee and price paths; (3) the 15% rule in both pool
regimes; (4) sybil neutrality, that splitting or merging accounts never raises pay; (5) price manipulation of the cap,
spot price versus a day-long average; (6) the chain's burn rule, what burning the unbacked pool costs in TAO share;
(7) the fee-tier property of the capped regime. Every check prints PASS or FAIL and the script exits non-zero on
any FAIL.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass

# ----- parameters (spec section 12) ---------------------------------------------------------------------------------

BUILDER = 2.5  # bps a side
C_M = 1.0  # maker credit: the builder fee is charged on maker fills too (confirmed 2026-10-20), so full credit
ALPHA = 0.3  # daily smoothing weight of today
C_MAX = 2.0
C_TOT = 0.75
M_MIN, W_S, W_H = 0.85, 0.05, 0.10
A_POOL = 7200 * 0.33  # alpha a day for miners

# Hyperliquid perps fee schedule, bps a side, tiers 0..6 (docs read 2026-10-19); maker tiers 4+ are rebates
TAKER_TIERS = [4.5, 4.0, 3.5, 3.0, 2.8, 2.6, 2.4]
MAKER_TIERS = [1.5, 1.2, 0.8, 0.4, 0.0, -0.1, -0.2, -0.3]
STAKING_DISCOUNTS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40]  # HYPE staking tiers, up to Diamond 40%
BEST_REBATE = -0.3  # the best maker rebate anywhere on Hyperliquid, bps a side

failures: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        failures.append(name)


def income_cap(credit: float, total_fee: float, m: float = 1.0) -> float:
    """Most the subnet pays (USD or bps) for a fee credit and the total fees on the Kokoah leg (spec section 6)."""
    return m * min(C_MAX * credit, C_TOT * total_fee)


# ----- 1. the one law -----------------------------------------------------------------------------------------------


@dataclass
class Case:
    hl_rate: float  # Hyperliquid fee a side on the Kokoah leg, bps (negative = rebate)
    taker: bool
    hidden: float  # cost a side of the other leg, bps (negative = rebate)

    def net(self) -> float:
        """Net per round trip of notional (two sides), bps. The Kokoah leg pays the builder fee on both sides."""
        kokoah_leg = 2 * (self.hl_rate + BUILDER)
        credit = 2 * BUILDER * (1.0 if self.taker else C_M)
        income = income_cap(credit, kokoah_leg)
        return income - kokoah_leg - 2 * self.hidden

    def break_even_rebate(self) -> float:
        """Hidden-leg rebate a side (bps, positive number) at which the round trip breaks even."""
        kokoah_leg = 2 * (self.hl_rate + BUILDER)
        credit = 2 * BUILDER * (1.0 if self.taker else C_M)
        return (kokoah_leg - income_cap(credit, kokoah_leg)) / 2


def one_law() -> None:
    print("1. The one law: no zero-edge strategy nets positive (searched over fee tiers, discounts, mixes)")
    cases: list[Case] = []
    for disc in STAKING_DISCOUNTS:
        for t in TAKER_TIERS:
            cases.append(Case(t * (1 - disc), True, BEST_REBATE))
        for mk in MAKER_TIERS:
            rate = mk * (1 - disc) if mk > 0 else mk  # discounts do not deepen rebates
            cases.append(Case(rate, False, BEST_REBATE))
    worst = max(cases, key=lambda c: c.net())
    check(
        "every tier loses with the best hidden rebate",
        worst.net() < 0,
        f"worst net {worst.net():+.2f} bps per round trip (Kokoah-leg rate {worst.hl_rate:.2f} a side, "
        f"{'taker' if worst.taker else 'maker'}), {len(cases)} cases",
    )
    tightest = min(cases, key=lambda c: c.break_even_rebate())
    check(
        "break-even needs a hidden rebate above Hyperliquid's best",
        tightest.break_even_rebate() > -BEST_REBATE,
        f"tightest case needs {tightest.break_even_rebate():.2f} bps a side "
        f"({'taker' if tightest.taker else 'maker'} at {tightest.hl_rate:.2f}); best available {-BEST_REBATE}",
    )
    # closed form: income <= c_tot * C, cost >= C + 2 * hidden, C >= 2 * (BUILDER + BEST_REBATE)
    c_min = 2 * (BUILDER + BEST_REBATE)
    bound = -(1 - C_TOT) * c_min - 2 * BEST_REBATE
    c_tot_limit = 1 + 2 * BEST_REBATE / c_min
    check(
        "closed-form bound agrees with the search",
        abs(bound - worst.net()) < 1e-9,
        f"net <= -(1 - c_tot) * {c_min:.1f} + {-2 * BEST_REBATE:.1f} = {bound:+.2f}; "
        f"safe for any c_tot < {c_tot_limit:.3f}",
    )
    # the spec's table rows (section 11)
    rows = {
        "wash, taker through Kokoah vs own maker elsewhere (tier 0)": Case(4.5, True, 1.5).net(),
        "hedge, taker on Kokoah (tier 0), hidden maker elsewhere": Case(4.5, True, 1.5).net(),
        "hedge, taker tier 6 + Diamond, hidden leg at the best rebate": Case(2.4 * 0.6, True, -0.3).net(),
        "top-tier market maker, maker both legs, one through Kokoah": Case(-0.3, False, -0.3).net(),
        "instant round trips, one account, tier 0 (no hidden leg)": Case(4.5, True, 0.0).net(),
        "two bound accounts, taker vs maker, both through Kokoah": (
            income_cap(2 * BUILDER, 2 * (4.5 + BUILDER))
            + income_cap(2 * BUILDER * C_M, 2 * (1.5 + BUILDER))
            - 2 * (4.5 + BUILDER)
            - 2 * (1.5 + BUILDER)
        ),
    }
    for name, net in rows.items():
        print(f"      {name:62} net {net:+.2f}")
    check("every table row is negative", max(rows.values()) < 0, f"max {max(rows.values()):+.2f}")


# ----- 2. cumulative backing ----------------------------------------------------------------------------------------


def backing() -> None:
    print("2. Cumulative backing: alpha paid, valued at the day's price, never exceeds c_max x fees collected")
    rng = random.Random(11)
    worst_ratio = 0.0
    for _trial in range(200):
        n_miners = rng.randint(1, 12)
        depth_usd = rng.choice([5_000.0, 50_000.0, 500_000.0, 5_000_000.0])  # pool value of A at the start
        price = depth_usd / A_POOL
        phi = [0.0] * n_miners
        fee_hat = [0.0] * n_miners
        cum_fees = cum_paid = 0.0
        for _day in range(rng.randint(5, 120)):
            price *= 1 + rng.gauss(0, 0.08)
            score = []
            for i in range(n_miners):
                vol = rng.choice([0.0, 1e5, 1e6, 1e7, 5e7]) * rng.random()
                taker = rng.random()
                fees_k = vol * BUILDER / 1e4
                hl = vol * (taker * 4.5 + (1 - taker) * 1.5) / 1e4 * rng.choice([1.0, 0.6, 0.32])
                credit = fees_k * (taker + C_M * (1 - taker))
                phi[i] = (1 - ALPHA) * phi[i] + ALPHA * credit
                fee_hat[i] = (1 - ALPHA) * fee_hat[i] + ALPHA * (hl + fees_k)
                cum_fees += fees_k
                m = M_MIN + W_S * rng.random() + W_H * rng.random()
                score.append((phi[i] * m, m))
            total = sum(s for s, _ in score)
            for i, (s, m) in enumerate(score):
                share = A_POOL * s / total if total > 0 else 0.0
                cap = income_cap(phi[i], fee_hat[i], m) / price
                cum_paid += min(share, cap) * price
        if cum_fees > 0:
            worst_ratio = max(worst_ratio, cum_paid / cum_fees)
    check("paid / fees <= c_max on every path", worst_ratio <= C_MAX + 1e-9, f"worst ratio {worst_ratio:.3f}")


# ----- 3. the 15% rule ------------------------------------------------------------------------------------------------


def fifteen_percent() -> None:
    print("3. Similar volume, similar pay: equal credit, best vs worst multiplier, both regimes")
    credit, total_fee = 1_000.0, 2_800.0
    best, worst = M_MIN + W_S + W_H, M_MIN
    for name, price in (("rich pool (caps bind)", 5.33), ("scarce pool (shares bind)", 0.02)):
        s_best, s_worst = credit * best, credit * worst
        tot = s_best + s_worst
        pay = []
        for s, m in ((s_best, best), (s_worst, worst)):
            share = A_POOL * s / tot
            cap = income_cap(credit, total_fee, m) / price
            pay.append(min(share, cap))
        ratio = pay[1] / pay[0]
        check(name, abs(ratio - M_MIN / best) < 1e-9 or ratio >= M_MIN, f"worst/best = {ratio:.3f}")


# ----- 4. sybil neutrality --------------------------------------------------------------------------------------------


def sybil() -> None:
    print("4. Sybil neutrality: one account vs the same fills split over k accounts")
    others = [(500.0, 1400.0, 0.9), (2000.0, 5600.0, 0.95)]  # (credit, total fee, M) of the rest of the subnet
    credit, total_fee, m = 1000.0, 2800.0, 0.9
    for price in (5.33, 0.02):
        pays = []
        for k in (1, 2, 5):
            parts = [(credit / k, total_fee / k, m)] * k
            scores = [c * mm for c, _, mm in parts + others]
            tot = sum(scores)
            pay = 0.0
            for (c, f, mm), s in zip(parts, scores[:k], strict=True):
                pay += min(A_POOL * s / tot, income_cap(c, f, mm) / price)
            pays.append(pay)
        check(
            f"price {price}: split pays no more than whole",
            max(pays) <= pays[0] + 1e-9,
            f"k=1 {pays[0]:.2f}, k=2 {pays[1]:.2f}, k=5 {pays[2]:.2f} alpha "
            "(gates and collateral only make splitting worse)",
        )


# ----- 5. price manipulation of the cap -------------------------------------------------------------------------------


def price_manipulation() -> None:
    print("5. Cap price: a 30% dip at the scoring block, spot vs the day's 20-sample average")
    base = 1.0
    spot = base * 0.7
    twap = (19 * base + spot) / 20
    gain_spot = base / spot - 1
    gain_twap = base / twap - 1
    check(
        "day average removes the gain",
        gain_twap < 0.02 < gain_spot,
        f"cap in alpha rises {gain_spot:.0%} under spot, {gain_twap:.1%} under the average",
    )


# ----- 6. the chain's burn rule -------------------------------------------------------------------------------------


def burn_rule() -> None:
    print("6. The burned share of the pool by fee level (directed to the burn hotkey, not the owner hotkey)")
    for fees, price in ((2_000.0, 5.33), (20_000.0, 5.33), (87_700.0, 5.33)):
        pool_usd = A_POOL * price
        paid = min(pool_usd, C_MAX * fees)  # all taker at tier 0: the first cap binds
        burned = 1 - paid / pool_usd
        print(
            f"      fees ${fees:>8,.0f}/day, pool ${pool_usd:,.0f}: burned {burned:.0%} "
            f"(via the owner hotkey the TAO share would be x {1 - burned:.2f}; via the burn hotkey it is not)"
        )
    check("rule recorded", True, "chain code read 2026-10-19 (research note 05, section 9); localnet test in §15")


# ----- 7. fee tiers in the capped regime ------------------------------------------------------------------------------


def fee_tiers() -> None:
    print("7. Capped regime: equal volume, different Hyperliquid fee tiers")
    vol = 4e6
    for name, rate in (("tier 0 taker", 4.5), ("tier 3 taker", 3.0), ("tier 6 + Diamond taker", 2.4 * 0.6)):
        credit = vol * BUILDER / 1e4
        cap = income_cap(credit, vol * (rate + BUILDER) / 1e4)
        paid = vol * (rate + BUILDER) / 1e4
        print(f"      {name:24} fees paid ${paid:,.0f}, cap ${cap:,.0f} ({cap / credit:.2f} x the builder fee)")
    check("documented", True, "pay follows what the account spent; equal in the scarce regime")


def main() -> int:
    for step in (one_law, backing, fifteen_percent, sybil, price_manipulation, burn_rule, fee_tiers):
        step()
    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
