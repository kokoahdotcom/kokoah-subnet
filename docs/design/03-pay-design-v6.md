# Pay design v6 (agreed 2026-10-22, not built yet)

Agreed; to be built as specification v6. Replaces the caps, the burn and the holding factor.

- **No caps, no burn.** The whole miner pool (A = 0.41 × 7,200 = 2,952 α a day) goes to miners, even when it
  is worth more than the fees they paid. Burn only when no miner has any weight at all (then the burn hotkey).
  The holding factor is removed. By design, volume farming becomes an open competition: farmers enter
  until their fees roughly equal what they receive, and their builder fees are Kokoah's revenue.
- **Two pools.** Volume pool 80% (2,361.6 α): shared by eligible smoothed builder fees, `vol_i = V × Φ̂_i / Σ Φ̂_j`.
  Skill pool 20% (590.4 α): `skill_i = 590.4 × w_i / Σ w_j`. Nobody in the skill pool: it joins the volume pool.
- **Skill score unchanged** (§7: PSR confidence × gain-to-pain quality, blow-up halving, 70/30 smoothing,
  `g' = min(1, g / 0.65)`).
- **Skill weight** `w_i = s_i × v_i` with the luck floor `s_i = max(0, (g'_i − 0.25) / 0.75)` and the volume
  participation factor `v_i = min(1, V_i,30d / $1,000,000)`, `V_i,30d` = eligible Kokoah perp notional over the
  last 30 days. No equity multiplier, no routed-share multiplier (equity misreads idle capital
  and leverage; the volume factor already counts only Kokoah trades).
- **Skill pool entry, all required:** eligible that day ($10,000 average equity, qualified with 3 trades /
  $1,000); ≥ 10 round trips and ≥ 7 active days in 30; mean equity ≥ $500; no mirror penalty; s_i > 0.
  Failing these still allows the volume pool.
- **Three additions agreed (2026-10-22), from a simulation of the unchanged score:** with the 0.25 floor alone,
  about 21% of zero-edge accounts entered the skill pool after 90 days and 5% scored near 1; per account a
  zero-edge trader took about a third of a real-edge trader's weight.
  1. **Entry test:** PSR of the capped daily returns over the last 90 days ≥ 0.95 (benchmark daily Sharpe 0.03).
     Simulated pass rates: zero edge ~2%, lottery ~0%, Sharpe 1.6/yr ~16%, Sharpe 3.2/yr ~49% (180 days: 2%, 0%,
     23%, 75%).
  2. **Copy rule:** bound accounts whose daily returns are almost identical (correlation above about 0.9 over 30
     or more common days) share one skill weight (also stops splitting above the $1M cap).
  3. **Longer backfill:** the exchange's all-time portfolio history, not only the last month, so experienced
     traders can pass the 90-day test soon after binding.
- **Keep:** eligibility (v5.9), the data plane (v5.10), mirror and cluster rules, the counterparty rule.
- **To redo with it:** the one-law section and attack table (fake volume no longer loses by design; the volume
  pool becomes a fee rebate competed for), whitepaper, simulator, verify script, replay.
- **Under discussion (2026-10-22): agent trading bonus** (+10% score for trades made by agents); how agent
  trades are verified is open.

