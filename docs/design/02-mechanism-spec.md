# Kokoah subnet incentive mechanism: complete specification (v4, 2026-10-20)

Self-contained. Every symbol is defined here. Changes from v3.1: a two-tier pool (fee rebate capped at 100% of the
Kokoah fee paid, the rest paid to active miners by holding), windows capped at 30 and 60 days, daily 70/30
smoothing, Kokoah-only counting (no coverage factor), holding-time weighting of credit, and a position-based
detector for two-account hedges.

## 0. Purpose and intention

A Bittensor subnet whose miners are traders. A miner binds one Hyperliquid address to a subnet hotkey and trades
perpetuals through the Kokoah builder code. Validators compute, from Hyperliquid data of the bound addresses and
chain state, how much of the daily miner emission each miner receives. Kokoah collects the builder fee on every
such trade and spends it buying back the subnet's token (alpha). The intention: many traders, high volume through
Kokoah, similar volume paid similarly, skill and holding as a small nudge, all alpha paid to miners every day, and
revenue that always exceeds what volume-earners can sell.

## 1. Chain facts used

- Blocks 12 s; tempo 360 blocks = 72 min. Validators set weights every tempo; scores are recomputed once a day.
- Emission `E = 7,200` alpha per day, split `miner_slot = 0.33` (`A = 2,376` alpha a day, all paid out),
  `owner_slot = 0.33` (auto-locked, never sold), `validator_slot = 0.34`.
- Alpha pool: constant product, `price = tao_reserve / alpha_reserve` (TAO per alpha).
- Registration: burn plus a minimum collateral in alpha locked on the hotkey (owner-set). Every miner therefore has
  some staked alpha from day one.

## 2. Notation

| Symbol | Meaning | Default |
|---|---|---|
| `i` | a miner: one bound Hyperliquid address (with its sub-accounts) and its hotkey | |
| `d` | a scoring day (00:00 UTC) | |
| `f` | a fill of miner `i` carrying the Kokoah builder code; `n_f` notional (USD), `t_f` 1 if taker, `b_f = n_f × fee` the builder fee | |
| `fee` | builder fee rate per fill | 0.00025 (0.025%) |
| `c_m` | maker fee credit (taker is 1.0) | 0.4 |
| `T_hold` | holding time for full credit | 10 min |
| `α` | daily smoothing weight of today's value (the rest is yesterday's smoothed value) | 0.3 |
| `E_d`, `flows_d` | end-of-day account value of the bound address (unrealized included); net deposits during the day | |
| `p_d` | alpha price in USD on day `d` = pool price (TAO) × TAO/USD mark from the node | |
| `g_i`, `h_i` | skill score and holding multiplier, both in [0, 1] | |
| `κ` | skill nudge in tier 2 | 0.15 |

## 3. What counts, and what never counts

Only the bound address's Hyperliquid data is used: fills with the Kokoah builder code (credit), the address's
daily account value and flows (skill), the hotkey's stake (holding). Fills without the builder code count for
nothing. No other platform is ever read.

Removed before anything is computed:

1. fills whose counterparty is the same address, another address of the same cluster (same coldkey, sub-accounts
   of the master, addresses funded by the same bridge depositor or transferring between each other);
2. fills priced more than 0.5% from the mark: valued at the mark for notional and profit;
3. **mirror rule (timing):** opposite-side fills of two bound addresses in the same coin within 60 s and within 20%
   of the size;
4. **mirror rule (positions):** for every pair of bound addresses with overlapping activity in a coin, the daily net
   position series over the last 30 days; if their correlation is below −0.8 on at least 10 overlapping days, the
   overlapping notional is mirrored.
   For both rules: mirrored notional above 25% of an address's 30-day notional is removed from credit; above 50%,
   both addresses get `g = 0` and `h = 0.5` for 30 days and are reported (collateral stranded).

## 4. Daily fee credit, holding-time weighted

