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


def entry_rates() -> None:
    print("3. Entry test (PSR over 90 days >= 0.95): who gets into the skill pool")
    profiles: dict[str, Callable[[random.Random], float]] = {
        "zero edge": lambda r: r.gauss(0, 0.02),
        "lottery": lambda r: 0.004 if r.random() > 0.05 else -0.075,
        "real edge (Sharpe 1.6/yr)": lambda r: r.gauss(0.001, 0.01),
        "strong edge (Sharpe 3.2/yr)": lambda r: r.gauss(0.002, 0.01),
    }
    rates = {}
    for name, gen in profiles.items():
        rng = random.Random(7)
        n = 2000
        rates[name] = sum(passes_entry([gen(rng) for _ in range(90)], P) for _ in range(n)) / n
    check("zero-edge accounts pass at most 5% of the time", rates["zero edge"] <= 0.05)
    check("lottery accounts pass at most 2%", rates["lottery"] <= 0.02)
    check(
        "strong edges pass more than ten times as often as luck",
        rates["strong edge (Sharpe 3.2/yr)"] > 10 * max(rates["zero edge"], 0.001),
    )


def luck_weight() -> None:
    print("4. Mean skill weight per account after 90 days (entry test, luck floor, full volume factor)")
    profiles: dict[str, Callable[[random.Random], float]] = {
        "zero edge": lambda r: r.gauss(0, 0.02),
        "lottery": lambda r: 0.004 if r.random() > 0.05 else -0.075,
        "strong edge (Sharpe 3.2/yr)": lambda r: r.gauss(0.002, 0.01),
    }
    means = {}
    for name, gen in profiles.items():
        rng = random.Random(9)
        total = 0.0
        n = 600
        for _ in range(n):
            rets: list[float] = []
            g = 0.0
            for _ in range(90):
                rets.append(gen(rng))
                g = ema(g, skill_raw(rets, P).raw, P)
            if passes_entry(rets, P):
                total += skill_weight(skill_factor(g, P), P.skill_volume_cap_usd, False, P)
        means[name] = total / n
    strong = means["strong edge (Sharpe 3.2/yr)"]
    check(
        "luck takes under a tenth of a strong trader's weight",
        means["zero edge"] < 0.1 * strong and means["lottery"] < 0.1 * strong,
    )


def agent_bonus() -> None:
    print("5. Agent bonus: +10% on the skill weight only")
    check("skill weight x 1.1", abs(skill_weight(0.8, 2e6, True, P) / skill_weight(0.8, 2e6, False, P) - 1.1) < 1e-12)
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
