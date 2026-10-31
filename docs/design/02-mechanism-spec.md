# Kokoah subnet incentive mechanism: complete specification (v6.4)

Self-contained. Every symbol is defined here. The whole miner emission goes to miners every day: 80% shared by the
eligible smoothed builder fees each miner paid (§5), 20% shared by skill weight among miners who prove a trading
record (§7a). Nothing is capped or burned in normal operation. The volume pool pays every miner in proportion to the
builder fees it pays, and every builder fee funds the buyback (§10, §11). The skill pool is protected against luck, copies and splitting by an entry test (a probabilistic Sharpe ratio of at
least 0.95 over 90 days), a luck floor, a volume participation factor capped at $1M over 30 days, and a copy rule.
A miner's agent running in a cloud confidential VM with Kokoah's harness, verified by the validators themselves,
earns 10% more skill weight (§16). Validator emissions and outside demand are out of scope.

## 0. Purpose and intention

A Bittensor subnet whose miners are traders. A miner binds one Hyperliquid address to a subnet hotkey and trades
perpetuals through the Kokoah builder code, by hand or with an agent. Validators compute, from Hyperliquid data of
the bound addresses and chain state, each miner's share of the daily miner emission. Kokoah collects the builder fee
on every such trade and spends it buying back and burning the subnet's token (alpha). The purpose: trading agents,
and traders, whose records are verified from public data. The intention: activity rewarded in proportion to the
builder fees it pays; a separate reward for proven trading skill that luck, copies and split accounts cannot
capture; more skill weight for verified agents, the subnet's main purpose; and the whole emission paid to miners
every day. Verified agents with long records are to be offered to traders on Kokoah as strategies, and that revenue
goes to the buyback too; that happens outside the mechanism and never changes a score.

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
- **Fallback hotkey.** Nothing is burned in normal operation: the whole pool goes to miners. Only a day on
  which no miner has any weight at all (no eligible fees and no skill weight) goes to the **burn hotkey**, a hotkey
  registered by the operator on a coldkey of its own and bound to no trader, whose alpha the operator's burn
  process destroys with `burn_alpha(hotkey, amount, netuid)`; without a registered burn hotkey that day goes to the
  owner hotkey. The owner hotkey is avoided where possible because the chain records incentive directed to owner
  hotkeys as `MinerBurned` and scales the subnet's TAO emission share by that fraction next tempo.
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
| `g'_i` | skill score in [0, 1] (§7) | |
| `s_i`, `v_i`, `w_i` | skill above the luck floor, volume participation, skill weight (§7a) | |
| `A_d`, `V_d`, `S_d` | the day's miner emission, volume pool, skill pool (§5) | 2,952; 80%; 20% |

## 3. What counts, and what never counts

Only the bound address's Hyperliquid data is used: perpetual fills with the Kokoah builder code (credit), the
address's daily account value and flows (skill); the hotkey's stake is recorded for information only (§8). Fills
without the builder code count for nothing. **Perpetuals only:** the app's builder code also rides on spot orders, where the
exchange charges it on sells; spot fills (pairs like `PURR/USDC` or index names like `@107`) are recorded but never
counted. No other platform is ever read.

Data plane. Two public sources, for every bound address and day:

