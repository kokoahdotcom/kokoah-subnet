# Kokoah subnet incentive mechanism: complete specification (v5.3, 2026-10-20)

Self-contained. Every symbol is defined here. v5.3 (owner, 2026-10-20): the buyback runs whenever the owner
decides (every two or three weeks is fine), so there is no daily matching rule and no reserve schedule; what the
design guarantees is cumulative: the alpha paid to miners is never worth more than the fees collected. Validator
emissions and outside demand are out of scope. The turnover cap and the 10-minute holding-time weighting are
removed (the fee-backed cap makes them unnecessary and they punished real scalpers and market makers).

## 0. Purpose and intention

A Bittensor subnet whose miners are traders. A miner binds one Hyperliquid address to a subnet hotkey and trades
perpetuals through the Kokoah builder code. Validators compute, from Hyperliquid data of the bound addresses and
chain state, each miner's share of the daily miner emission. Kokoah collects the builder fee on every such trade
and spends it buying back the subnet's token (alpha). The intention: maximum trading volume through Kokoah;
similar volume, similar pay; skill and holding a small nudge (two traders with equal volume differ by at most 15%,
one perfect at both, the other worst at both); no miner ever paid more than the fee-backed amount, the rest
burned; and no cheat that lets what miners can sell outrun what the fees can buy back.

## 1. Chain facts used

- Blocks 12 s; tempo 360 blocks = 72 min. Validators set weights every tempo; scores are recomputed once a day.
- Emission `E = 7,200` alpha per day, split `miner_slot = 0.33` (`A = 2,376` alpha a day), `owner_slot = 0.33`
  (auto-locked, never sold), `validator_slot = 0.34` (validators and their stakers; outside this mechanism).
- Alpha pool: constant product, `price = tao_reserve / alpha_reserve` (TAO per alpha).
- Registration: burn plus a minimum collateral in alpha locked on the hotkey (owner-set).
- **Burning:** weight sent to the subnet owner's hotkey is burned (or recycled, `recycle_or_burn`). The chain
  counts it as `MinerBurned` and scales the subnet's TAO emission share by `1 − MinerBurned` next tempo. Burning
  happens only while fees are small next to the pool. Its cost is measured before launch (§15, item 1); if it
  matters, the fallbacks are a sink UID on a non-owner coldkey burned by hand, or a higher `c_max`.

## 2. Notation

| Symbol | Meaning | Default |
|---|---|---|
| `i`, `d` | a miner (one bound Hyperliquid address with its sub-accounts, and its hotkey); a scoring day (00:00 UTC) | |
| `f` | a fill of miner `i` carrying the Kokoah builder code; `n_f` notional (USD), `t_f` 1 if taker, `b_f = n_f × fee` the builder fee | |
| `fee` | builder fee rate per fill | 0.00025 (0.025%) |
| `c_m` | maker fee credit (taker is 1.0) | 0.4 |
| `α` | daily smoothing weight of today's value, for credit and skill | 0.3 |
| `E_d`, `flows_d` | end-of-day account value of the bound address (unrealized included); net deposits that day | |
| `p_d` | alpha price in USD on day `d` = pool price (TAO) × TAO/USD mark from the node | |
| `g'_i`, `h'_i` | skill and holding factors, both in [0, 1] | |
| `c_max` | pay cap as a multiple of the multiplier-scaled Kokoah fee credit | 1.0 |
| `w_s`, `w_h` | weights of skill and holding in the multiplier; `m_min = 1 − w_s − w_h` is its floor | 0.05, 0.10 |

## 3. What counts, and what never counts

Only the bound address's Hyperliquid data is used: fills with the Kokoah builder code (credit), the address's
daily account value and flows (skill), the hotkey's stake (holding). Fills without the builder code count for
nothing. No other platform is ever read.

Removed before anything is computed:

1. fills whose counterparty is the same address or another address of the same cluster (same coldkey, sub-accounts
   of the master, addresses funded by the same bridge depositor or transferring between each other);
