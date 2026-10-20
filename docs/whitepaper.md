# Kokoah Subnet

## Verified trading activity as a Bittensor subnet

Whitepaper, draft 0.9, October 2026. Mechanism specification v5.8.

### Abstract

Kokoah Subnet is a proposed Bittensor subnet whose miners are traders. A miner binds a Hyperliquid address to a
subnet hotkey and trades perpetual futures through the Kokoah builder code; validators recompute, from public
Hyperliquid data and chain state, each miner's share of the daily miner emission. The work the subnet rewards is
therefore real, external, and verifiable by anyone: every scored fill is a public record on a public exchange, and
every validator reaches the same result from the same data.

The incentive mechanism shares the miner pool in proportion to the builder fees each trader actually paid, adjusted
by a small quality factor bounded between 0.85 and 1.0, and caps what any miner can receive at a fraction of the
fees that trader spent on the trade. Everything above the caps is burned the same day, in the way the chain
recognizes, so the subnet never draws TAO emission for miner rewards it did not award. The builder fees fund an
on-chain buyback and burn of the subnet token. The design goal is a subnet whose token supply is backed by revenue
from genuine activity, whose rewards cannot be farmed by fake volume at any fee tier, and whose accounting is
auditable end to end.

This document describes the mechanism, the reasoning behind each rule, the economic analysis that supports it, the
verification done so far, and the questions that remain open before a mainnet launch.

---

## 1. Motivation

Bittensor's premise is that token emission should flow to measurable, useful work. Subnets that measure their work
against an external, independent source of truth are the easiest for the network to trust: validators can be
checked, incentives can be audited, and the value produced does not depend on the subnet's own token.

Trading on a public exchange is such a source of truth. Every fill on Hyperliquid is a public record with a price,
a size, a side, a fee, and a counterparty; account values and deposits are public; and a venue-level feature, the
builder code, attributes a fill to the application that routed it and charges a small fee that the venue itself
publishes. A subnet built on this data can pay for an activity that is real, priced by an outside market, and
revenue-generating, without asking the network to trust the operator's measurements.

The hard part is not measurement but incentives. Rewards for trading volume have a long record of failure:
trans-fee mining in 2018 collapsed when the reward exceeded the fee, dYdX's trading rewards were cut repeatedly for
paying more than they earned, and academic estimates put wash trading above 70% of reported volume on unregulated
venues. A Bittensor subnet that paid for volume naively would pay bots to trade against themselves, and the alpha
they sold would exceed anything the subnet's revenue could absorb.

Kokoah Subnet is an attempt to do this carefully. The mechanism below is built around one rule that is checked
rather than hoped for: no strategy without a trading edge can net a profit from the subnet, at any pool size and at
any fee tier of the exchange. Everything else follows from that rule.

## 2. Design goals

1. **Verifiable.** Every input is public: Hyperliquid fills, account values, deposits, and Bittensor chain state.
   Validators recompute independently; the operator has no privileged measurement.
2. **Proportional.** Rewards are linear in the fees a trader actually paid through the builder code. Twice the
   activity, about twice the reward. Splitting or merging accounts changes nothing.
3. **Bounded modifiers.** Skill and holding adjust a trader's rate only within 0.85 to 1.0. Two traders with the
   same activity and fee tier are always within 15% of each other, so anyone can participate without fear of a
   score they cannot control.
4. **Fee-backed and farm-proof.** No miner is ever paid more than a fraction of what the trade cost them. Fake
   volume loses money at every fee tier the exchange offers, whatever the size of the pool.
5. **Nothing wasted, nothing hidden.** Emission above the fee-backed caps is burned the same day through the
   owner hotkey, which the chain records and accounts for. Builder fees buy back and burn the subnet token in
   single, verifiable on-chain transactions. All daily scoring artifacts are published.

## 3. Participants and data

**Miners** are traders. A miner registers a hotkey on the subnet, binds one Hyperliquid address to it with a
message signed by both the address and the hotkey, and trades perpetuals through the Kokoah application, so that
every scored fill carries the Kokoah builder code. Miners run no software; the binding and the trading are the
work.

**Validators** run open-source software that, once a day, fetches the public data for every bound address,
applies the exclusion rules, computes each miner's reward, and sets weights every tempo. The computation is
deterministic: every validator with the same inputs produces the same weights, and Yuma consensus has nothing to
reconcile but data availability.