- **The address's own fill history** (`userFillsByTime`, paged), the primary source. Every fill carries its exact
  time, price, size, side, taker flag, its total fee (the exchange's fee plus the builder fee), the builder fee
  (`builderFee`), the trade id (`tid`, the same on both sides of a trade) and the client order id (`cloid`). It
  does not name the builder.
- **The per-builder daily file** (`builder_fills/{builder}/{YYYYMMDD}.csv.lz4`: time to the second, user, coin,
  side, px, sz, crossed, special trade type, tif, trigger flag, **counterparty**, closed PnL, TWAP id, builder fee),
  which names the builder but may not cover the whole day, so the history is the primary source. It is fetched by
  every validator, its hash is recorded and committed on chain by the owner, and it confirms fills and names their
  counterparties where it has them.

The two are merged per address on (coin, side, price, size, second), identical rows one to one. A fill in both is
taken from the history with the file's counterparty and builder fee. A fill only in the file is kept as it is (it
lies beyond the history's reach of 10,000 fills; its total fee then comes from the address's fee rates, `userFees`,
times notional). A fill only in the history is a candidate when it paid a builder fee of at most the configured
rate, and counts only once its **order is confirmed**: the address's own transactions on the exchange's explorer
(`userDetails`) list every order it signed with its builder field and client order id, and a candidate counts when
its client order id belongs to an order the address placed with Kokoah's builder (and was not modified afterwards,
a modify carries no builder field), or, for a taker fill, when the transaction that placed and filled it
(`txDetails` of the fill's hash) is such an order. Builder approvals are not used: the order's own builder field
decides, and it cannot be changed after the fact. Counterparties of history fills come from trade ids: the
validator reads the day's fills of every bound address and of up to 25 linked accounts per address (its
sub-accounts first, then its transfer partners by the amount moved, largest first), and a fill whose trade id
appears among another known address's fills traded with that address. A failed read of the history, of the
explorer or of a linked account fails the day's scoring until the file deadline (12 hours after the day ends;
retried within the hour); after the deadline the day is scored, and only the fills that needed the failed read go
uncounted for that address (the failure is recorded): when its sub-accounts or transfer partners cannot be read,
every fill whose counterparty is not a bound address; when a linked account's fills cannot be read, the history
fills whose counterparty stays unnamed.

**Earnings check.** The exchange reports what the builder has earned (`referral`: `builderRewards`). The validator
records it at every scoring run, and the health check raises an alarm when the builder fees of all counted fills
of bound addresses, over all scored days, exceed it: every credited fee must have reached the builder. (Kokoah's
other traders pay fees too, so earnings exceed the credited fees in normal operation.)

Removed before anything is computed:

1. fills whose counterparty is the same address or another address of the same cluster (bound addresses whose
   hotkeys share a coldkey, sub-accounts of the master, addresses transferring between each other);
2. fills priced more than 0.5% from the mark (the close of the exchange's minute candle for the coin): valued at the
   mark (their notional counts at the mark price; their builder fee, the credit, is the fee they paid); and for skill, the money an off-market fill moves into the account (buying below the mark or
   selling above it) is removed from the day's return like a deposit, over all of the address's perpetual fills,
   builder code or not, so two accounts cannot manufacture a steady profit by trading each other at off-market prices.
   A failed read of a coin's minute candles fails the day's scoring until the file deadline; after it, the coin's
   builder fills go uncounted and the day's return of every address that traded the coin is unknown. The exchange
   keeps only its latest 5000 minute candles (about three and a half days): a day scored later, whose candles no
   longer reach back to its start, is settled at once the same way, never clipped in part. In a coin in which an
   address made more than 25% of the day's traded notional (the same minute candles' volume at their close), the
   marks are its own prices and its counterparty's, so they tell nothing about what it made there. The day stays
   known (a day left out would hide a loss): its loss counts in full, its profit counts as nothing (removed like
   money moved in), and its builder fills still count;
3. **bound counterparties:** fills whose counterparty is another bound address (the builder file names the
   counterparty of every fill) are removed from credit: the subnet does not pay for volume between its own miners.
   They report no one: any miner can take another's resting orders, so who traded with whom says nothing about who
   meant to (their share of the address's notional is recorded);
4. **mirror rule (positions):** for every pair of bound addresses one owner tied together (hotkeys on one coldkey, a
   master and its sub-accounts: the links only the owner can create, §14a) and active in the same coin, the net
   position at each day's end (the position the validator reads, less the fills since the day ended, at the coin's
   last minute mark of the day: the same whenever a validator reads it) over the last 30 days; correlation below
   −0.8 on at least 10 overlapping days marks the overlapping notional as mirrored. Pairs without such a link are
   not compared, since anyone can hold the opposite of another trader's position. Mirrored notional above 25% of an
   address's position notional is removed from its credit; an address with more than 50% mirrored is reported, and
   for 30 days it and the addresses its owner controls (§14a) take no part in the skill pool. These rules protect
   the leaderboard's integrity.

## 4. Daily fee credit, smoothed