2. fills priced more than 0.5% from the mark: valued at the mark for notional and profit;
3. **mirror rule (timing):** opposite-side fills of two bound addresses in the same coin within 60 s and within 20%
   of the size;
4. **mirror rule (positions):** for every pair of bound addresses active in the same coin, the daily net position
   series over the last 30 days; correlation below −0.8 on at least 10 overlapping days marks the overlapping
   notional as mirrored.
   For both rules: mirrored notional above 25% of an address's 30-day notional is removed from credit; above 50%,
   both addresses get `g' = h' = 0` for 30 days and are reported (collateral stranded). These rules protect the
   leaderboard's integrity and the generous setting `c_max > 1`; at `c_max = 1` the economics alone already make
   mirrored volume a loss (§11).

Abuse signals that do not change pay but are published and reviewed: the share of an address's credit from
positions held under one minute, and the share from fills against bound counterparties.

## 4. Daily fee credit, smoothed

```
Φ_i,d  = Σ_{f on day d}  b_f × ( t_f + (1 − t_f) × c_m )          taker fills count fully, maker fills at 0.4
Φ̂_i,d  = (1 − α) × Φ̂_i,d−1 + α × Φ_i,d                            70/30 smoothing; Φ̂ = 0 at the binding
```

A day's fees are worth 30% today, 21% tomorrow, 15% the day after; the weights sum to 1, so over time every
dollar of fees is counted exactly once. Scalpers and market makers earn credit on every fill like everyone else.

## 5. The share: by volume, within 0.85 to 1.0

```
M_i,d     = m_min + w_s × g'_i,d + w_h × h'_i,d           the rate multiplier, in [m_min, 1]; 0.85 + 0.05 g' + 0.10 h'
score_i,d = Φ̂_i,d × M_i,d
share_i,d = A × score_i,d / Σ_j score_j,d                 the miner's proportional share of the day's pool
```

Two miners with equal volume are within 15% of each other whatever their skill and holding; a miner with twice
the volume gets about twice as much. The share is linear in fees with bounded multipliers.

## 6. The pay: capped per miner and per day, the rest burned

```
cap_i,d   = c_max × M_i,d × Φ̂_i,d / p_d                 alpha worth the miner's Kokoah fee credit times its multiplier
pay_i,d   = min( share_i,d , cap_i,d )                    per-miner cap
burn_d    = A − Σ_i pay_i,d                               weight to the owner's hotkey: burned
```

What this does:

- **Similar volume, similar pay, always.** Scarce pool (busy subnet): proportional shares. Rich pool (young
  subnet): everyone gets their cap, 85% to 100% of the Kokoah fee they paid. The 15% rule holds in both regimes.
- **Farms cannot profit at any pool size.** Income per round trip is at most `c_max × M ×` the credited builder
  fee; the unavoidable cost is Hyperliquid's fees on both legs plus the builder fee (§11).
- **Pay never outruns fees.** The caps sum to at most `c_max × Σ Φ̂`, and the smoothing weights sum to 1, so up to
  any day the alpha paid is worth at most `c_max ×` the fees collected so far. The buyback can run on any cadence
  the owner chooses and still covers everything ever paid (at `c_max = 1`).
- **Nothing is hoarded.** The excess is burned the same day.

## 7. Skill factor `g'` (windows 60 and 30 days, smoothed)

Daily returns of the bound address, deposits and withdrawals removed, capped at ±10%:

```
r_d = (E_d − E_{d−1} − flows_d) / E_{d−1}       r̃_d = max(−0.10, min(0.10, r_d))
```

Confidence, last 60 days: `PSR` = probabilistic Sharpe ratio of `r̃` against a daily hurdle of 0.03 (Bailey and
López de Prado 2012; skew- and kurtosis-adjusted; 0.5 if fewer than 10 days); `confidence = clip((PSR − 0.5) /
0.45, 0, 1)`. Quality, last 30 days with weights `0.5^(age / 10 days)`: `GPR = Σ w max(r̃, 0) / Σ w max(−r̃, 0)`,
`quality = clip((GPR − 1) / 1.2, 0, 1)`, halved if two or more days in the last 60 were below −10%.

