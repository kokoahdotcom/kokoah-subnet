# Kokoah subnet incentive mechanism: complete specification (v5.8, 2026-10-20)

Self-contained. Every symbol is defined here. v5.8 (2026-10-20, found by the adversarial replay through the real
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
| `p_d` | alpha price in USD for day `d`: the mean of the pool price sampled at every tempo boundary of the day (20 samples) times the day's mean TAO/USD mark from the node | |
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

Data plane. Hyperliquid publishes one file per builder and day (`builder_fills/{builder}/{YYYYMMDD}.csv.lz4`:
time, user, coin, side, px, sz, crossed, special trade type, tif, trigger flag, **counterparty**, closed PnL, TWAP
id, builder fee). That file is the canonical list of scored fills: every validator fetches it independently, and
the owner commits its hash on chain daily so disagreements are visible. The total `fee_f` comes from the address's
own public fills (`userFillsByTime`, matched by user, coin, time, price, size and side) or, for addresses beyond the
public limit of 10,000 fills, from the address's published fee rates (`userFees`) times notional.

Removed before anything is computed:

1. fills whose counterparty is the same address or another address of the same cluster (same coldkey, sub-accounts
   of the master, addresses funded by the same bridge depositor or transferring between each other);
2. fills priced more than 0.5% from the mark: valued at the mark for notional and profit;
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
r_d = (E_d − E_{d−1} − flows_d) / E_{d−1}       r̃_d = max(−0.10, min(0.10, r_d))
```

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
| mirror rules | fills against bound counterparties never count; position correlation < −0.8 on ≥ 10 days; exclude > 25%, zero > 50% |
| mark clip | 0.5% |
| migration | once per 60 days, signed by old address, new address and hotkey; credit, skill and target carry over |
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
once per 60 days by a message signed by the old address, the new address and the hotkey; everything carries over.
A fresh binding starts from zero with `staked = collateral`. Clusters (§3) cover linked addresses. The chain keeps
only a hotkey's latest commitment, so a validator enforces the 60-day limit from the bindings it has accepted; a
validator that starts fresh accepts the current commitment on its signatures alone and converges with the others
after 60 days.

### 14a. Leaving, returning, switching (v5.8)

- **Deregistered or pruned, same hotkey comes back.** Records are kept by hotkey and pause while the hotkey is
  off the subnet (nothing can be paid to it); they resume when it is registered again. The holding target counts
  calendar days, so pay from before a long gap no longer weighs on it.
- **Deregistered, a new hotkey binds the same address.** A hotkey that left the subnet releases its address; the
  new hotkey starts fresh (no credit tail, no skill history, gates to pass again). The unpaid credit tail of the old
  hotkey is lost to the trader: with 70/30 smoothing, leaving right after a trading day forfeits about 70% of that
  day's credit, 49% of the day before, and so on. A new identity never gains on skill (the floor is 0.85; skill only
  adds) and never gains on holding (collateral does not count).
- **A mirror report follows the address and its cluster**, not the hotkey: binding a penalized address to a new
  hotkey keeps the penalty; a new address plus a new hotkey costs registration, collateral, the week of gates and
  the credit tail, for a penalty worth at most 15% of pay for 30 days.
- **Hotkey swap on chain** (same UID and coldkey, new hotkey): the smoothed state follows the new hotkey; the
  binding must be published again because the signed line names the hotkey.
- **Trading before joining.** Fills before the binding are recorded as unbound and never pay. The account's own
  history does count for skill: on the first day an address is scored, the exchange's portfolio history (perps
  window) backfills daily account values, so a trader with a real record is recognized within days instead of
  starting from nothing.
- **The burn hotkey always receives at least 0.1% of the pool**, so a full subnet never prunes it in the scarce
  regime; the slice is burned like the rest.

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
4. **Data plane.** Confirm the builder-fills file is fetchable by anyone for the real builder address (unknown
   builders return 403), and that `fee_f` matching against `userFillsByTime` is exact.
5. **Leaderboard metrics** (no effect on pay): skill `g` with its parts, managed capital (mean equity), volume,
   dollar profit, execution quality (realized slippage against the mark), holding, coverage figures.

## 16. Assumptions and open points

1. Resolved 2026-10-20: the builder fee is charged on maker fills as on taker fills (Kokoah attaches one rate,
   0.025%, at order placement; other builders' daily files show maker fills paying it).
2. Builder attribution relies on Hyperliquid's per-builder daily file (public per builder, confirmed for the
   backend's ingest; see §15 item 4); the public per-user API keeps 10,000 fills per address.
3. The TAO/USD mark for `p_d`: the node's daily mean; an exchange median is the fallback.
4. The buyback path (USDC on Hyperliquid → TAO → `add_stake_burn`) and its publication.
5. Legal review of paying tokens for trading (pending for the app).