```
Φ_i,d  = Σ_{f on day d}  b_f × ( t_f + (1 − t_f) × c_m )          with c_m = 1: the builder fee paid, maker or taker
Φ̂_i,d  = (1 − α) × Φ̂_i,d−1 + α × Φ_i,d                            70/30 smoothing; Φ̂ = 0 at the binding
```

A day's fees are worth 30% today, 21% tomorrow, 15% the day after; the weights sum to 1, so over time every
dollar of fees is counted exactly once. Scalpers and market makers earn credit on every fill like everyone else:
the fee is the same on every order, so a dollar of maker fees is worth exactly a dollar of taker fees to the
subnet's revenue, and the credit says so.

## 5. The two pools

```
A_d   = 0.41 × 7,200 = 2,952 alpha           the day's miner emission
S_d   = 0.20 × A_d   (590.4 alpha)           skill pool, when anyone has skill weight; otherwise 0
V_d   = A_d − S_d    (2,361.6 alpha)         volume pool
volume_pay_i = V_d × Φ̂_i / Σ_j Φ̂_j            over eligible miners (§9a)
skill_pay_i  = S_d × w_i / Σ_j w_j             over miners with skill weight (§7a)
pay_i        = volume_pay_i + skill_pay_i
```

Nobody with skill weight: the skill pool joins the volume pool. Nobody with eligible fees but someone with skill
weight: the skill pool takes the whole pool. Nobody with any weight: the day goes to the fallback hotkey (§1). The
volume pool is linear in fees paid, so splitting an account or merging accounts changes nothing in it.

## 6. Pay is not capped

Pay is not capped by fees: a miner who paid one dollar of builder fees on a quiet day can take the whole volume
pool. The economics this creates are in §11. `F̂` (smoothed total fees on the builder leg) is computed and recorded
for information; it does not bound pay.

## 7. Skill factor `g'` (windows 60 and 30 days, smoothed)

Daily returns of the bound address, deposits and withdrawals removed, capped at ±10%:

```
r_d = (E_d − E_{d−1} − flows_d − offmarket_d) / E_{d−1}       r̃_d = max(−0.10, min(0.10, r_d))
```

`flows_d` are deposits, withdrawals, account and vault transfers; `offmarket_d` is the money moved in through
off-market fills that day (§3, item 2). A day has a return only when its account value and the previous day's are
both known; the windows below count known returns, so a day without one (an account value that could not be read,
or a day the exchange's record did not cover) is skipped and the window reaches one day further back.

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

No drawdown anywhere. The scale is set so that a strong trader scores 1.

## 7a. The skill pool

```
s_i = max(0, (g'_i − 0.25) / 0.75)                         skill above the luck floor
v_i = min(1, V_i,30d / $1,000,000)                         participation: credited Kokoah notional, 30 days
w_i = s_i × v_i × (1.1 if a verified agent account, §16)    skill weight
```

`V_i,30d` is the credited notional (eligible trades only, §9a) of the last 30 days, today included. A miner takes
part only when all of these hold: eligible today (§9a); the activity gates pass (§9); no mirror penalty (§3); and
the **entry test**: the probabilistic Sharpe ratio of the last 90 capped daily returns against the benchmark daily
Sharpe ratio of 0.03 is at least 0.95 (fewer than 90 known returns recorded: no entry). Then:

- **Copy rule.** Bound accounts whose daily returns correlate above 0.9 over at least 30 common days form a group
  (joined transitively), and only the group's leader keeps its weight; the others get none. The leader is the member
  whose trades come first. Each member's orders of the day (the pieces filled in one millisecond are one order) are
  matched with the other member's most recent earlier order on the same coin and side, from that day or the 3 days
  before; a match counts the smaller of the two orders' notional for the earlier member, in full when the later one
  followed within 10 minutes and less the longer it waited (10 minutes over the wait), each order's notional used
  once. Small orders placed just before weigh no more than their size, and a copy made hours later still counts, a
  little. Each member's lead over each other is smoothed over the days like the credit (30% today), and the member
  that led the most notional leads, when its lead is at least $1,000 and 1.5 times the next member's (a copier
  trades after what it copies). Without such a lead, the member registered on the subnet first (the same on every
  validator) leads, then the hotkey order. The same strategy in many accounts earns one weight, copying another
  miner earns nothing, and splitting one account above the volume cap into several gains nothing.