**The operator** (Kokoah) runs the application through which the trades are routed, collects the builder fee the
exchange charges on those trades, publishes the daily fill file's hash on chain, and spends the collected fees on
buying back and burning the subnet token on a published cadence. The operator holds the owner hotkey, through
which unawarded emission is burned.

**Data.** Hyperliquid charges the builder fee on maker and taker fills alike (its documentation is silent on the
point; the builders' own daily files show it). It publishes, for every builder, one file per day listing every fill
routed under that builder's code, with the user address, coin, side, price, size, whether the order crossed the spread (taker), the
counterparty, the closed PnL, and the builder fee. This file is the canonical list of scored fills. Each address's
own public fill history provides the total fee charged by the exchange, and its public account state provides the
daily account value and deposits. Fills without the builder code count for nothing; no other venue is ever read.

**Clusters.** Addresses that belong together are scored as one and never trade against each other for credit: a
master account and its sub-accounts, addresses on the same coldkey, addresses funded by the same bridge depositor
or transferring between each other.

**Migration.** A miner's smoothed records belong to the binding, not the address. Once per 60 days a miner may
move to a new address by a message signed by the old address, the new address, and the hotkey; the records carry
over. A fresh binding starts from zero.

## 4. The incentive mechanism

Notation: `i` a miner, `d` a scoring day (00:00 UTC), `f` a fill of miner `i` carrying the builder code, `n_f`
its notional in USD, `b_f` the builder fee it paid (0.025% of notional, maker or taker), `fee_f` the fill's total fee field (the exchange's fee plus the builder fee; a maker rebate makes the
exchange's part negative). `A` is the day's miner emission (33% of 7,200 alpha, 2,376 alpha). `p_d` is the alpha
price in USD for the day: the mean of the pool price sampled at every tempo boundary of the day, times the day's
mean TAO/USD mark.

### 4.1 Fee credit

Each day, a miner's fee credit is the builder fee paid on its fills. The builder fee is one rate on every order,
0.025% of notional, attached when the order is placed and therefore the same whether the order rests on the book or
crosses it; the exchange charges it on maker and taker fills alike. The credit is smoothed with a daily factor of
0.3:

```
Φ_i,d  = Σ_f  b_f
Φ̂_i,d  = 0.7 × Φ̂_i,d−1 + 0.3 × Φ_i,d
```

A day's fees are worth 30% on the day, 21% the next, 15% the day after. The weights sum to one, so every dollar of
fees is counted exactly once over time; the smoothing only spreads it. A dollar of maker fees is worth exactly a
dollar of taker fees to the subnet's revenue, and the credit treats them the same; the second cap (section 4.4),
not a credit discount, is what keeps the passive leg of a two-account wash from being subsidized.

### 4.2 Quality multiplier

```
M_i,d  = 0.85 + 0.05 × g'_i,d + 0.10 × h'_i,d          g', h' ∈ [0, 1]
```

`g'` is the skill factor and `h'` the holding factor, both defined below. The multiplier is bounded in [0.85, 1]
by construction. Traders with the same activity differ by at most 15% whatever their skill or holding.

### 4.3 Share

```
share_i,d  = A × Φ̂_i,d × M_i,d / Σ_j Φ̂_j,d × M_j,d
```

The pool is shared in proportion to credit times multiplier. Because the share is linear in a quantity that an
account can split (fees paid), and the multiplier is bounded, splitting one account into many or merging many
into one does not change the total received. This is the proportional allocation rule, which Chen, Papadimitriou
and Roughgarden (2019) show to be the unique rule that is budget-balanced, symmetric, sybil-proof and weakly
collusion-proof.

### 4.4 Caps and burn

```
F̂_i,d    = 0.7 × F̂_i,d−1 + 0.3 × Σ_f fee_f                      smoothed total fees on the builder leg
cap_i,d  = M_i,d × min( 2 × Φ̂_i,d ,  0.75 × F̂_i,d ) / p_d        in alpha
pay_i,d  = min( share_i,d , cap_i,d )
burn_d   = A − Σ_i pay_i,d                                       weight to the owner hotkey
```

Two caps apply to every miner. The first says that nobody is paid more than twice the builder fee they paid. The
second says that nobody is paid more than three quarters of everything the trade cost them on the exchange, the
exchange's fee and the builder fee together. For an ordinary account, the first cap binds; for an account with
large volume discounts and staking rebates, the second does. The second cap is what makes the mechanism safe at
every fee tier (section 5.1); the first sets how generous it is to ordinary traders.

Caps are converted from dollars to alpha at the day's average price, never at a spot price, so a price dip at the
scoring moment cannot inflate anyone's alpha.

When the pool is larger than the sum of the caps, which is the normal state of a young subnet, the remainder is
not paid to anyone. It is directed to the owner hotkey, where the chain burns it (section 6.2).

### 4.5 Skill factor

Daily returns of the bound address are computed from account value with deposits and withdrawals removed, and
each day is capped at ±10%:

```
r_d = (E_d − E_d−1 − flows_d) / E_d−1          r̃_d = clip(r_d, −0.10, +0.10)
```

Two statistics are combined. **Confidence** is the probabilistic Sharpe ratio (Bailey and López de Prado 2012,
adjusted for skew and kurtosis) of the last 60 days of capped returns against a benchmark daily Sharpe ratio of
0.03, about 0.5 annualized; it asks whether the edge is statistically real rather than lucky. With fewer than 10
days it is 0.5. **Quality** is the recency-weighted gain-to-pain ratio of the last 30 days (half-life 10 days); it
asks whether recent gains clearly exceed recent losses. It is halved if two or more days in the last 60 lost more
than 10%.

```
confidence = clip((PSR − 0.5) / 0.45, 0, 1)
quality    = clip((GPR − 1) / 1.2, 0, 1)
g_raw      = confidence × quality
g_i,d      = 0.7 × g_i,d−1 + 0.3 × g_raw
g'_i,d     = min(1, g_i,d / 0.65)
```

No drawdown statistic is used anywhere. A single bad day is one capped loss that fades; a repeated pattern of
blow-ups keeps counting. In simulation (6,000 accounts per profile, 60 days), accounts with no edge score 0.05 to
0.10, a martingale-style account 0.08, a steady low-volatility trader 0.30, a trader with a real edge 0.35, a
strong trader 0.56. The skill factor moves pay by at most 5%; it exists to recognize skill in the public ranking
and to nudge, not to dominate.

### 4.6 Holding factor

```
staked_i,d = the day's minimum alpha staked on i's own hotkey by its own coldkey
target_i,d = alpha paid to i in the last 60 days
h'_i,d     = min(1, staked_i,d / max(target_i,d, collateral_min))
```

A miner who keeps what they were paid over the last two months has a factor of 1; one who sold everything has 0;
alpha bought on the market counts. The stake is read as the day's minimum, so staking for one block earns nothing.
The factor moves pay by at most 10%. Whether it reduces selling in practice is something to measure on testnet,
not to assume.

### 4.7 Activity gates

The skill and holding parts of the multiplier, and the public ranking, require over the last 30 days at least 10
completed round trips, at least 7 days with a builder-code fill, and a mean account value of at least $500. A
miner failing a gate is paid at the floor multiplier of 0.85, not excluded.

### 4.8 Exclusions

Before anything is computed, the following are removed from a miner's fills: fills whose counterparty is the same
cluster; fills whose counterparty is another bound address, since the subnet does not pay for volume between its
own miners (the per-builder file names every fill's counterparty, so this is exact and never flags an honest
trader by coincidence); and fills priced more than 0.5% from the mark, which are valued at the mark. Persistent
hedging through the market is caught by position: daily net positions in a coin correlated below −0.8 with another
bound address on at least 10 overlapping days in the last 30. Mirrored notional above 25% of an address's notional
is removed from its credit; above 50%, both addresses lose their quality multiplier for 30 days and are reported.
These rules protect the integrity of the public ranking. The economics alone already make mirrored volume a loss
(section 5.1).

Two abuse signals are published without affecting pay: the share of an address's credit from positions held under
one minute, and the share from fills against bound counterparties.

### 4.9 Parameters

| Parameter | Value |
|---|---|
| Builder fee | 0.025% of notional per fill |
| Smoothing factor | 0.3 per day (credit, total fees, skill) |
| Multiplier | 0.85 + 0.05 × skill + 0.10 × holding |
| First cap | 2 × smoothed fee credit |
| Second cap | 0.75 × smoothed total fees on the builder leg (must stay below 0.864) |
| Cap price | day's mean pool price × day's mean TAO/USD |
| Skill windows | confidence 60 days, quality 30 days (half-life 10), blow-ups 60 days |
| Holding target | alpha paid in the last 60 days, floor at the collateral minimum |
| Gates | 10 round trips, 7 active days, $500 mean equity, over 30 days |
| Mirror rules | fills against bound counterparties never count; correlation < −0.8 on ≥ 10 days; exclude above 25%, zero above 50% |
| Migration | once per 60 days, signed by old address, new address and hotkey |
| Emission split | miner 33%, owner 33% (locked), validators 34% |

Any change to a parameter is announced at least seven days before it takes effect and recorded in a public
changelog.

## 5. Economic analysis

### 5.1 The one law: fake volume loses at every fee tier

Consider any strategy that generates volume without a trading edge: wash trading against oneself on another venue
or in another account, a hedged pair, instant round trips, dust accounts, a farm of small accounts. Let `C` be the
total fees such an account pays on the builder leg per round trip (the exchange's fee plus the builder fee, both
sides) and `H` the cost of the other leg, if any. The subnet pays at most `0.75 × C`, so

```
net  ≤  −0.25 × C − H
```

The smallest `C` on Hyperliquid is a maker at the deepest rebate tier, 0.3 bps a side, trading through a 2.5 bps
builder fee: `C = 2 × (2.5 − 0.3) = 4.4` bps per round trip. The most favorable other leg is the same rebate,
`H = −0.6`. Hence `net ≤ −1.1 + 0.6 = −0.5` bps per round trip. Every zero-edge strategy loses, at every pool size,
at every fee tier, whatever the first cap is set to. The inequality holds for any second-cap ratio below 0.864; the
chosen 0.75 leaves margin.

| Strategy (bps per round trip, builder fee 2.5 a side) | Exchange fee, builder leg | Builder fee | Other leg | Paid at most | Net |
|---|---|---|---|---|---|
| Wash: taker through the builder vs own maker elsewhere, base tier | 9 | 5 | 3 | 10 | −7 |
| Two bound accounts, taker vs maker, both through the builder | 12 | 10 | 0 | 16, and mirrored → 0 | −6 or worse |
| Hedge, taker through the builder, hidden maker leg elsewhere | 9 | 5 | 3 | 10 | −7 |
| Hedge, taker at the deepest volume and staking discounts (1.44 a side), hidden leg at the best rebate | 2.88 | 5 | −0.6 | 5.91 | −1.37 |
| Market maker at the deepest rebate, maker both legs, one through the builder | −0.6 | 5 | −0.6 | 3.3 | −0.5 |
| Instant round trips in one account, base tier | 9 | 5 | 0 | 10 | −4 |
| Farm of small accounts | 9 | 5 | 0 | 10 | −4 each |

The tightest case, the rebated market maker, would need a hidden rebate of 0.55 bps a side to break even, almost
twice the best rebate the exchange offers. A search over 105 combinations of volume tier, staking discount and
maker or taker on the builder leg found no positive case.

What the law does not forbid is honest high-frequency activity. Scalpers and market makers earn credit on every
fill like everyone else; they are paid less than their costs, like everyone else, and whether that trade-off is
worth it is their decision, exactly as a fee rebate program on any exchange.

### 5.2 Sybil neutrality

Shares and caps are both linear in the splittable quantity, fees paid, and the multiplier is bounded. One account
and the same fills spread over two or five accounts receive the same total (verified numerically in both pool
regimes). The things that are not linear all work against splitting: each registration costs a burn and locks
collateral, each account must pass the activity gates separately, and the holding target is per hotkey.

### 5.3 Cumulative backing

Each day's pay is at most `2 × Φ̂` in dollars at that day's price, and the smoothing weights sum to one, so up to any
day the alpha paid out, valued when it was paid, is worth at most twice the builder fees collected so far. If miners
keep half of what they are paid, which the holding factor rewards, the fees cover everything ever paid; if the
first cap were set to 1, they would cover it unconditionally. On 200 random paths of fees, prices and miner
populations the worst ratio of paid to collected was 1.36.

The operator spends the collected fees on buying back and burning alpha on a published cadence (every two to three
weeks). A coverage figure, cumulative buyback over cumulative alpha sold by miner hotkeys, is published for
information.

### 5.4 Price manipulation

The caps convert dollars to alpha at the day's mean pool price over 20 tempo samples. A 30% dip engineered at the
scoring block would raise the alpha cap by 43% at a spot price and by 1.5% at the mean; holding the price down for a
whole day against other buyers is not a cheap manipulation.

### 5.5 Fee tiers

In a busy subnet, where shares bind, equal activity means equal pay regardless of fee tier. In a young subnet,
where caps bind, pay follows what the account actually spent: an account at the deepest discounts receives about
59% of what a base-tier account with the same volume receives, because it paid about 56% as much. This is a
deliberate property of the second cap, and it is stated here so nobody is surprised by it.

### 5.6 What the mechanism does not do

It does not pay for profit. Dollar profit is published in the ranking but never enters the share; paying for it
would reward luck and leverage, and it is not what the exchange charges for. It does not promise returns on the
token, to miners or to anyone. It does not require miners to run infrastructure or expose keys; the subnet reads
public data only and never trades on anyone's behalf.

## 6. Relationship with the Bittensor chain

### 6.1 Emission split and the owner's share

Emission is split 33% to miners, 33% to the owner, 34% to validators and their stakers. The owner's share is
locked into conviction by enabling the chain's `owner_cut_auto_lock_enabled` hyperparameter, so the operator
never sells into its own buyback. Validator emission and outside demand for the token are outside this
mechanism's scope.

### 6.2 Unawarded emission is burned, and the chain is told

When the sum of caps is below the pool, the difference is directed to the owner hotkey. The chain burns incentive
that targets an owner hotkey, records the burned share as `MinerBurned`, and scales the subnet's TAO emission
share by one minus that fraction in the next tempo. The design accepts this scaling in full. A subnet that did not
award 70% of its miner emission should not draw TAO for it, and under this rule it does not; alpha emission to
participants is unchanged, so miners and validators are unaffected. On a local chain, with 85% of the miner pool
pointed at the owner hotkey, that hotkey's stake grew only by the owner's own cut, the miner and the validator
received exactly their shares, and alpha emission continued at its full rate; the withheld 85% landed nowhere. There are ways to burn alpha that the chain
would not see (routing weight to a hotkey outside the owner's coldkey and burning it by hand with `burn_alpha`);
the design does not use them, because the rule they would dodge is the right rule. As fees grow, the burned
fraction falls toward zero on its own.

### 6.3 Buyback and burn, one transaction

The chain provides `add_stake_burn`, which buys alpha with TAO and burns it in a single call, emitting a
`TokensBurned` event. Every buyback is therefore one verifiable on-chain event, and the published coverage figure
can be checked against the chain by anyone.

### 6.4 Validation cadence and consensus

Scores are computed once a day from the previous UTC day's data. Validators set the same weights every tempo (72
minutes) until the next scoring run. Because the computation is deterministic over public inputs, the chain's
Yuma consensus converges on identical weights; weight copying harms nothing, and timelock-encrypted weights are
available if ever needed. The daily fill file's hash, committed on chain by the operator, lets validators detect a
divergent input immediately.

### 6.5 Registration and collateral

Miners register by burning TAO; a minimum alpha collateral set by the owner is locked on the hotkey. The collateral
is the holding factor's floor and is stranded if a hotkey is reported for mirroring. Validators score by hotkey
root, so a hotkey swap keeps the binding.

## 7. Transparency

The validator software is open source. Each day the following are published per miner: fee credit, total fees on
the builder leg, skill and holding factors, multiplier, share, cap, pay, and the day's burn; plus the abuse signals,
the coverage figure, and the public ranking (skill with its parts, managed capital, volume, dollar profit, execution
quality, holding). Parameter changes are announced at least seven days in advance. Reported addresses and the
reason are published.

## 8. Verification and limitations

**Done.** An adversarial replay of 60 days through the real scoring code (honest swing trader, scalper and market
maker beside a wash trader, a maximal-discount hedge, a rebated market maker, instant round trips and a pair
crossing each other): every zero-edge strategy ends negative and the honest traders receive their proportional
share; the replay also found and removed a timing heuristic that flagged honest traders by coincidence. The one law
searched over 105 fee cases and proven in closed form; cumulative backing on 200 random
paths; the 15% rule in both pool regimes; sybil neutrality for one, two and five accounts; the cap price under a
30% spot dip; the skill factor calibrated on simulated track records (luck, lottery, martingale, grinder, real edge,
strong edge, fading edge, one bad day); on a local chain, that incentive aimed at the owner hotkey is withheld
while alpha emission to everyone else is unchanged. The verification script, the simulations and the chain
measurement are in the repository.

**Before mainnet.**

1. Burn cost, TAO side, on testnet: measure `MinerBurned` and the TAO-share scaling with 85%, 50% and 0% of the
   pool burned. A fresh local subnet receives no TAO injection, so this cannot be seen locally.
2. The same replay on recorded public Hyperliquid histories of real scalpers, market makers and swing traders, and
   with one trader on several wallets, volume timed to price, and coordinated groups. Pass criterion unchanged: no
   zero-edge strategy nets positive at any pool size, and genuine high-volume traders receive their proportional
   share.
3. Holding factor: measure selling by miners at factor 1 against factor 0 on testnet.
4. Data plane: confirm the per-builder daily file is fetchable by anyone for the live builder address, and that
   total-fee matching against public fill histories is exact.

**Open assumptions.** The public per-address fill API keeps 10,000 fills, so the per-builder file is
essential for high-frequency accounts. The TAO/USD mark is the node's daily mean, with an exchange median as
fallback. Whether, and when, root enables emission for a new subnet is the network's decision, not the
operator's. Paying tokens for trading activity is subject to legal review in the operator's jurisdictions.

## 9. Roadmap

Local chain: scoring library with tests, validator loop, burn-cost measurement. Testnet: wallets, registration, a
validator and miners running for at least a month, the holding measurement, public dashboards. Mainnet: only after
the verification list above is complete and published, and with the parameters that the testnet period supports.

## 10. References

- Bailey, D. H., and M. López de Prado (2012). The Sharpe ratio efficient frontier. Journal of Risk 15(2).
- Chen, Y., C. Papadimitriou, and T. Roughgarden (2019). An axiomatic approach to block rewards. arXiv:1909.10645.
- Cong, L. W., X. Li, K. Tang, and Y. Yang (2023). Crypto wash trading. Management Science; NBER w30783.
- Victor, F., and A. M. Weintraud (2021). Detecting and quantifying wash trading on decentralized cryptocurrency
  exchanges. arXiv:2102.07001.
- Hyperliquid documentation: fees, builder codes, info endpoint.
  https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees ·
  https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes ·
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Bittensor (Rao Foundation) subtensor: emissions, emission weights, `run_coinbase.rs`, `subnet_emissions.rs`.
  https://github.com/RaoFoundation/subtensor
- dYdX trading rewards. https://docs.dydx.xyz/concepts/trading/rewards/trading-rewards

---

### Appendix A. Worked example

A young subnet: 2,376 alpha a day at $5.33 (a pool worth $12,667). Fees today $2,000 on $8M of volume. Two miners,
equal volume of $4M, all taker at the base tier, smoothed credit $1,000 each, total fees on the builder leg $2,800
each. Miner A has strong skill and holds everything (multiplier 1.0); miner B has no skill and sold everything
(0.85). Shares would be 1,284 and 1,092 alpha. Caps: min(2 × 1,000, 0.75 × 2,800) = $2,000, times the multiplier,
at $5.33: 375 and 319 alpha. Pay: 375 ($2,000) and 319 ($1,700). Burned: 1,682 alpha, 71% of the pool, so the
subnet's TAO share next tempo is scaled by 0.29. Same volume, pay within 15%; total pay $3,700 against $2,000 of
fees, covered if the two keep at least 46% of it.

A busy subnet: fees $87,700 a day on $350M of volume. The sum of caps (about 16,450 alpha) exceeds the pool;
nothing is burned; the pool works as a volume rebate returning about 14% of fees, with shares within 15% for equal
volume.

### Appendix B. Daily procedure of a validator

1. Fetch the previous UTC day's per-builder fill file; verify its hash against the operator's on-chain commitment.
2. For each bound address: fetch public fills for the total fee field, account value and ledger for returns and
   flows, and chain state for stake; apply cluster, mark and mirror exclusions.
3. Update smoothed credit, smoothed total fees and skill; compute holding, gates, multiplier.
4. Compute shares, caps in alpha at the day's mean price, pay, and the burn remainder.
5. Publish the per-miner artifacts; set weights (miners by pay, the remainder to the owner hotkey) every tempo until
   the next run.