```
g_raw,d = confidence × quality
g_i,d   = (1 − α) × g_i,d−1 + α × g_raw,d                 70/30 smoothing; g = 0 at the binding
g'_i,d  = min( 1 , g_i,d / 0.65 )                         0.65 is a strong trader
```

No drawdown anywhere. Calibration (mean `g_raw` after 60 days, 6,000 simulated accounts per profile): zero-edge
0.05 to 0.10, martingale 0.08, low-volatility grinder 0.30, real edge 0.35, strong edge 0.56.

## 8. Holding factor `h'`

```
staked_i,d   = alpha staked on i's own hotkey by its own coldkey (collateral, kept rewards, bought alpha)
target_i,d   = alpha paid to i in the last 60 days
h'_i,d       = min( 1 , staked_i,d / max(target_i,d, collateral_min) )
```

Keep what you were paid and the factor is 1; sell it all and it is 0; buying alpha counts. Its effect on pay is at
most 7.5% by design; whether it reduces selling is measured (§10), not assumed.

## 9. Activity gates (for the multiplier's skill and holding parts, and the leaderboard)

Over the last 30 days: at least 10 completed round trips, at least 7 days with a builder-code fill, mean account
value at least $500. A miner failing a gate is paid at `M = 0.85`.

## 10. Revenue and buyback

```
R_d        = Σ_i Σ_f b_f                                   builder-fee revenue of the day (USD)
Σ_{t ≤ d} paid_t  ≤  c_max × Σ_{t ≤ d} R_t                 cumulative backing, by construction (§6)
```

The owner buys back and burns alpha with the collected fees on a cadence of their choosing (every two or three
weeks is fine). At `c_max = 1` the fees collected up to any date are at least the value of all alpha paid out up
to that date, so even if every paid token were sold, the fees can absorb it. A coverage figure `coverage =
cumulative buyback / cumulative alpha sold by miner hotkeys` is published for information.

## 11. Attack accounting (per round trip of notional, bps, fee 2.5 a side, c_max = 1)

Income is the total pay, at most `c_max × M ×` the credited builder fee, so at most 1.0 × it.

| Strategy | Pays Hyperliquid | Pays Kokoah | Hidden leg | Income at most | Net |
|---|---|---|---|---|---|
| Wash: taker through Kokoah vs own maker elsewhere | 9 + 3 | 5 | 0 | 5 | −12 |
| Two bound accounts, taker vs maker, both through Kokoah | 9 + 3 | 5 + 5 | 0 | 5 + 2 (maker credit 0.4), and mirrored → 0 | −15 or worse |
| Hedge, taker on Kokoah, hidden leg maker elsewhere | 9 | 5 | 3 | 5 | −12 |
| Top-tier market maker, maker both legs, one through Kokoah | −0.6 | 5 | −0.6 | 2 | −1.8 |
| Instant round trips in one account | 9 | 5 | 0 | 5 | −9 |
| Dust account, huge volume | 9 | 5 | 0 | 5 | −9 (no turnover cap needed) |
| Lottery farm of small accounts | 9 | 5 | 0 | 5 | −9 per account |
| Volume timed to alpha price moves | its fees | its fees | | the cap is in USD at the day's price | nothing to extract |
| Whale staking a lot, trading little | its fees | its fees | | pay ∝ its own volume; holding adds ≤ 7.5% | nothing to extract |
| Sybil splitting or merging | same | same | | shares and caps linear in credit | 0 gain, burn lost (tested, §15) |

Every row is negative at every pool size. At `c_max = 1.5` the top-tier market maker row becomes −0.8 and the
rest stay clearly negative; `c_max` above 1.9 would let that row turn positive, and lowering the maker credit to
0.25 would widen every maker row.

## 12. Parameters

