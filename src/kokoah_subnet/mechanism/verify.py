"""Numerical checks of the claims in the mechanism specification. Run: uv run kokoah-subnet verify

Each check prints its figures and PASS or FAIL; the exit code is non-zero if any check fails.
"""

from __future__ import annotations

import random
import sys
from collections.abc import Callable

from kokoah_subnet.mechanism import DEFAULT, allocate, ema
from kokoah_subnet.mechanism.skill import skill_factor, skill_raw
from kokoah_subnet.mechanism.skillpool import (
    adjusted_skill,
    passes_entry,
    record_days,
    record_factor,
    skill_weight,
    volume_factor,
)

P = DEFAULT
A = P.miner_pool_alpha
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def pool_conservation() -> None:
    print("1. The whole miner pool is paid every day (no caps, no burn)")
    rng = random.Random(1)
    worst = 0.0
    for _ in range(2000):
        vol = {f"m{i}": rng.random() * 1000 for i in range(rng.randint(1, 30))}
        skl = {f"m{i}": max(0.0, rng.random() - 0.6) for i in range(rng.randint(0, 30))}
        alloc = allocate(vol, skl, 1.0, P)
        worst = max(worst, abs(alloc.paid_alpha - A))
    check("paid = pool on 2,000 random days", worst < 1e-6, f"worst gap {worst:.2e} alpha")
    alloc = allocate({"a": 1.0}, {"b": 1.0}, 1.0, P)
    check("80/20 split", abs(alloc.volume_pool - 0.8 * A) < 1e-9 and abs(alloc.skill_pool - 0.2 * A) < 1e-9)
    check("empty skill pool joins the volume pool", abs(allocate({"a": 1.0}, {}, 1.0, P).volume_pool - A) < 1e-9)


def volume_pool_sybil() -> None:
    print("2. The volume pool is proportional to fees: splitting or merging accounts changes nothing")
    for k in (1, 2, 5, 20):
        one = allocate({"x": 900.0, "rest": 600.0}, {}, 1.0, P).pays["x"]
        split = allocate({**{f"x{i}": 900.0 / k for i in range(k)}, "rest": 600.0}, {}, 1.0, P).pays
        total = sum(v for key, v in split.items() if key.startswith("x"))
        check(f"k = {k}", abs(total - one) < 1e-6, f"{total:.3f} vs {one:.3f} alpha")


def lottery(r: random.Random) -> float:
    """Small steady gains with a little noise, and a 5% chance a day of a 7.5% loss (a strategy that hides its risk
    until it lands)."""
    return 0.004 + r.gauss(0, 0.001) if r.random() > 0.05 else -0.075


def entry_rates() -> None:
    print("3. Entry test (PSR >= 0.95 over the record since joining, 30 to 90 days): who gets into the skill pool")
    profiles: dict[str, Callable[[random.Random], float]] = {
        "zero edge": lambda r: r.gauss(0, 0.02),
        "lottery": lottery,
        "real edge (Sharpe 1.6/yr)": lambda r: r.gauss(0.001, 0.01),
        "strong edge (Sharpe 3.2/yr)": lambda r: r.gauss(0.002, 0.01),
    }
    for days in (P.entry_min_days, P.entry_days):
        rates = {}
        for name, gen in profiles.items():
            rng = random.Random(7)
            n = 2000
            rates[name] = sum(passes_entry([gen(rng) for _ in range(days)], P) for _ in range(n)) / n
        print(f"      {days} days: " + ", ".join(f"{k} {v:.1%}" for k, v in rates.items()))
        check(f"{days} days: zero-edge accounts pass at most 5% of the time", rates["zero edge"] <= 0.05)
        if days == P.entry_days:
            check(f"{days} days: lottery accounts pass at most 2%", rates["lottery"] <= 0.02)
            check(
                f"{days} days: strong edges pass more than ten times as often as luck",
                rates["strong edge (Sharpe 3.2/yr)"] > 10 * max(rates["zero edge"], 0.001),
            )
    check(
        "a longer record weighs more, up to 90 days",
        record_factor(P.entry_min_days, P) == P.record_start_weight
        and record_factor(P.entry_min_days, P) < record_factor(60, P) < record_factor(P.entry_days, P) == 1.0
        and record_factor(400, P) == 1.0,
    )


def luck_weight() -> None:
    days = 180
    print(
        f"4. Skill weight per account over its first {days} days (record from 30 days, record weight, entry test,"
        " luck floor, full volume factor)"
    )
    profiles: dict[str, Callable[[random.Random], float]] = {
        "zero edge": lambda r: r.gauss(0, 0.02),
        "lottery": lottery,
        "strong edge (Sharpe 3.2/yr)": lambda r: r.gauss(0.002, 0.01),
    }
    means: dict[str, float] = {}
    young: dict[str, float] = {}  # per day while the record is under 90 days (a fresh account)
    grown: dict[str, float] = {}  # per day once it is 90 days or more
    for name, gen in profiles.items():
        rng = random.Random(9)
        n = 300
        by_day = [0.0] * days
        for _ in range(n):
            rets: list[float] = []
            g = 0.0
            for d in range(days):
                rets.append(gen(rng))
                g = ema(g, skill_raw(rets, P).raw, P)
                if passes_entry(rets, P):
                    w = skill_weight(skill_factor(g, P), P.skill_volume_cap_usd, False, P, days=record_days(rets, P))
                    by_day[d] += w / n
        means[name] = sum(by_day) / days
        young[name] = sum(by_day[: P.entry_days]) / P.entry_days
        grown[name] = sum(by_day[P.entry_days :]) / (days - P.entry_days)
        print(f"      {name}: {means[name]:.4f} a day (under 90 days {young[name]:.4f}, after {grown[name]:.4f})")
    strong = means["strong edge (Sharpe 3.2/yr)"]
    check("zero-edge luck takes under a tenth of a strong trader's weight", means["zero edge"] < 0.1 * strong)
    check(
        "a fresh zero-edge account is paid no more than an established one (new accounts do not farm luck)",
        young["zero edge"] <= grown["zero edge"],
    )


def agent_bonus() -> None:
    print("5. Agent bonus: +10% on the skill weight only")
    agent, human = skill_weight(0.8, 2e6, True, P, days=90), skill_weight(0.8, 2e6, False, P, days=90)
    check("skill weight x 1.1", abs(agent / human - 1.1) < 1e-12)
    vol = allocate({"agent": 100.0, "human": 100.0}, {}, 1.0, P).pays
    check("no effect on the volume pool", abs(vol["agent"] - vol["human"]) < 1e-9)
    print(f"      s(0.25) = {adjusted_skill(0.25, P)}, v($500k) = {volume_factor(5e5, P)}")


def main() -> int:
    for f in (
        pool_conservation,
        volume_pool_sybil,
        entry_rates,
        luck_weight,
        agent_bonus,
    ):
        f()
        print()
    print("All checks passed." if not failures else f"FAILED: {', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
