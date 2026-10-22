# Kokoah subnet incentive mechanism: complete specification (v5.10, 2026-10-22)

Self-contained. Every symbol is defined here. v5.10 (2026-10-22): the data plane (§3). Hyperliquid's per-builder
daily file turned out to be incomplete: from 2026-10-01 to 2026-10-20 it held only the fills of about the first
twelve hours of each UTC day (00:00 to 12:12, checked for Kokoah's builder and two large builders; one day was
complete), and a day whose fills all fell after the cut has no file at all. The fills it leaves out did pay the
fee: Kokoah's builder earnings on the exchange ($7.58 by 2026-10-22) equal the builder fees in its traders' own fill
histories, while the files show $1.54. Each bound address's own fill history is therefore the primary source, the
file a cross-check; the file's whole-second times are matched on the second (earlier matching on the millisecond
never matched, so total fees fell back to fee rates); counterparties come from trade ids; a missing file no longer
holds a day back past a deadline; and the fees credited to traders are checked against the builder's earnings. v5.9
(2026-10-22): eligibility (§9a). A miner earns on a day only
when its account's time-weighted average value that day is at least $10,000 and it has qualified with its first
three trades worth $1,000 together; ineligible days earn no credit and no pay, the smoothed credit decays, and
nothing is owed later. Credit and qualification belong to one identity: a migration to a new address, a hotkey
swap or a new registration starts them from zero (pay history, skill and penalties stay). v5.8 (2026-10-20, found by the adversarial replay through the real
code): the 60-second timing heuristic for mirrored fills is gone. It flagged honest high-frequency traders against
each other by coincidence (same coin, opposite sides within a minute, similar sizes happen all day in a crowded
market). Its replacement is exact: the builder file names each fill's counterparty, so fills between two bound
addresses are removed before anything is computed, and an address whose volume against other bound addresses
exceeds the thresholds is treated as mirrored. The position-correlation rule stays for persistent hedging through
the market. v5.7 (owner, 2026-10-20): the builder fee is one rate on every order,
0.025%, attached when the order is placed, so maker and taker fills pay it alike, and Hyperliquid charges it on
both (confirmed from Kokoah's own fills and from other builders' daily files). Maker fills therefore earn their
fee credit in full (`c_m = 1`); the maker discount of earlier versions is no longer needed, because the second cap
already bounds every account by what it actually paid. v5.6 was the verified form of v5.5: `scripts/verify_mechanism.py` checks
every claim below numerically, and the verification corrected four things. (1) The tightest zero-edge strategy is a
rebated market maker (net −0.5 bps per round trip; it would need a hidden rebate of 0.55 bps a side to break even),
not the maximal-tier taker hedge (net −1.37 at Hyperliquid's best rebate); the second cap is safe for any `c_tot`
below 0.864. (2) The second cap is simply `c_tot` × the fill's total `fee` field, no floor; a maker rebate lowers
it. (3) The alpha price used by the caps is the day's average, never a spot price. (4) The holding factor reads the
day's minimum stake. It also records two chain facts: the owner's share is locked only when the owner enables the
auto-lock hyperparameter (policy: enabled), and burning through the owner hotkey scales the subnet's TAO draw down
by the burned share next tempo, which the design accepts rather than dodges.

Earlier versions: v5.5 set `c_max = 2` with the tier-aware second cap; v5.3 put the buyback on the owner's cadence
with cumulative backing; v5.1 replaced vesting with burning. Validator emissions and outside demand are out of scope.

## 0. Purpose and intention

A Bittensor subnet whose miners are traders. A miner binds one Hyperliquid address to a subnet hotkey and trades
perpetuals through the Kokoah builder code. Validators compute, from Hyperliquid data of the bound addresses and
chain state, each miner's share of the daily miner emission. Kokoah collects the builder fee on every such trade
and spends it buying back and burning the subnet's token (alpha). The intention: maximum trading volume through
Kokoah; similar volume, similar pay; skill and holding a small nudge (two traders with equal volume and fee tier
differ by at most 15%, one perfect at both, the other worst at both); no miner ever paid more than the fee-backed
amount, the rest burned; and no cheat that lets what miners can sell outrun what the fees can buy back.

## 1. Chain facts used

- Blocks 12 s; tempo 360 blocks = 72 min. Validators set weights every tempo; scores are recomputed once a day.
- Emission `E = 7,200` alpha per day, split `miner_slot = 0.41` (`A = 2,952` alpha a day), `owner_slot = 0.18`,
  `validator_slot = 0.41` (validators and their stakers; outside this mechanism). The chain allows owner ≤ 0.33, miner ≥ 0.33, validator ≤ 0.50. The owner's share is locked into
  conviction only when `owner_cut_auto_lock_enabled` is on (default off). **Policy: on**, so the owner never sells
  into its own buyback.
- Alpha pool: constant product, `price = tao_reserve / alpha_reserve` (TAO per alpha). Alpha emission to
  participants (`alpha_out`, up to 1 per block) does not depend on the subnet's TAO share; the TAO share only sets
  how much TAO (and matching alpha) is injected into the pool.
- Registration: burn plus a minimum collateral in alpha locked on the hotkey (owner-set, `SetMinCollateral`).
- **Burning (owner, 2026-10-20).** The unawarded remainder of the miner pool is directed to a **burn hotkey**: a
  hotkey registered on the subnet by the operator on a coldkey of its own (not the owner's coldkey), bound to no
  trader. A separate operator process (`kokoah-subnet owner burn`) destroys the alpha that lands there with
  `burn_alpha(hotkey, amount, netuid)`, signed by that coldkey; each burn emits `TokensBurned`. The owner hotkey is
  not used because the chain records incentive directed to owner hotkeys as `MinerBurned` and scales the subnet's
  TAO emission share by that fraction next tempo (research note 05, section 9); the burn hotkey removes the alpha
  without that scaling. The validator refuses to score until the configured burn hotkey is registered. Measured on a
  local chain (`scripts/measure_burn.py`): directing 85% of the pool to one hotkey leaves alpha emission to the
  other participants unchanged. Reconciliation: the daily artifact records the alpha directed to the burn hotkey;
  the burn transactions are public.
- **Buyback.** `add_stake_burn(hotkey, netuid, amount, limit)` buys alpha with TAO and burns it in one
  transaction, emitting `TokensBurned`; every buyback is one verifiable on-chain event.

## 2. Notation

| Symbol | Meaning | Default |
|---|---|---|
| `i`, `d` | a miner (one bound Hyperliquid address with its sub-accounts, and its hotkey); a scoring day (00:00 UTC) | |
| `f` | a fill of miner `i` carrying the Kokoah builder code; `n_f` notional (USD), `t_f` 1 if taker (`crossed`), `b_f = n_f × fee` the builder fee (`builderFee`), `fee_f` the fill's total fee field (Hyperliquid's fee plus `b_f`; negative Hyperliquid part means a rebate) | |
| `fee` | builder fee rate per fill | 0.00025 (0.025%) |
| `c_m` | maker fee credit (taker is 1.0); kept as a parameter, set to 1 since the builder fee is charged on maker fills too | 1.0 |
| `α` | daily smoothing weight of today's value, for credit, fees and skill | 0.3 |
| `E_d`, `flows_d` | end-of-day account value of the bound address (unrealized included); net deposits that day | |
| `p_d` | conversion rate for day `d`: the mean pool rate (TAO per alpha) at 20 blocks anchored to the scored day (the day's start block snapped to a tempo boundary, then one block per 1/20 of the day; identical for every validator) times the day's mean TAO/USD, the mean close of the exchange's hourly TAO-perpetual candles | |
| `g'_i`, `h'_i` | skill and holding factors, both in [0, 1] | |
| `c_max` | pay cap as a multiple of the multiplier-scaled Kokoah fee credit | 2.0 |
| `c_tot` | second cap: share of the account's total fees on the Kokoah leg (`fee_f` summed) | 0.75 |
| `w_s`, `w_h` | weights of skill and holding in the multiplier; `m_min = 1 − w_s − w_h` is its floor | 0.05, 0.10 |

## 3. What counts, and what never counts

Only the bound address's Hyperliquid data is used: perpetual fills with the Kokoah builder code (credit), the
address's daily account value and flows (skill), the hotkey's stake (holding). Fills without the builder code count
for nothing. **Perpetuals only (owner, 2026-10-21):** the app's builder code also rides on spot orders, where the
exchange charges it on sells; spot fills (pairs like `PURR/USDC` or index names like `@107`) are recorded but never
counted, in this version. No other platform is ever read.

Data plane (v5.10). Two public sources, for every bound address and day:

- **The address's own fill history** (`userFillsByTime`, paged), the primary source. Every fill carries its exact
  time, price, size, side, taker flag, its total fee (the exchange's fee plus the builder fee), the builder fee
  (`builderFee`), the trade id (`tid`, the same on both sides of a trade) and the client order id (`cloid`). It
  does not name the builder.
- **The per-builder daily file** (`builder_fills/{builder}/{YYYYMMDD}.csv.lz4`: time to the second, user, coin,
  side, px, sz, crossed, special trade type, tif, trigger flag, **counterparty**, closed PnL, TWAP id, builder fee),
  which names the builder but has held only about the first twelve hours of each UTC day. It is fetched by every
  validator, its hash is recorded and committed on chain by the owner, and it confirms fills and names their
  counterparties where it has them.

The two are merged per address on (coin, side, price, size, second), identical rows one to one. A fill in both is
taken from the history with the file's counterparty and builder fee. A fill only in the file is kept as it is (it
lies beyond the history's reach of 10,000 fills; its total fee then comes from the address's fee rates, `userFees`,
times notional). A fill only in the history counts as a builder fill when it paid a builder fee and either the
scored builder is the only builder the address has approved (`approvedBuilders`: the exchange charges a builder
fee only for an approved builder, so every builder fee is ours; at no more than the configured rate), or it carries
Kokoah's order marker (a `cloid` starting `0x6b6f`) at exactly the configured rate. Counterparties of history fills
come from trade ids: the validator reads the day's fills of every bound address and of up to 25 linked accounts per
address (sub-accounts and transfer partners), and a fill whose trade id appears among another known address's fills
traded with that address. A failed read of the history or of the approvals fails the day's scoring (retried within
the hour) rather than scoring the day without it.

**Earnings check.** The exchange reports what the builder has earned (`referral`: `builderRewards`). The validator
records it at every scoring run, and the health check raises an alarm when the builder fees of all counted fills
of bound addresses, over all scored days, exceed it: every credited fee must have reached the builder. (Kokoah's
other traders pay fees too, so earnings exceed the credited fees in normal operation.) This catches the one gap
the attribution rule leaves: a trader who approves another builder and gives its orders Kokoah's marker pays that
builder real fees (the one law still makes it a loss) while the subnet would credit them.

Removed before anything is computed:

1. fills whose counterparty is the same address or another address of the same cluster (same coldkey, sub-accounts
   of the master, addresses funded by the same bridge depositor or transferring between each other);
2. fills priced more than 0.5% from the mark (the close of the exchange's minute candle for the coin): valued at the
   mark for credit; and for skill, the money an off-market fill moves into the account (buying below the mark or
   selling above it) is removed from the day's return like a deposit, over all of the address's perpetual fills,
   builder code or not, so two accounts cannot manufacture a steady profit by trading each other at off-market prices;
3. **mirror rule (counterparties):** fills whose counterparty is another bound address (the builder file names the
   counterparty of every fill), measured as a share of the address's notional, then removed from credit: the subnet
   does not pay for volume between its own miners. Exact, so an honest trader is never flagged by coincidence;
4. **mirror rule (positions):** for every pair of bound addresses active in the same coin, the daily net position
   series over the last 30 days; correlation below −0.8 on at least 10 overlapping days marks the overlapping
   notional as mirrored.
   For both rules: mirrored notional above 25% of an address's notional is removed from credit (fills against
   bound counterparties are removed whatever the share); above 50%, both addresses get `g' = h' = 0` for 30 days
   and are reported (collateral stranded). These rules protect the
   leaderboard's integrity; the economics alone already make mirrored volume a loss (§11).

Abuse signals that do not change pay but are published and reviewed: the share of an address's credit from
positions held under one minute, and the share from fills against bound counterparties.

## 4. Daily fee credit, smoothed

```
Φ_i,d  = Σ_{f on day d}  b_f × ( t_f + (1 − t_f) × c_m )          with c_m = 1: the builder fee paid, maker or taker
Φ̂_i,d  = (1 − α) × Φ̂_i,d−1 + α × Φ_i,d                            70/30 smoothing; Φ̂ = 0 at the binding
```

A day's fees are worth 30% today, 21% tomorrow, 15% the day after; the weights sum to 1, so over time every
dollar of fees is counted exactly once. Scalpers and market makers earn credit on every fill like everyone else:
the fee is the same on every order, so a dollar of maker fees is worth exactly a dollar of taker fees to the
subnet's revenue, and the credit says so.

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
F̂_i,d     = (1 − α) × F̂_i,d−1 + α × Σ_f fee_f              smoothed total fees on the Kokoah leg (Hyperliquid's plus ours)
cap_i,d   = M_i,d × min( c_max × Φ̂_i,d ,  c_tot × F̂_i,d ) / p_d
pay_i,d   = min( share_i,d , cap_i,d )                    per-miner cap
burn_d    = A − Σ_i pay_i,d                               weight to the burn hotkey, destroyed by the operator's burn process
```

The first term is the generous cap (200% of the fee credit). The second follows each account's real cost: an
ordinary taker pays Hyperliquid 4.5 bps a side, so 75% of (4.5 + 2.5) = 5.25 bps a side, just above the first
term's 5.0, and the first term binds; a tier-6 account with the Diamond staking discount pays 1.44 bps a side, so
75% of (1.44 + 2.5) = 2.96 bps a side binds instead; a rebated market maker's `fee_f` is the builder fee minus the
rebate, so its cap is lower still. Whatever the fee tier, an account is never paid more than three quarters of what
it spent on the trade, so no fee tier makes farming pay (§11), whatever `c_max` is.

What this does:

- **Similar volume, similar pay, always.** Scarce pool (busy subnet): proportional shares, equal for equal volume
  whatever the fee tier. Rich pool (young subnet): everyone gets their cap, which follows what the account spent,
  so a tier-6 Diamond account is paid about 59% of what a tier-0 account with the same volume gets (it also paid
  less). The 15% rule holds in both regimes (the second cap is also multiplied by `M`).
- **Farms cannot profit at any pool size or fee tier.** Income per round trip is at most the smaller of 2 × the
  credited builder fee and 75% of the total fees paid on the Kokoah leg; the unavoidable cost is those total fees
  plus the other leg (§11).
- **Pay stays within 2 × fees, cumulatively.** The caps sum to at most `c_max × Σ Φ̂`, and the smoothing weights
  sum to 1, so up to any day the alpha paid (valued at the day's price) is worth at most 2 × the fees collected so
  far. Fees cover everything ever paid when miners keep at least half of it; the holding factor pays them to.
- **Nothing is hoarded.** The excess goes to the burn hotkey the same day and is destroyed on chain (§1).
- **The price cannot be gamed.** The caps convert dollars to alpha at the day's average price; a 30% dip at the
  scoring block would raise the alpha cap by 43% at a spot price and by 1.5% at the average.

## 7. Skill factor `g'` (windows 60 and 30 days, smoothed)

Daily returns of the bound address, deposits and withdrawals removed, capped at ±10%:

```
r_d = (E_d − E_{d−1} − flows_d − offmarket_d) / E_{d−1}       r̃_d = max(−0.10, min(0.10, r_d))
```

`flows_d` are deposits, withdrawals, account and vault transfers; `offmarket_d` is the money moved in through
off-market fills that day (§3, item 2).

Confidence, last 60 days: `PSR` = probabilistic Sharpe ratio of `r̃` (Bailey and López de Prado 2012; skew- and
kurtosis-adjusted) against a benchmark daily Sharpe ratio of 0.03 (about 0.5 annualized); 0.5 if fewer than 10
days; `confidence = clip((PSR − 0.5) / 0.45, 0, 1)`. Quality, last 30 days with weights `0.5^(age / 10 days)`:
`GPR = Σ w max(r̃, 0) / Σ w max(−r̃, 0)`, `quality = clip((GPR − 1) / 1.2, 0, 1)`, halved if two or more days in
the last 60 were below −10%.

```
g_raw,d = confidence × quality
g_i,d   = (1 − α) × g_i,d−1 + α × g_raw,d                 70/30 smoothing; g = 0 at the binding
g'_i,d  = min( 1 , g_i,d / 0.65 )                         0.65 is a strong trader
```

No drawdown anywhere. Calibration (mean `g_raw` after 60 days, 6,000 simulated accounts per profile,
`scripts/sim_skill.py`, re-run 2026-10-20): zero-edge 0.05 to 0.10, martingale 0.08, low-volatility grinder 0.30,
real edge 0.35, strong edge 0.56, real edge with one −20% day 0.20 (recovers to 0.36 after a quarter).

## 8. Holding factor `h'`

```
staked_i,d   = the day's minimum (over the 20 tempo samples) of alpha staked on i's own hotkey by its own coldkey
kept_i,d     = staked_i,d − collateral locked on the hotkey
target_i,d   = alpha paid to i in the last 60 calendar days
h'_i,d       = min( 1 , kept_i,d / max(target_i,d, 1 alpha) )
```

Keep what you were paid and the factor is 1; sell it all and it is 0; buying alpha counts; staking for one block
does not; the registration collateral does not count (v5.8: a fresh hotkey starts at 0, so re-registering to shed a
selling record gains nothing). Its effect on pay is at most `w_h` = 10% by design; whether it reduces selling is
measured (§15), not assumed.

## 9. Activity gates (for the multiplier's skill and holding parts, and the leaderboard)

Over the last 30 days: at least 10 completed round trips, at least 7 days with a builder-code fill, mean account
value at least $500. A miner failing a gate is paid at `M = 0.85`.

## 9a. Eligibility (v5.9): the equity floor and the qualifying trades

A miner is **eligible** on day `d` when both hold; otherwise it is paid nothing that day.

**Equity floor.** The day's average account value of the bound address is at least `E_min = $10,000`:

```
Ē_d = (1 / T) × ∫_{start of d}^{end of d} E(t) dt          T = 24 h
```

`E(t)` comes from the exchange's own recorded history of the account (the `portfolio` endpoint, window
`perpWeek`: the perps account value, recorded about every two hours), never from a single snapshot. Between
recorded points the value is a straight line; the day's two edges are interpolated from the points around them.
The window's live last point and the made-up point at its far edge are dropped, so every validator that asks
while the day is inside the window reads the same points, and each validator keeps the points it read
(`equity_points`). Fewer than 4 recorded points inside the day, or a history that does not reach back to the day's
start, make `Ē_d` **unknown**, and unknown is ineligible (recorded as an error, raised by the health check). An
account whose history begins during the day had nothing before its first point. A failed read of the history does
not make a trader ineligible: the day is not scored and is retried within the hour. A drop below the floor costs
that day only; there is no permanent disqualification, and rewards resume on the next day at or above it.

**Qualifying trades.** A new identity must first make `n_q = 3` qualifying trades with at least `N_q = $1,000` of
eligible notional together. A trade is one execution: the eligible fills (§3, after the exclusions and the mark
clip) of one coin and side at one millisecond, so an order that crosses several resting orders is one trade. Trades
are walked in time order; the counts carry from day to day; there is no waiting period. The first two trades earn
no credit; the trade that completes both counts, and every later one, earn credit. Trades on a day below the
equity floor still count toward qualification (they earn nothing that day). Qualification is kept for as long as
the identity lasts.

**An ineligible day.**

```
Φ_i,d = 0 and Σ fee_f = 0 for the smoothing        Φ̂ and F̂ decay by 0.7, nothing is added
miner i is left out of Σ_j score_j                 it takes no share, so it dilutes nobody
pay_i,d = 0                                        its slice of the pool goes to the burn hotkey
```

Volume, fills, account value and the skill smoothing are recorded as on any day. Credit from before the
ineligible days stays in `Φ̂` (decaying) because it was earned while eligible; credit is never added for
ineligible days, and nothing is paid for them later.

**Identity.** Credit (`Φ̂`, `F̂`) and qualification belong to one identity: one hotkey, one bound address, one
registration. They start from zero when the bound address changes (migration), when the hotkey is swapped on
chain, when the hotkey registers again after leaving, and naturally for a new hotkey. What a fresh identity must
not escape is kept: the pay history behind the holding target, the skill record and any mirror penalty (which
follows the address and its cluster, §3).

Effect on the rest: every formula of §4 to §9 is unchanged for eligible miners. The one law (§11) is unaffected
(an ineligible day pays nothing and costs the same), and sybil splitting now needs $10,000 of capital in every
account on top of the registration, collateral and gates.

## 10. Revenue and buyback

```
R_d        = Σ_i Σ_f b_f                                   builder-fee revenue of the day (USD)
Σ_{t ≤ d} paid_t  ≤  c_max × Σ_{t ≤ d} R_t                 cumulative backing, by construction (§6)
```

The owner buys back and burns alpha with the collected fees on a cadence of their choosing (every two or three
weeks is fine), each time as one `add_stake_burn` transaction. At `c_max = 2` the alpha paid out up to any date is
worth at most 2 × the fees collected, so the fees absorb everything paid as long as miners keep half of it; at
`c_max = 1` they absorb it all unconditionally. A coverage figure `coverage = cumulative buyback / cumulative alpha
sold by miner hotkeys` is published for information.

## 11. Attack accounting (per round trip of notional, bps, builder fee 2.5 a side, c_max = 2, c_tot = 0.75)

The general bound. Let `C` be the total fees paid on the Kokoah leg (Hyperliquid's plus ours, both sides) and `H`
the cost of the other leg. Income is at most `c_tot × C`, cost is `C + H`, so

```
net  ≤  −(1 − c_tot) × C − H  ≤  −(1 − c_tot) × 4.4 + 0.6  =  −0.5 bps
```

because `C ≥ 2 × (2.5 − 0.3) = 4.4` (the best Hyperliquid maker rebate is 0.3 bps a side) and `H ≥ −0.6` for the
same reason. Every zero-edge strategy loses for any `c_tot < 0.864`, at any pool size, whatever `c_max` is.
Searched over 105 combinations of fee tier, staking discount and maker or taker on the Kokoah leg
(`scripts/verify_mechanism.py`, 2026-10-20): worst net −0.5.

| Strategy | Pays Hyperliquid (Kokoah leg) | Pays Kokoah | Other leg | Income at most | Net |
|---|---|---|---|---|---|
| Wash: taker through Kokoah vs own maker elsewhere (tier 0) | 9 | 5 | 3 | min(10, 0.75 × 14 = 10.5) = 10 | −7 |
| Two bound accounts, taker vs maker, both through Kokoah | 9 + 3 | 5 + 5 | 0 | 10 + min(10, 0.75 × 8 = 6) = 16, and mirrored → 0 | −6 or worse |
| Hedge, taker on Kokoah (tier 0), hidden leg maker elsewhere | 9 | 5 | 3 | 10 | −7 |
| Hedge, taker on Kokoah, tier 6 + Diamond (1.44 a side), hidden leg at the best rebate | 2.88 | 5 | −0.6 | min(10, 0.75 × 7.88 = 5.91) = 5.91 | −1.37 (break-even needs a 0.99 bps rebate a side) |
| **Top-tier market maker, maker both legs, one through Kokoah** | −0.6 | 5 | −0.6 | min(10, 0.75 × 4.4 = 3.3) = 3.3 | **−0.5 (the tightest; break-even needs 0.55 bps a side)** |
| Instant round trips in one account (tier 0) | 9 | 5 | 0 | 10 | −4 |
| Dust account, huge volume | 9 | 5 | 0 | 10 | −4 |
| Lottery farm of small accounts | 9 | 5 | 0 | 10 | −4 per account |
| Volume timed to alpha price moves | its fees | its fees | | the caps are in USD at the day's average price | nothing to extract |
| Whale staking a lot, trading little | its fees | its fees | | pay ∝ its own volume; holding adds ≤ 10% | nothing to extract |
| Sybil splitting or merging | same | same | | shares and caps linear in credit (verified for k = 1, 2, 5) | 0 gain, burn and gates lost |
| Self-referral (10% of the exchange fee back) | its fees | its fees | −0.1 × exchange fee | at most 0.75 × C | −0.25 C + 0.1 HL < 0 at every tier (tier 0 taker: −2.6; tier 6: −1.7) |
| Two accounts trading at off-market prices to fake profit | its fees | its fees | | skill only (≤ 5%) | the transfer is removed from the return; the credit is valued at the mark |

Without the second cap, `c_max = 2` would pay the Diamond taker 10 against a cost of 7.28, a clear profit; the
second cap is what makes any `c_max` above about 1.4 safe. Lowering `c_tot` to 0.6 would move the tightest case
to −1.2 and is the knob to turn if a generous `c_max` worries anyone.

## 12. Parameters

| Parameter | Value |
|---|---|
| builder fee | 0.025% per fill (owner) |
| maker credit | 1.0 (the builder fee is charged on maker fills; the second cap makes a discount unnecessary) |
| smoothing `α` | 0.3, for credit, total fees and skill |
| multiplier | `0.85 + 0.05 g' + 0.10 h'` (floor `m_min`, weights `w_s`, `w_h`; the owner tunes the balance) |
| pay caps | `M × min(2 × smoothed fee credit, 0.75 × smoothed total fees on the Kokoah leg)`, in alpha at the day's average price; `c_tot` must stay below 0.864 |
| burn | the pool above the caps, same day, to the burn hotkey; destroyed with `burn_alpha` by the operator's process |
| buyback | the collected fees, on the owner's cadence, by `add_stake_burn` |
| owner share | `owner_cut_auto_lock_enabled` on |
| skill windows | confidence 60 days (PSR, benchmark Sharpe 0.03, ≥ 10 days), quality 30 days (gain-to-pain, half-life 10 days), blow-ups 60 days |
| `g'` full score | g = 0.65 |
| return cap | ±10% a day |
| holding target | alpha paid in the last 60 calendar days (floor 1 alpha); stake read as the day's minimum, collateral excluded |
| activity gates | ≥ 10 round trips, ≥ 7 active days, mean equity ≥ $500 (30 days) |
| eligibility | the day's average account value ≥ $10,000 (recorded history, ≥ 4 points in the day); 3 qualifying trades with ≥ $1,000 of eligible notional, the first two uncredited |
| mirror rules | fills against bound counterparties never count; position correlation < −0.8 on ≥ 10 days; exclude > 25%, zero > 50% |
| mark clip | 0.5% |
| data plane | the address's own fill history, merged with the builder file on the second; history-only fills count when the builder is the only approved one, or with Kokoah's order marker at exactly the fee rate; no file 12 h after the day: scored from the histories |
| migration | once per 60 days, signed by old address, new address and hotkey; skill, pay history and penalties carry over, credit and qualification start from zero (v5.9) |
| emission split | miner 41%, owner 18% (locked), validators 41% (owner, 2026-10-20; the chain's default) |

## 13. Worked examples

**Young subnet.** `A = 2,952` alpha at $5.33 (pool $15,734); fees today $2,000 (volume $8M). Two miners, equal
volume $4M, 100% taker at tier 0, `Φ̂ = $1,000` each, total fees on the leg $2,800 each. Miner A: strong skill and
holds everything, `M = 1.0`; miner B: no skill, sold everything, `M = 0.85`. Shares: A 1,596 alpha, B 1,356.
Caps: `min(2 × 1,000, 0.75 × 2,800) = min(2,000, 2,100) = $2,000` times `M`: A 2,000 / 5.33 = 375 alpha, B
0.85 × 2,000 / 5.33 = 319. Pay: A 375 ($2,000), B 319 ($1,700); burned 2,258 alpha (76% of the pool). Same volume, pay within 15%, total pay $3,700 against $2,000 of
fees collected: covered if the two keep at least 46% of it.

**Busy subnet.** Fees $87,700 a day (volume $350M). Σ caps ≈ 16,450 alpha > `A`: every share is below its cap,
nothing is burned, the pool is a volume rebate paying about 18% of fees back, shares within 15% for equal volume.

## 14. Account switching and migration

A miner's smoothed credit, skill and holding target belong to the binding. Migration to a new address is allowed
once per 60 days by a message signed by the old address, the new address and the hotkey; skill, pay history and
penalties carry over, while credit and qualification start from zero (v5.9, §9a). A fresh binding starts from zero
with `staked = collateral`. Clusters (§3) cover linked addresses. The chain keeps
only a hotkey's latest commitment, so a validator enforces the 60-day limit from the bindings it has accepted; a
validator that starts fresh accepts the current commitment on its signatures alone and converges with the others
after 60 days.

### 14a. Leaving, returning, switching (v5.8)

- **Deregistered or pruned, same hotkey comes back.** Records are kept by hotkey and pause while the hotkey is
  off the subnet (nothing can be paid to it); they resume when it is registered again, as a fresh identity for
  credit and qualification (v5.9, §9a): the new registration must qualify again. The holding target counts
  calendar days, so pay from before a long gap no longer weighs on it.
- **Deregistered, a new hotkey binds the same address.** A hotkey that left the subnet releases its address; the
  new hotkey starts fresh (no credit tail, no skill history, gates to pass again). The unpaid credit tail of the old
  hotkey is lost to the trader: with 70/30 smoothing, leaving right after a trading day forfeits about 70% of that
  day's credit, 49% of the day before, and so on. A new identity never gains on skill (the floor is 0.85; skill only
  adds) and never gains on holding (collateral does not count).
- **A mirror report follows the address and its cluster**, not the hotkey: binding a penalized address to a new
  hotkey keeps the penalty; a new address plus a new hotkey costs registration, collateral, the week of gates and
  the credit tail, for a penalty worth at most 15% of pay for 30 days.
- **Hotkey swap on chain** (same UID and coldkey, new hotkey): the pay history, skill and penalties follow the new
  hotkey, the credit and qualification do not (v5.9; the swap is recorded in `hotkey_swaps` and applied when the
  day is scored, so a re-score applies it again); the binding must be published again because the signed line
  names the hotkey.
- **Trading before joining.** Fills of a day scored before the binding was accepted are recorded as unbound and
  never pay. The bindings are read when a day is scored and the day's file is scored as a whole, so a binding
  accepted before a day is scored covers that whole day, including its earlier fills (they also count toward
  qualification). The account's own
  history does count for skill: on the first day an address is scored, the exchange's portfolio history (perps
  window) backfills daily account values, so a trader with a real record is recognized within days instead of
  starting from nothing.
- **The burn hotkey always receives at least 0.1% of the pool**, so a full subnet never prunes it in the scarce
  regime; the slice is burned like the rest.

### 14b. Robustness rules (v5.8)

- An exchange API failure never costs a trader the day's fee credit: credit always advances from the builder file;
  only the figures that depend on the failed read (account value, flows, positions, fee rates, stake) fall back,
  and the failure is recorded. The one exception is the account's recorded history behind the equity floor
  (§9a): a failed read fails the day's scoring, which is retried within the hour, so a trader is never made
  ineligible by an API failure.
- A re-score of a day starts from the state as it was before that day (state history), never from the already
  advanced state.
- The builder file is fetched again after 06:00 UTC and its hash compared with the one scored; a change is
  recorded and flagged, the scored file stands.
- External incentives on the exchange (referral rewards, points) are not the subnet's and do not change the bound:
  the subnet alone never makes a zero-edge round trip profitable.
- **Late or missing files.** The exchange writes a day's file about an hour after the day ends, and none when the
  part of the day it covers had no fills. The validator scores every unscored day in order, oldest first, waits
  for the file until 12 hours after the day ends (`file_deadline_hours`), and then scores the day from the bound
  addresses' histories alone (recorded as an error, so the health check shows it). A file published later is
  fetched by the re-check and its hash recorded; the scored day stands. `kokoah-subnet validator skip-day` remains
  for a day to be closed by announcement as a day with no fills (an outage of the exchange's history itself).
- **Day zero** is a parameter of the mechanism (`Params.day_zero`), so every validator counts penalty and holding
  windows from the same date.
- **Fee floor.** The one law needs the builder fee above `rebate + rebate / (1 − c_tot)` = 1.5 bps a side at
  `c_tot = 0.75`. The validator measures the effective fee in each day's file and records an error (raised by the
  health check) if it falls into the unsafe band; the parameters refuse a `c_tot` that the configured fee cannot
  support.
- **Rebate guard.** The bound assumes the exchange's deepest maker rebate is 0.3 bps a side; the health check reads
  the fee schedules of bound addresses and alarms on a deeper one, so the caps are re-checked before it matters.
- **Limits accepted.** Addresses controlled by smart-contract wallets cannot sign the binding line (EIP-191 only).
  The subnet's UID capacity is the chain's; a full subnet admits new traders only as the lowest-paid are pruned.

## 15. Verification

Done (2026-10-20, `scripts/verify_mechanism.py`, `scripts/sim_skill.py`, `scripts/measure_burn.py`): the one law
over 105 fee cases and the closed-form bound; cumulative backing on 200 random paths (worst paid / fees 1.36 at
`c_max = 2`); the 15% rule in both regimes; sybil neutrality for k = 1, 2, 5; the cap price under a 30% spot dip;
the skill calibration; on the local chain, incentive aimed at the owner hotkey is withheld and alpha emission to
the other participants is unchanged (§1); `scripts/replay.py --real` over a month of recorded public fills of four
active accounts (one with 15,471 fills) beside the synthetic attackers: all cheats negative, real accounts paid
proportionally; two independent validators on testnet 385 produced identical weights for the same day; the first
full burn cycle ran on testnet 385 (weights to the burn hotkey → the chain paid it the epoch's unawarded pool,
147.6 alpha → `burn_alpha` destroyed 147.59 alpha, leaving the dust floor).

Before launch:

1. **Burn process on testnet.** A month of daily figures: alpha directed to the burn hotkey against alpha actually
   burned (`TokensBurned` events), published side by side. The alpha side of the emission effect is done (§1).
2. **Adversarial replay.** Replay real public Hyperliquid histories (scalpers, market makers, swing traders) and
   simulated attackers side by side through the real scoring code: rebated market makers; cross-exchange hedges;
   one trader on several wallets (checking collateral, gates, holding target and smoothing, which are not linear);
   volume timed to alpha price moves; coordinated groups. Pass criterion: no strategy without a trading edge nets
   positive at any pool size, and real high-volume traders are paid their proportional share.
3. **Holding bonus.** Measure selling by miners with `h' = 1` against `h' = 0` on testnet; report; the weights
   stay inside the 15% rule.
4. **Data plane.** Done 2026-10-22: the builder file is fetchable by anyone for the real builder address but
   incomplete (about the first twelve hours of each day); the merged sources recover every builder fee the
   exchange credits to the builder ($7.584 found against $7.584466 reported), and every file row matched a history
   fill on the second.
5. **Leaderboard metrics** (no effect on pay): skill `g` with its parts, managed capital (mean equity), volume,
   dollar profit, execution quality (realized slippage against the mark), holding, coverage figures.

## 16. Assumptions and open points

1. Resolved 2026-10-20: the builder fee is charged on maker fills as on taker fills (Kokoah attaches one rate,
   0.025%, at order placement; other builders' daily files show maker fills paying it).
2. Builder attribution: the address's own history (complete, but it does not name the builder) merged with the
   per-builder daily file (names the builder, about half the day), with the approvals rule, the order marker and
   the earnings check (§3). The public per-user API keeps 10,000 fills per address; an address beyond that within
   a day keeps the file's fills for the part the history cannot reach.
3. The TAO/USD mark for `p_d`: the node's daily mean; an exchange median is the fallback.
4. The buyback path (USDC on Hyperliquid → TAO → `add_stake_burn`) and its publication.
5. Legal review of paying tokens for trading (pending for the app).