| Parameter | Value |
|---|---|
| builder fee | 0.025% per fill (owner) |
| maker credit | 0.4 |
| smoothing `α` | 0.3, for credit and skill |
| multiplier | `0.85 + 0.05 g' + 0.10 h'` (floor `m_min`, weights `w_s`, `w_h`; the owner tunes the balance) |
| pay cap `c_max` | 1.0 × multiplier × smoothed Kokoah fee credit, in alpha at the day's price (1.5 = generous) |
| burn | the pool above the caps, same day, via the owner's hotkey (fallbacks in §1) |
| buyback | the collected fees, on the owner's cadence |
| skill windows | confidence 60 days (PSR, hurdle 0.03, ≥ 10 days), quality 30 days (gain-to-pain, half-life 10 days), blow-ups 60 days |
| `g'` full score | g = 0.65 |
| return cap | ±10% a day |
| holding target | alpha paid in the last 60 days (floor: the collateral minimum) |
| activity gates | ≥ 10 round trips, ≥ 7 active days, mean equity ≥ $500 (30 days) |
| mirror rules | 60 s / 20% size; position correlation < −0.8 on ≥ 10 days; exclude > 25%, zero > 50% |
| mark clip | 0.5% |
| migration | once per 60 days, signed by old address, new address and hotkey; credit, skill and target carry over |
| emission split | miner 33%, owner 33% auto-locked, validators 34% |

## 13. Worked examples

**Young subnet.** `A = 2,376` alpha at $5.33 (pool $12,667); fees today $2,000 (volume $8M). Two miners, equal
volume $4M, 100% taker, `Φ̂ = $1,000` each. Miner A: strong skill and holds everything, `M = 1.0`; miner B: no
skill, sold everything, `M = 0.85`. Shares: A 1,284 alpha, B 1,092. Caps: A `1.0 × 1,000 / 5.33 = 188` alpha, B
`0.85 × 1,000 / 5.33 = 159`. Pay: A 188 ($1,000), B 159 ($850); burned 2,029 alpha. Same volume, pay within 15%,
total pay $1,850 against $2,000 of fees collected.

**Busy subnet.** Fees $87,700 a day (volume $350M). Σ caps ≈ 16,450 alpha > `A`: every share is below its cap,
nothing is burned, the pool is a volume rebate paying about 14% of fees back, shares within 15% for equal volume.

## 14. Account switching and migration

A miner's smoothed credit, skill and holding target belong to the binding. Migration to a new address is allowed
once per 60 days by a message signed by the old address, the new address and the hotkey; everything carries over.
A fresh binding starts from zero with `staked = collateral`. Clusters (§3) cover linked addresses.

## 15. Verification before launch

1. **Burn cost.** On the localnet, measure `MinerBurned` and the TAO-share scaling with 85%, 50% and 0% of the
   miner pool burned, and simulate the subnet's TAO inflow over 30, 90 and 180 days; decide between burning via the
   owner hotkey, a sink UID burned by hand, and a higher `c_max`.
2. **Adversarial replay.** Replay real public Hyperliquid histories (scalpers, market makers, swing traders) and
   simulated attackers side by side: rebated market makers; cross-exchange hedges; one trader on several wallets
   (checking collateral, gates, holding target and smoothing, which are not linear); volume timed to alpha price
   moves; coordinated groups. Pass criterion: no strategy without a trading edge nets positive at any pool size,
   and real high-volume traders are paid their proportional share.
3. **Holding bonus.** Measure selling by miners with `h' = 1` against `h' = 0` on testnet; report; the weights
   stay inside the 15% rule.
4. **Leaderboard metrics** (no effect on pay): skill `g` with its parts, managed capital (mean equity), volume,
   dollar profit, execution quality (realized slippage against the mark), holding, coverage figures.

## 16. Assumptions and open points

1. Whether the builder fee is charged on maker fills (Hyperliquid docs silent; if not, maker credit is moot).
2. Builder attribution needs node data; the public API lacks the builder address and keeps 10,000 fills per address.
3. The alpha price for the caps: pool price at the scoring block times the node's TAO/USD mark.
4. The buyback path (USDC on Hyperliquid → TAO → alpha) and its publication.
5. Legal review of paying tokens for trading (pending for the app).