- **Why the entry test.** It admits only records whose edge is statistically real over 90 days, so luck does not
  pay; proving an edge from daily returns takes months by design.
- **History.** The exchange keeps day-by-day account values for about a month (the `perpMonth` window); further
  back its records are one to seven days apart. The backfill therefore covers about 30 days, and the rest of the
  90 days is the subnet's own daily record.

## 8. Stake

Stakes are sampled and recorded for information; they do not affect pay.

## 9. Activity gates (for the skill pool and the leaderboard)

Over the last 30 calendar days, today included: at least 10 completed round trips, at least 7 days with a
builder-code fill, mean account value at least $500 (over the days whose end-of-day value is the exchange's
recorded one). A miner failing a gate is not in the skill pool that day (the volume pool still pays its fees).

## 9a. Eligibility: the equity floor and the qualifying trades

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
pay_i,d = 0                                        the eligible miners share the pool without it
```

Volume, fills, account value and the skill smoothing are recorded as on any day. Credit from before the
ineligible days stays in `Φ̂` (decaying) because it was earned while eligible; credit is never added for
ineligible days, and nothing is paid for them later.

**Identity.** Credit (`Φ̂`, `F̂`) and qualification belong to one identity: one hotkey, one bound address, one
registration. They start from zero when the bound address changes (migration), when the hotkey is swapped on
chain, when the hotkey registers again after leaving, and naturally for a new hotkey. What a fresh identity must
not escape is kept: the pay history, the skill record and any mirror penalty (which follows the address and the
addresses its owner controls, §3).

An ineligible miner takes no part in either pool that day. Every account needs its own $10,000 of capital, its own
registration and its own qualification.

## 10. Revenue and buyback

```
R_d = Σ_i Σ_f b_f        builder-fee revenue of the day (USD), from bound and other Kokoah traders alike
```

The owner buys back and burns alpha with the collected fees on a cadence of their choosing, each time as one
`add_stake_burn` transaction. The alpha paid is not bounded by the fees collected (§6).

## 11. The volume pool

The volume pool pays each eligible miner in proportion to its smoothed builder fees, so every fee dollar is worth the
same to every miner, and splitting or merging accounts changes nothing (the pool is linear in fees). Volume between
two bound accounts and self-matches inside a cluster never count (§3); the skill pool is reserved for a proven edge
(§7a). Every builder fee paid funds the buyback (§10).

## 12. Parameters

| Parameter | Value |
|---|---|
| builder fee | 0.025% per fill |
| maker credit | 1.0 (the builder fee is charged on maker fills) |
| smoothing `α` | 0.3, for credit, total fees and skill |
| pools | 80% volume (eligible smoothed builder fees), 20% skill (skill weight); an empty skill pool joins the volume pool; no caps; nothing burned unless no miner has weight |
| skill score | confidence 60 days (PSR, benchmark Sharpe 0.03, ≥ 10 days), quality 30 days (gain-to-pain, half-life 10 days), blow-ups 60 days; `g'` full at g = 0.65; returns capped ±10% a day |
| skill pool | luck floor 0.25; volume factor full at $1M of credited Kokoah notional over 30 days; entry test PSR ≥ 0.95 over 90 days; copy rule: correlation > 0.9 over ≥ 30 common days, the leader by orders that came first (3 days back, a lead of ≥ $1,000 and 1.5× the next) else the earliest registration; verified agents ×1.1 |
| activity gates | ≥ 10 round trips, ≥ 7 active days, mean equity ≥ $500 (30 days), for the skill pool |
| eligibility | the day's average account value ≥ $10,000 (recorded history, ≥ 4 points in the day); 3 qualifying trades with ≥ $1,000 of eligible notional, the first two uncredited |
| mirror rules | fills against bound counterparties never count (and report no one); a coin with > 25% of its day volume the address's own: the day's profit not counted, a loss counted; position correlation < −0.8 on ≥ 10 days at the day's end, owner-linked pairs only; exclude > 25%, out of the skill pool > 50% |
| mark clip | 0.5% |
| data plane | the address's own fill history, merged with the builder file on the second; history-only fills count when their order is confirmed as the address's own order with Kokoah's builder (explorer); no file 12 h after the day: scored from the histories |
| migration | once per 60 days, signed by old address, new address and hotkey; skill, pay history and penalties carry over, credit and qualification start from zero |
| agents | sealed machines on Google Cloud Confidential Space running an approved harness image; checked daily by each validator (§16) |
| buyback | the collected fees, on the owner's cadence, by `add_stake_burn` |
| owner share | `owner_cut_auto_lock_enabled` on |
| emission split | miner 41%, owner 18% (locked), validators 41% (the chain's default) |

## 13. Worked examples

**Two traders, one with a proven record.** Equal smoothed builder fees today. A has 90 days of strong returns and
passes the entry test with `s × v` = 0.8; B has none. Volume pool: 1,180.8 alpha each. Skill pool: all 590.4 alpha
to A. A is paid 1,771.2 alpha, B 1,180.8; nothing is burned.

**An agent beside a human.** Two traders pass the entry test with the same `s × v`; one trades through a verified
sealed-machine agent. Skill weights 1.1 and 1.0: the agent takes 52.4% of the skill pool, the human 47.6%; the
volume pool is split by fees alone.

**Ten copies.** One strategy run in ten accounts, each with `s × v` = 0.9: the copy rule groups them and only the
leader keeps its 0.9, the same as a single account.

## 14. Account switching and migration

A miner's smoothed credit, skill and pay history belong to the binding. Migration to a new address is allowed
once per 60 days by a message signed by the old address, the new address and the hotkey; skill, pay history and
penalties carry over, while credit and qualification start from zero (§9a). A fresh binding starts from
zero. Clusters (§3) cover linked addresses. The chain keeps
only a hotkey's latest commitment, so a validator enforces the 60-day limit from the bindings it has accepted; a
validator that starts fresh accepts the current commitment on its signatures alone and converges with the others
after 60 days.

### 14a. Leaving, returning, switching

- **Deregistered or pruned, same hotkey comes back.** Records are kept by hotkey and pause while the hotkey is
  off the subnet (nothing can be paid to it); they resume when it is registered again, as a fresh identity for
  credit and qualification (§9a): the new registration must qualify again.
- **Deregistered, a new hotkey binds the same address.** A hotkey that left the subnet releases its address; the
  new hotkey starts with no credit tail, no qualification and no smoothed skill (`g = 0`). The unpaid credit tail of
  the old hotkey is lost to the trader: with 70/30 smoothing, leaving right after a trading day forfeits about 70% of
  that day's credit, 49% of the day before, and so on. The activity gates, the entry test and the copy rule read the
  address's own record, which stays with the address, as do its penalties.
- **Unlinking.** The linked address ends its link with one signature, without the hotkey:
  `kokoah-unlink-1 netuid=<netuid> hotkey=<ss58> address=<0x lowercase> time=<unix ms>` (EIP-191), sent to the
  subnet's service, which accepts it only within 10 minutes of its time and publishes it (`GET /unlinks`). Every
  validator checks each signature, keeps every unlink it has seen and applies it from the first UTC day after its
  time: the link ends (credit and qualification end with the identity, as when leaving), the address is free for
  another hotkey and the hotkey for another address, and this hotkey and this address are never accepted together
  again (the old link signature cannot be republished). Penalties stay with the address.
- **A mirror report follows the address**, and the addresses its owner controls (hotkeys on the same coldkey, its
  sub-accounts), not the hotkey; a transfer someone else sends never carries it: binding a penalized address to a new
  hotkey keeps the penalty.
- **Hotkey swap on chain** (same UID, coldkey and registration block as on the previous scored day, new hotkey):
  the pay history, skill and penalties follow the new hotkey, the credit and qualification do not (the swap is
  recorded in `hotkey_swaps` and applied when the day is scored, so a re-score applies it again); the binding must
  be published again because the signed line names the hotkey. A UID registered again, even by the same coldkey,
  is a new registration, not a swap.
- **Trading before joining.** Fills of a day scored before the binding was accepted are recorded as unbound and
  never pay. The bindings are read when a day is scored and the day's file is scored as a whole, so a binding
  accepted before a day is scored covers that whole day, including its earlier fills (they also count toward
  qualification). The account's own
  history does count for skill: on the first day an address is scored, the exchange's portfolio history (perps
  window) backfills daily account values, so a trader with a real record is recognized within days instead of
  starting from nothing.