Each fill opens or closes FIFO lots per coin. A lot's hold time is the time from the opening fill to the fill that
closes it (lots still open at scoring time count their age so far). Both the opening and the closing fill of a lot
are weighted by `hold(f) = min(1, hold_time / T_hold)`.

```
Φ_i,d = Σ_{f on day d}  b_f × ( t_f × 1.0 + (1 − t_f) × c_m ) × hold(f)
Φ_i,d ≤ 30 × Ē_i,d × fee        turnover cap: credit at most the fee on 30 × mean equity of notional per day
```

Instant round trips earn almost nothing; a position held ten minutes earns in full. Market orders count fully,
limit orders at 40%.

Smoothed credit (the quantity that is paid), the owner's 70/30 rule:

```
Φ̂_i,d = (1 − α) × Φ̂_i,d−1 + α × Φ_i,d          α = 0.3;  Φ̂ = 0 before the binding
```

A day's fees are worth 30% today, 21% tomorrow, 15% the day after; over time every dollar of fees is counted
exactly once (the weights sum to 1), so the total pay follows total volume while the timing follows recent
activity.

## 5. Tier 1: the fee rebate (volume)

```
cap_i,d   = Φ̂_i,d / p_d                           the alpha worth exactly the (smoothed) Kokoah fee credit
T1_i,d    = min( cap_i,d ,  A × Φ̂_i,d / Σ_j Φ̂_j,d )
tier1_d   = Σ_i T1_i,d                            ≤ min( A , Σ_i cap_i,d )
```

Every miner is rebated in proportion to its fee credit, and no miner ever receives for volume more alpha than its
Kokoah fee credit is worth. Similar volume, similar pay, always.

Why this closes the farm: a farm's cost per round trip is Hyperliquid's fees on both legs plus the builder fee
plus any hidden-leg fee; its tier-1 income is at most the builder fee. It loses at least Hyperliquid's fees on
every round trip at any pool size, so the "scale up to break-even" strategy has no break-even. It also means the
alpha paid for volume is always worth at most the fees Kokoah collected, which are all spent on the buyback: tier
1 can never sell more than the buyback absorbs.

## 6. Tier 2: the holding reward (active miners only)

```
tier2_d   = A − tier1_d
T2_i,d    = tier2_d × S_i,d / Σ_j S_j,d
S_i,d     = staked_i,d × ( 1 − κ + κ × g_i,d )      only for miners with Φ̂_i,d > 0 and the activity gates passed
```

`staked_i` is the alpha staked on the miner's own hotkey by its own coldkey (collateral, kept rewards, bought
alpha). Whatever the pool does not spend on rebates goes to the miners who trade through Kokoah and hold alpha,
in proportion to what they hold, with skill as a 15% nudge. Nothing is withheld or recycled. When the pool is big
next to the fees (a young subnet), most of the day's alpha goes out this way; when fees grow past the pool, tier 1
takes it all and the subnet is a pure volume rebate. Alpha earned here that is sold stops earning here.

Total daily pay: `alpha_i,d = T1_i,d + T2_i,d`, weights `w_i = alpha_i,d / A` submitted every tempo.

## 7. Skill score `g_i,d` (a nudge, not a gate), windows 60 and 30 days

Daily returns of the bound address, deposits and withdrawals removed, capped at ±10%:

```
r_d = ( E_d − E_{d−1} − flows_d ) / E_{d−1}      r̃_d = max(−0.10, min(0.10, r_d))
```

Confidence (is the edge real?), last 60 days: the probabilistic Sharpe ratio `PSR` of `r̃` against a daily hurdle
of 0.03 (skew- and kurtosis-adjusted; 0.5 if fewer than 10 days): `confidence = clip((PSR − 0.5) / 0.45, 0, 1)`.

Quality (how good?), last 30 days with recency weights `w = 0.5^(age / 10 days)`:
`GPR = Σ w × max(r̃, 0) / Σ w × max(−r̃, 0)`, `quality = clip((GPR − 1) / 1.2, 0, 1)`, halved if two or more days in
the last 60 were below −10%.

