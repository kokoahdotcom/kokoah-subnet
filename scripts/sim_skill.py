"""Compare skill-score designs on simulated track records.

Run: uv run python scripts/sim_skill.py

Each profile is a daily-return process. For each design we report, per profile, how often the score is above zero
and its mean, after 30, 90 and 180 days of history. A good design pays real edge, pays luck little, pays a
martingale (small steady wins, rare big loss) little, and does not punish a steady low-volatility grinder.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable
from dataclasses import dataclass

# ----- profiles ----------------------------------------------------------------------------------------------------


@dataclass
class Profile:
    name: str
    draw: Callable[[random.Random, int], float]  # (rng, day index) -> daily return


def normal(mu: float, sd: float) -> Callable[[random.Random, int], float]:
    return lambda rng, _d: rng.gauss(mu, sd)


def martingale(rng: random.Random, _d: int) -> float:
    return 0.005 if rng.random() < 0.92 else -0.07  # mean -0.1%/day, strongly negative skew


def fading(rng: random.Random, d: int) -> float:
    return rng.gauss(0.004, 0.02) if d < 120 else rng.gauss(0.0, 0.02)  # edge for 4 months, then none


def one_bad_day(rng: random.Random, d: int) -> float:
    return -0.20 if d == 44 else rng.gauss(0.003, 0.02)  # a real edge with one -20% day in the middle


PROFILES = [
    Profile("luck, calm (0%, 2% vol)", normal(0.0, 0.02)),
    Profile("luck, wild (0%, 5% vol)", normal(0.0, 0.05)),
    Profile("lottery (0%, 8% vol)", normal(0.0, 0.08)),
    Profile("martingale (92% +0.5%, 8% -7%)", martingale),
    Profile("grinder (+0.05%, 0.4% vol)", normal(0.0005, 0.004)),
    Profile("real edge (+0.3%, 2% vol)", normal(0.003, 0.02)),
    Profile("strong edge (+0.6%, 2.5% vol)", normal(0.006, 0.025)),
    Profile("fading edge (+0.4% then 0%)", fading),
    Profile("real edge + one -20% day", one_bad_day),
]

# ----- statistics on a return series ---------------------------------------------------------------------------------


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def curve(returns: list[float]) -> list[float]:
    c = [1.0]
    for r in returns:
        c.append(c[-1] * (1 + r))
    return c


def calmar(returns: list[float], full: float) -> float:
    c = curve(returns)
    r = c[-1] - 1
    peak, mdd = 1.0, 0.0
    for x in c:
        peak = max(peak, x)
        mdd = max(mdd, 1 - x / peak)
    if r <= 0 or mdd > 0.5:
        return 0.0
    return clip01((r / max(mdd, 0.05)) / full)


def gain_to_pain(returns: list[float]) -> float:
    gains = sum(r for r in returns if r > 0)
    pains = -sum(r for r in returns if r < 0)
    if pains <= 0:
        return 10.0 if gains > 0 else 0.0
    return gains / pains


def prob_sharpe(returns: list[float], hurdle: float) -> float:
    """Probabilistic Sharpe ratio (Bailey and Lopez de Prado 2012): P(true daily Sharpe > hurdle), with skew and
    kurtosis adjustment. Returns 0.5 when nothing can be said."""
    n = len(returns)
    if n < 10:
        return 0.5
    mu = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    if sd <= 1e-12:
        return 0.5
    sr = mu / sd
    z3 = sum(((r - mu) / sd) ** 3 for r in returns) / n
    z4 = sum(((r - mu) / sd) ** 4 for r in returns) / n
    denom = math.sqrt(max(1e-9, 1 - z3 * sr + (z4 - 1) / 4 * sr * sr))
    z = (sr - hurdle) * math.sqrt(n - 1) / denom
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def ew_gain_to_pain(returns: list[float], half_life: float = 45.0, cap: float = 0.10) -> float:
    """Gain-to-pain with recency weighting (half-life in days, newest day weight 1) and daily returns winsorized
    at +-cap, so one disaster counts as one capped loss that fades, while a repeated pattern keeps counting."""
    n = len(returns)
    gains = pains = 0.0
    for i, r in enumerate(returns):
        w = 0.5 ** ((n - 1 - i) / half_life)
        r = max(-cap, min(cap, r))
        if r > 0:
            gains += w * r
        else:
            pains -= w * r
    if pains <= 0:
        return 10.0 if gains > 0 else 0.0
    return gains / pains


def blowups(returns: list[float], threshold: float = -0.10) -> int:
    return sum(1 for r in returns if r < threshold)


def months_positive(returns: list[float], k: int = 3) -> int:
    c = curve(returns)
    n = len(returns)
    cnt = 0
    for i in range(k):
        end = n - 30 * i
        start = end - 30
        if start < 0:
            break
        if c[end] / c[start] - 1 > 0:
            cnt += 1
    return cnt


# ----- score designs ----------------------------------------------------------------------------------------------


def score_f(returns: list[float]) -> float:
    """Current design (test F): mean of 30-day Calmar/2 and 90-day Calmar/3, two of three months positive."""
    g30 = calmar(returns[-30:], 2.0)
    if len(returns) < 90:
        return g30
    g90 = calmar(returns[-90:], 3.0)
    return 0.5 * (g30 + g90) if months_positive(returns) >= 2 else 0.0


def score_gpr(returns: list[float]) -> float:
    """Gain-to-pain over the last 90 days: 1.0 = break-even scores 0, 3.0 scores full."""
    return clip01((gain_to_pain(returns[-90:]) - 1) / 2)


def score_psr(returns: list[float]) -> float:
    """Confidence only: probabilistic Sharpe over all history (up to 180 days) against a 0.03 daily hurdle."""
    return clip01((prob_sharpe(returns[-180:], 0.03) - 0.5) / 0.45)


def score_composite(returns: list[float]) -> float:
    """First composite: confidence (PSR, hurdle 0.03) x quality (plain gain-to-pain over 90 days),
    with tail gates: 30-day drawdown <= 50%, worst day >= -25%, 30-day return > 0."""
    last30 = returns[-30:]
    c30 = curve(last30)
    peak, mdd = 1.0, 0.0
    for x in c30:
        peak = max(peak, x)
        mdd = max(mdd, 1 - x / peak)
    if c30[-1] - 1 <= 0 or mdd > 0.5 or min(returns[-90:]) < -0.25:
        return 0.0
    conf = clip01((prob_sharpe(returns[-180:], 0.03) - 0.5) / 0.45)
    qual = clip01((gain_to_pain(returns[-90:]) - 1) / 2)
    return conf * qual


def score_v3(returns: list[float]) -> float:
    """Proposed v3: no drawdown anywhere. Daily returns are winsorized at +-10% for both parts. confidence = PSR over
    up to 180 days (hurdle 0.03 daily Sharpe); quality = recency-weighted gain-to-pain over up to 180 days (half-life
    45 d), 1.0 -> 0, 2.2 -> 1. Repeated blow-ups (2+ days below -10% in 90 days) halve the quality; one is a capped
    loss that fades."""
    capped = [max(-0.10, min(0.10, r)) for r in returns[-180:]]
    conf = clip01((prob_sharpe(capped, 0.03) - 0.5) / 0.45)
    qual = clip01((ew_gain_to_pain(returns[-180:]) - 1) / 1.2)
    if blowups(returns[-90:]) >= 2:
        qual *= 0.5
    return conf * qual


def score_v4(returns: list[float]) -> float:
    """v4: windows capped at 60 days. confidence = PSR of capped returns over the last 60 days (hurdle 0.03);
    quality = recency-weighted gain-to-pain over the last 30 days, half-life 10 days, (GPR - 1)/1.2; blow-up rule
    over 60 days. The daily EMA smoothing (0.7 previous + 0.3 today) does not change the mean and is left out."""
    capped = [max(-0.10, min(0.10, r)) for r in returns[-60:]]
    conf = clip01((prob_sharpe(capped, 0.03) - 0.5) / 0.45)
    qual = clip01((ew_gain_to_pain(returns[-30:], half_life=10.0) - 1) / 1.2)
    if blowups(returns[-60:]) >= 2:
        qual *= 0.5
    return conf * qual


DESIGNS = [
    ("F: Calmar 30/90 + months (current)", score_f),
    ("gain-to-pain 90d", score_gpr),
    ("PSR confidence", score_psr),
    ("composite: PSR x gain-to-pain", score_composite),
    ("v3: PSR x recency-weighted gain-to-pain, no drawdown", score_v3),
    ("v4: same, windows capped at 60 / 30 days", score_v4),
]


def main(trials: int = 6000) -> None:
    rng = random.Random(3)
    horizons = (30, 90, 180)
    print(f"{'design / profile':44} " + " ".join(f"{f'{h}d pass/E[g]':>15}" for h in horizons))
    for dname, fn in DESIGNS:
        print(dname)
        results: dict[str, list[tuple[float, float]]] = {}
        for p in PROFILES:
            row = []
            for h in horizons:
                passed = total = 0.0
                for _ in range(trials):
                    rets = [p.draw(rng, d) for d in range(h)]
                    g = fn(rets)
                    passed += g > 0
                    total += g
                row.append((passed / trials, total / trials))
            results[p.name] = row
            print(f"  {p.name:42} " + " ".join(f"{a:6.0%} / {b:5.2f}" for a, b in row))
        luck = max(results[k][1][1] for k in results if k.startswith(("luck", "lottery")))
        mart = results["martingale (92% +0.5%, 8% -7%)"][1][1]
        edge = results["real edge (+0.3%, 2% vol)"][1][1]
        grind = results["grinder (+0.05%, 0.4% vol)"][1][1]
        ratio = edge / max(luck, 1e-9)
        bad = results["real edge + one -20% day"][1][1]
        print(f"  -> 90d: edge/luck {ratio:.1f}x, martingale {mart:.2f}, grinder {grind:.2f}, one bad day {bad:.2f}")


if __name__ == "__main__":
    main()