### 14b. Robustness rules

- A failed read of the account's recorded history (§9a), the fill history, the explorer, a linked account, the minute
  candles or the unlinks fails the day's scoring, which is retried within the hour, until the deadline: the file
  deadline, and never sooner than `retry_grace_hours` (3) after this validator's first attempt at the day. The days of
  a backlog (days already over when the day before them was first attempted, as after an outage) share the first one's
  window, so a validator back from an outage retries for 3 hours in all, and a validator that was away from a day it
  had started (for more than twice its retry pace) starts that day's window again once on its return; after that a
  failed read is final for that day on that validator. Past the deadline the day is scored and only what depends on
  the failed read is lost: without the recorded history the day is measured from points an earlier read stored if they
  reach past the day's end, otherwise the account's average is unknown and it is not eligible that day; without the
  fill history its return is unknown; fills that needed a failed read go uncounted (§3). The minute candles of a coin
  the exchange does not list are settled at once the same way, so no trader can hold everyone's day with it, and so
  are those of a day older than the exchange keeps (§3). The other
  reads (account value, flows, positions, fee rates, stake) fall back. Every failure is recorded.
- A re-score of a day starts from the state as it was before that day (state history), never from the already
  advanced state.
- The builder file is fetched again after 06:00 UTC and its hash compared with the one scored; a change is
  recorded and flagged, the scored file stands.