```
g_raw,d = confidence × quality
g_i,d   = (1 − α) × g_i,d−1 + α × g_raw,d          daily 70/30 smoothing; g = 0 at the binding
```

Calibration (`scripts/sim_skill.py`, 6,000 accounts per profile, mean `g_raw` after 60 days): zero-edge 0.05 to
0.10, martingale 0.08, low-volatility grinder 0.30, real edge 0.35, strong edge 0.56, real edge with one −20% day
0.20 recovering to 0.36. No drawdown is used anywhere; one bad day is one capped loss that fades in weeks.

## 8. Holding multiplier `h_i,d` (used in the mirror penalty and the leaderboard)

```
target_i = max( alpha received in the last 60 days ,  30 × A / N_active )
h_i,d    = 0.5 + 0.5 × min( 1 , staked_i,d / target_i )
```

Tier 2 already pays by staked alpha directly, so `h` is informational (leaderboard) and the penalty value in §3.

## 9. Activity gates (for tier 2 and the leaderboard)

Over the last 30 days: at least 10 completed round trips, at least 7 days with a builder-code fill, mean account
value at least $500.

## 10. Economics

```
R_d        = Σ_i Σ_f b_f                         builder-fee revenue of the day (USD)
buyback_d  = β × R_d                              β = 1, daily, on chain, bought alpha burned
tier1 value ≤ Σ_i Φ̂_i,d  ≈  R (smoothed)         by construction (§5)
```

Revenue beats the sell pressure of volume-earners by construction. Tier 2 goes to holders; what they sell lowers
their own future tier 2. The price the buyback holds with no other demand: `price* = β R / (s_2 × tier2 + 0 ×
tier1)` where `s_2` is the share of tier-2 alpha that gets sold; with tier 1 fully covered, the floor depends only on
how much of the holding reward is sold.

## 11. Attack accounting (per round trip of notional, bps, fee = 2.5 a side)

| Strategy | Pays Hyperliquid | Pays Kokoah | Hidden leg | Tier-1 income at most | Net |
|---|---|---|---|---|---|
| Wash: taker through Kokoah vs own maker elsewhere | 9 + 3 | 5 | 0 | 5 (only the Kokoah leg is credited) | −12 |
| Two bound accounts, taker vs maker, both through Kokoah | 9 + 3 | 5 + 5 | 0 | 5 + 2 (maker credit 0.4), and mirrored → 0 | −10 or worse |
| Hedge, taker on Kokoah, hidden leg maker elsewhere | 9 | 5 | 3 | 5 | −12 |
| Top-tier market maker, maker both legs | −0.6 | 5 | −0.6 | 2 | −1.8 |
| Instant round trips in one account | 9 | 5 | 0 | ≈ 0 (hold weighting) | −14 |
| Lottery farm of small accounts | 9 | 5 | 0 | 5; tier 2 needs stake and trading | −9 per account |
| Sybil splitting | same fees | same fees | | tier 1 linear, tier 2 linear in stake | 0 gain, burn lost |

Every row is negative at every pool size. The only way to earn more than the Kokoah fee back is tier 2, which
requires staking alpha (demand, not sell pressure) and trading through Kokoah.

## 12. Parameters

| Parameter | Value |
|---|---|
| builder fee | 0.025% per fill (owner) |
| maker credit | 0.4 |
| `T_hold` | 10 min |
| turnover cap | credit ≤ fee on 30 × mean equity per day |
| smoothing `α` | 0.3 (70% yesterday's smoothed value, 30% today), for credit, profit and skill |
| tier 1 cap | 100% of the smoothed Kokoah fee credit, in alpha at the day's price |
| tier 2 | `staked × (0.85 + 0.15 × g)`, active miners only |
| skill windows | confidence 60 days (PSR, hurdle 0.03, ≥ 10 days), quality 30 days (gain-to-pain, half-life 10 days), blow-ups 60 days |
| return cap | ±10% a day |
| activity gates | ≥ 10 round trips, ≥ 7 active days, mean equity ≥ $500 (30 days) |
| holding target | max(60-day receipts, 30 × A / N_active) |
| mirror rules | 60 s / 20% size; position correlation < −0.8 on ≥ 10 days; exclude > 25%, zero > 50% |
| mark clip | 0.5% |
| migration | once per 60 days, signed by old address, new address and hotkey; record and stake target carry over |
| emission split | miner 33%, owner 33% auto-locked, validators 34% |
| buyback | 100% of fees, daily, on chain |

## 13. Worked example (one day)

`A = 2,376` alpha, alpha price $5.33 (0.02 TAO at TAO $266.54), fee 0.025%, maker credit 0.4. Smoothed credits
equal today's for simplicity; all positions held over 10 minutes.

- Anna: $5M volume, 80% taker → `Φ̂ = 0.00025 × 5,000,000 × 0.88 = $1,100`; staked 400 alpha; `g = 0.35`.
- Bob: $40M, 60% taker → `Φ̂ = $7,600`; staked 1,500 alpha; `g = 0.2`.
- Cara: $300k, 90% taker → `Φ̂ = $70.5`; staked 2,000 alpha (she bought some); `g = 0.5`.
- Σ Φ̂ = $8,770.5.

Tier 1. Proportional shares of `A`: Anna 12.5% = 298 alpha ($1,588), Bob 86.7% = 2,059 alpha ($10,975), Cara
0.8% = 19 alpha ($102). Caps (`Φ̂ / p`): Anna 1,100 / 5.33 = 206 alpha; Bob 1,426 alpha; Cara 13 alpha. Everyone is
above cap, so tier 1 pays the caps: Anna 206, Bob 1,426, Cara 13; `tier1 = 1,645` alpha ($8,770, exactly the
fees collected). Nobody is paid more than they paid Kokoah.

Tier 2. `tier2 = 2,376 − 1,645 = 731` alpha. `S = staked × (0.85 + 0.15 g)`: Anna 400 × 0.9025 = 361; Bob 1,500 ×
0.88 = 1,320; Cara 2,000 × 0.925 = 1,850. Σ = 3,531. Anna 75 alpha, Bob 273, Cara 383.

Pay: Anna 281 alpha ($1,498 on $1,250 of fees), Bob 1,699 ($9,056 on $10,000), Cara 396 ($2,111 on $75: she holds
the most alpha). Volume paid the same rate for everyone (100% of fees); holding paid the rest.

Economics: revenue $8,770 = buyback. Tier-1 alpha is worth $8,770, so even if every tier-1 token were sold, the
buyback covers it. Tier-2 alpha ($3,897) went to the three largest holders; what they sell is the only sell
pressure the buyback does not already cover.

With ten times the volume ($450M a day, fees $87,700), the caps sum to 16,450 alpha, above `A`: tier 1 takes the
whole pool (each miner gets `A × Φ̂ / Σ Φ̂`, about 14% of fees back), tier 2 is zero, and the subnet is a pure
volume rebate.

## 14. Account switching and migration

A miner's record, smoothed credit, skill and stake target belong to the binding. Migration to a new address is
allowed once per 60 days by a message signed by the old address, the new address and the hotkey; everything
carries over. A fresh binding starts with `Φ̂ = 0`, `g = 0`, `staked = collateral`, and must pass the activity
gates for tier 2. Clusters (§3) cover linked addresses.

## 15. Assumptions and open points

1. Whether the builder fee is charged on maker fills (Hyperliquid docs silent; if not, maker credit is moot).
2. Builder attribution needs node data; the public API lacks the builder address and keeps 10,000 fills per address.
3. The position-correlation detector needs per-address fills from node data for all bound addresses (available).
4. The alpha price used for the tier-1 cap: pool price at the scoring block times the node's TAO/USD mark.
5. Legal review of paying tokens for trading (pending for the app).