- **Late or missing files.** The exchange writes a day's file about an hour after the day ends, and none when the
  part of the day it covers had no fills. The validator scores the days after its latest scored day in order, waits
  for the file until 12 hours after the day ends (`file_deadline_hours`), and then scores the day from the bound
  addresses' histories alone (recorded as an error, so the health check shows it). A file published later is
  fetched by the re-check and its hash recorded; the scored day stands. `kokoah-subnet validator skip-day` remains
  for a day to be closed by announcement as a day with no fills (an outage of the exchange's history itself).
- **Day zero** is a parameter of the mechanism (`Params.day_zero`), so every validator counts penalty windows from
  the same date.

## 15. Verification

The specification's numerical claims are reproduced by `uv run kokoah-subnet verify`
(`src/kokoah_subnet/mechanism/verify.py`, part of every install) and by the test suite; `scripts/check.sh`, which CI
runs, runs both.

## 16. Sealed-machine agents

A miner can trade by agent: code that places the account's orders with no person able to trade the account.
Agents run on the miner's own Google Cloud project, in a **Confidential Space** machine (Intel TDX or AMD SEV-SNP)
started from Kokoah's published harness image. The harness (`src/kokoah_subnet/agent/`, `deploy/agent/`):

- **Creates the account's key inside the machine** and never exports it. Nobody else can sign for the account, so
  no person can trade it by hand: every action on it comes from the machine. The key lives in memory; before the
  account is used, the machine stores a copy encrypted with a secret only the miner holds (given at start) with
  the owner's service, which accepts it only with the machine's attestation. The service's address is an image
  setting (https, certificate checked), never a start setting, so whoever starts a machine cannot put a relay in
  its place and read what it stores or gets back. **Restore:** a new sealed machine
  started with the same secret gets the copy back after proving with a fresh attestation (an answer to the
  service's challenge) that it is a genuine sealed machine of an approved image; the key stays out of human hands
  and the account stays an agent account. The machine resumes the account only for the master address
  and hotkey the copy was filed for (the service returns them with the copy), so a secret alone cannot restart the
  account with its withdrawals going elsewhere. The key store accepts only tokens that carry exactly the store or
  restore digest; the machine's public `/attestation` always signs two nonces, so no answer to anyone's challenge can
  stand in for one. **Release:** the miner can take the copy themselves with a request
  signed by the machine's master address (`agent release`); the service records it publicly (`GET /key-store/released`),
  and from then on the account never earns the agent bonus again, whatever its machine says. The operator cannot
  read the copy (it lacks the secret); the miner cannot get it without a public release. **The secret** is 32 bytes
  made by `kokoah-subnet agent secret`: random, or from a password the tool accepts only when strong (at least 16
  characters, an estimated 10^18 guesses or more, the miner's own hotkey and address counting against it),
  stretched with scrypt (N = 2^17, r = 8, p = 1) and salted with the master address; the machine refuses a secret
  that is clearly not random. The service adds its own AES-256-GCM layer over every stored copy with a key kept
  out of its backups, so a stolen database or backup opens nothing by itself. The service
  (`https://api.kokoah.com/miners`) takes submissions and stored copies only from hotkeys registered on the subnet;
  restores and releases never depend on the registration. **A copy is filed only with the miner's start
  permission:** the hotkey's signature (made on the miner's computer, `agent authorize`) of the subnet, hotkey,
  master address, submission and time, at most 7 days old, used once, at most 50 kept copies per hotkey; the
  machine's attested store nonce covers the permission, so a copy can be filed under a hotkey only by its owner.
- **Runs the miner's agent code in a sandbox**: a separate user, its own view of the filesystem in which the
  launcher's folder (the attestation socket and the launcher's token) is empty, no new privileges, no core dumps,
  memory and process limits, and a seccomp filter that refuses every network socket except Unix sockets, io_uring and
  other processes' memory. The agent's only way out is the harness's Unix socket, and it cannot ask the machine for
  an attestation token; the harness starts it only after a probe, run exactly as the agent, sees nothing there.
- **Offers the agent a narrow interface**: orders (limit and trigger), cancels, leverage, isolated margin and a
  scheduled cancel; read-only exchange data, account queries answered for this account only. Kokoah's builder code
  is attached to every order and the order id carries Kokoah's agent marker (`0x6b6f0603…`). An order that executes
  now (a limit order, or a trigger order already past its trigger price) is refused when priced more than 5% from
  the mid; a trigger order that has not fired yet rests at any trigger price, and its limit price must be within 5%
  of its trigger price. Not offered: modify, TWAP, trailing stops, withdrawals, transfers, approvals.
- **Withdraws only to the miner's own address**, and only on a request signed by it.

**Submission.** The miner packages the code reproducibly (`kokoah-subnet agent package`, an `entry.py` and any
packages it imports inside), encrypted by default with a key only the miner holds, and submits it to the owner's
service signed by a registered hotkey (`agent submit`; one submission per hotkey per 12 hours). The machine fetches exactly
that submission, checks its id, decrypts it inside, and computes the **code fingerprint** (sha256 of the zip).

**Binding.** The machine signs the subnet binding line with the account's key; the miner publishes it with an
agent field (`A1`: the code fingerprint and the machine's address) as the hotkey's commitment (`agent bind`).

**Verification, by every validator, every scored day** (`src/kokoah_subnet/validator/agents.py`):

```
challenge  = 32 random bytes, as 64 hex characters (the machine accepts exactly that)
answer     = GET <machine>/attestation?challenge=<64 hex>  {token, address, netuid, hotkey, code_sha256}
binding    = sha256("kokoah-agent-1 netuid=<n> hotkey=<ss58> address=<0x..> code=<sha256>")
agent_i    = answer names this subnet, hotkey, account and the committed fingerprint
             ∧ Google's signature on the token verifies, iss/aud/exp, Confidential Space, production
               (dbgstat disabled-since-boot, STABLE), confidential hardware, secure boot
             ∧ the token's image digest is an approved harness image (src/kokoah_subnet/agent/images.py)
             ∧ the token's nonces include the challenge and the binding digest
             ∧ the account has no other trading key approved on the exchange (extraAgents empty)
             ∧ the account is not on the owner's released list (read every day; unreadable: no agent that day)
```

Only a day on which all of these hold gives the agent bonus (§7a). The validator relies on neither the operator
nor the miner for the machine itself: the token is Google's, the image digest is public, and the challenge is its
own. Only machines attested by Google are accepted.
