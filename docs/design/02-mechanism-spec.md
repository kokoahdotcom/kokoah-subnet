# Kokoah subnet incentive mechanism: complete specification (v3.1, 2026-10-20)

Self-contained. Every symbol is defined here; nothing else is needed to understand or re-implement the mechanism or
the simulator (`sim/index.html`).

## 0. Purpose

A Bittensor subnet whose miners are traders. A miner binds one Hyperliquid address to a subnet hotkey and trades
perpetuals through the Kokoah builder code. Validators compute, from public Hyperliquid data, how much of the daily
miner emission each miner receives. Kokoah collects the builder fee on every such trade and uses it to buy back the
subnet's token (alpha) and burn it. The subnet's product is a verified ranking of traders.

## 1. Chain facts used

- Blocks: 12 s. Tempo: 360 blocks = 72 min. Validators set weights every tempo; scores are recomputed once a day.
- Emission: the subnet mints `E = 7,200` alpha per day (1 alpha per block), split into three slots:
  `miner_slot = 0.33`, `owner_slot = 0.33` (auto-locked, never sold), `validator_slot = 0.34`.
  Daily miner pool: `A = E × miner_slot = 2,376` alpha. All of `A` is paid out every day.
- Alpha pool: a constant-product market. `price (TAO per alpha) = tao_reserve / alpha_reserve`. Selling `x` alpha
  into the pool returns `tao_out = tao_reserve × x / (alpha_reserve + x)`; buying with `y` TAO returns
  `alpha_out = alpha_reserve × y / (tao_reserve + y)`.
- Registration: burn, plus a minimum collateral in alpha locked on the hotkey (owner-set).

## 2. Notation

| Symbol | Meaning | Unit |
|---|---|---|
| `i` | a miner (one bound Hyperliquid address plus its sub-accounts) | |
| `W30`, `W90` | trailing 30-day and 90-day windows ending at the scoring time (00:00 UTC) | |
| `f` | a fill (one execution) of miner `i` carrying the Kokoah builder code | |
| `n_f` | notional of fill `f` = size × price | USD |
| `t_f` | 1 if the fill was a taker fill (`crossed = true`), 0 if maker | |
| `b_f` | builder fee paid on fill `f` = `n_f × fee` | USD |
| `fee` | builder fee rate per fill | 0.00025 (= 0.025% = 2.5 bps) |
| `E_d` | account value of the whole address at the end of day `d`, including unrealized PnL | USD |
| `flows_d` | net deposits into the address during day `d` (deposits − withdrawals − transfers out) | USD |
| `mark(f)` | Hyperliquid mark price of the coin at the time of fill `f` | |
| `λ` | dollar-profit weight | 50 |
| `c_m` | maker fee credit | 0.4 |
| `m_min`, `g_full` | floor of the rate multiplier and the skill score that reaches the top of the range | 0.85, 0.65 |

## 3. Inputs and exclusions

Inputs per address per day: all fills (with `crossed`, `fee`, `builderFee`, `closedPnl`, size, price, coin, time),
daily account value, deposits and withdrawals, funding payments, per-minute mark prices, the master address's list
of sub-accounts, the on-chain binding commitment. Builder attribution comes from node data (the order action carries
the builder address); the public fill record carries `builderFee` but not the builder address, and
`userFillsByTime` keeps only the last 10,000 fills per address.

Before anything is computed, remove:

1. fills without the Kokoah builder code (they count only in the coverage ratio, §6.5);
2. fills whose counterparty is the same address, another address of the same miner coldkey, or a sub-account of the
   same master (self-trade);
3. mirrored fills between two bound addresses: an opposite-side fill in the same coin within 60 s and within 20% of
   the size. If mirrored notional exceeds 25% of an address's notional in `W30`, the mirrored fills are removed; if
   it exceeds 50%, both addresses get `g = 0` for the window and are reported.
4. price clipping: if `|price_f − mark(f)| / mark(f) > 0.005`, the fill is valued at `mark(f)` for notional and
   profit (neutralizes off-market self-trades that transfer profit between accounts).

## 4. Fee credit

```
Φ_i = Σ_{f ∈ W30}  b_f × ( t_f × 1.0  +  (1 − t_f) × c_m )
    = fee × ( taker_notional_i + 0.4 × maker_notional_i )
```

Taker fills earn full credit, maker fills 40%. Reason: to earn taker credit an attacker must pay Hyperliquid's
taker fee (4.5 bps a side), which exceeds anything the subnet returns; a top-tier market maker's maker cost can be a
rebate, so maker credit stays low (§11).

## 5. Dollars made

```
P_i = max( 0 ,  Σ_{d ∈ W30} ( E_d − E_{d−1} − flows_d ) )     dollar profit of the whole address, 0 if negative
```

Equity-based (includes unrealized PnL), net of fees and funding (inside `E_d`), deposits and withdrawals removed.

## 6. Skill score `g_i ∈ [0, 1]` = confidence × quality × coverage

History: all days since the miner's binding (or since migration, §16), at most the last 180. No drawdown is used
anywhere.

### 6.1 Daily returns, capped

```
r_d  = ( E_d − E_{d−1} − flows_d ) / E_{d−1}
r̃_d  = max( −0.10 , min( 0.10 , r_d ) )                           capped return
n    = number of days in the history (≤ 180)
```

### 6.2 Confidence: probabilistic Sharpe ratio (Bailey and López de Prado, 2012)

```
μ  = mean(r̃),  σ = population std(r̃),  SR = μ / σ
γ3 = mean( ((r̃ − μ)/σ)^3 ),  γ4 = mean( ((r̃ − μ)/σ)^4 )
z  = ( SR − SR* ) × sqrt(n − 1) / sqrt( 1 − γ3 × SR + (γ4 − 1)/4 × SR² )       SR* = 0.03 (daily hurdle)
PSR = Φ_N(z)                                                                    standard normal CDF; PSR = 0.5 if n < 10 or σ = 0
confidence_i = clip( (PSR − 0.5) / 0.45 , 0 , 1 )
```

### 6.3 Quality: recency-weighted gain-to-pain

```
w_d     = 0.5 ^ ( age_d / 45 )                                   age_d in days, newest day = 1
GPR_ew  = Σ_d w_d × max(r̃_d, 0)  /  Σ_d w_d × max(−r̃_d, 0)       (10 if there are no losses and some gains)
quality_i = clip( (GPR_ew − 1) / 1.2 , 0 , 1 )
quality_i ×= 0.5   if  #{ d in the last 90 days : r_d < −0.10 } ≥ 2
```

### 6.4 Coverage and activity gates

```
coverage_i = min( 1 ,  C_i / 0.9 )        C_i = builder-code notional / all notional of the address in W30
g_i = confidence_i × quality_i × coverage_i
g_i = 0   if round trips in W30 < 10, or active days in W30 < 7, or mean daily equity in W30 < $500
```

Calibration (6,000 simulated accounts per profile, `scripts/sim_skill.py`), mean `g` after 90 days: zero-edge
accounts (0% drift, 2% to 8% daily volatility) 0.03 to 0.07; martingale (92% of days +0.5%, 8% of days −7%) 0.07;
low-volatility grinder (+0.05%, 0.4%) 0.30; real edge (+0.3%, 2%) 0.38; strong edge (+0.6%, 2.5%) 0.65; real edge
with one −20% day 0.20 (0.32 at 180 days). Pass rates at 90 days: luck 37 to 39%, real edge 86%, strong 97%.

## 7. Holding multiplier

```
staked_i = alpha staked on i's own hotkey by i's own coldkey (kept rewards + collateral + bought alpha)
target_i = max( alpha received by i in W90 ,  30 × A / N_active )          N_active = miners with Φ > 0 in W30
h_i      = 0.5 + 0.5 × min( 1 , staked_i / target_i )
```

Unstaking or moving alpha to another coldkey lowers `staked_i` at once. A new hotkey has `staked = collateral`.

## 8. Pool split

```
M_i      = m_min + ( 1 − m_min ) × min( 1 , g_i / g_full ) × h_i          m_min = 0.85, g_full = 0.65
score_i  = Φ_i × M_i  +  fee × λ × P_i × g_i × h_i                          λ = 50
w_i      = score_i / Σ_j score_j
```

`M_i ∈ [0.85, 1.0]`: a miner with no skill or nothing held earns 85% of the rate of a strong, persistent holder.

`Σ_i w_i = 1`. Validators submit `w_i` as on-chain weights every tempo; the chain pays `alpha_i = w_i × A` per day.
If `Σ_j score_j = 0` (no fees at all), no weights are set and the chain's fallback applies.

Properties: `Φ_i` is linear in fees, `P_i` linear in profit, `g_i` and `h_i` bounded multipliers unchanged by
splitting an account; `w` is homogeneous of degree one in the splittable quantities (sybil- and collusion-neutral,
Chen, Papadimitriou, Roughgarden 2019). Skill and holding move at most 15% of a miner's rate; the fee level and the
taker/maker credit are what keep fake volume out (§14).

## 9. Daily payment

```
alpha_i   = w_i × A                                     tokens per day
usd_i     = alpha_i × price_tao × tao_usd               value at the day's token price and TAO price
```

## 10. Economics

### 10.1 Revenue, buyback, selling

```
R        = Σ_i Σ_{f ∈ day} b_f = fee × daily volume      builder-fee revenue per day (USD)
buyback  = β × R                                         β = 1.0: 100% of fees buy alpha, daily, bought alpha burned
sold     = Σ_i s_i × alpha_i  +  v × E × validator_slot  alpha sold per day; s_i = share miner i sells, v = share
                                                         validators and stakers sell (default 0.2)
sell_usd = sold × price_tao × tao_usd                    sell pressure (USD, at the current price)
```

### 10.2 Requirement and the price the buyback holds

```
requirement:   β × R  +  D  >  sold × price_tao × tao_usd       D = external staker net buying (USD/day)

price*  =  ( β × R + D ) / ( sold × tao_usd )                     in TAO per alpha
```

`price*` is where the token settles with no other demand: above it, daily selling exceeds daily buying and the price
falls toward it; below it, the buyback lifts the price. The subnet never withholds alpha; the requirement is met by
the fee level (`fee`), by holding (`s_i`, the collateral, vesting) and by the emission split (`miner_slot` at its
33% minimum, owner cut locked).

### 10.3 Day-by-day price model (the simulator)

Start: `tao_reserve = depth` (TAO), `alpha_reserve = depth / price_0`. Each day, in order:

1. optional TAO emission to the pool: `tao_reserve += tao_in`, `alpha_reserve += tao_in / price`;
2. sales: `x = sold`; `tao_reserve −= tao_reserve × x / (alpha_reserve + x)`; `alpha_reserve += x`;
3. buyback plus staker buying: `y = β × R / tao_usd + D / tao_usd` (TAO); `alpha_reserve −= alpha_reserve × y / (tao_reserve + y)`;
   `tao_reserve += y`; the alpha removed is burned;
4. record `price = tao_reserve / alpha_reserve`.

Weights, `R` and `sold` are held constant across days in the simulator (miner inputs are per day).

## 11. Cost of trading and cheat accounting

Per miner per day:

```
hl_i      = Σ_f n_f × ( t_f × 4.5 bps + (1 − t_f) × 1.5 bps )     Hyperliquid fees (base tier; top-tier maker rebate −0.3 bps)
hidden_i  = V_i × hidden_bps_i                                     fees on the hedge leg at another venue (0 for honest)
net_i     = hl_i + b_i + hidden_i − usd_i                          net cost of trading; b_i = Σ_f b_f
```

For an honest trader `net_i` is compared with `hl_i` (what trading on Hyperliquid directly costs): below it, Kokoah
is cheaper; below zero, the trader is paid to trade. For a cheat (no real market bet) `net_i < 0` means the cheat
profits.

Per round trip of notional, in bps, with `fee = 2.5 bps` a side (`F = 5` per round trip): a taker-based wash or
hedge pays Hyperliquid 9 and Kokoah 5 on our leg (36% of its spend is our revenue; 29% counting its hidden leg's
fees); a maker-based one 3 and 5 (62%; 45%); a top-tier market maker −1.2 and 5 (132%; 156%). Cost per unit of fee
credit: taker hedge with a rebate on the hidden leg 13.4 bps per 1.0 credit; maker-based top-tier market maker 3.8
bps per 0.4 credit = 9.5 per credit (the cheapest); normal maker hedge 11 per 0.4 = 27.5. A zero-edge account
earns the fee-credit rate times `M ≈ 0.85` to `0.90` (its `g` is 0 for a wash bot and about 0.07 for a hedge leg),
so it must out-earn a rate that is itself bounded by the pool. In the young-subnet phase the pool per dollar of fees
is high and zero-edge traders enter until the pay per unit of credit falls to about 9.5 bps per round trip (190% of
the builder fee); honest traders are paid the same rate times 0.85 to 1.0, and that phase is paid by the emission,
not by Kokoah.

## 12. Parameters (defaults)

| Parameter | Value |
|---|---|
| builder fee `fee` | 0.025% per fill (2.5 bps a side), the app's target; Hyperliquid maximum 0.1% |
| Hyperliquid fees | taker 4.5 bps, maker 1.5 bps a side (base tier); maker rebates −0.1 to −0.3 bps above 0.5–3% of 14-day maker volume |
| maker fee credit `c_m` | 0.4 |
| pool rule | `score = Φ × M + fee λ P g h`, `M = m_min + (1 − m_min) × min(1, g/g_full) × h`, proportional |
| `m_min`, `g_full` | 0.85, 0.65 |
| `λ` | 50 |
| return cap | ±10% a day |
| confidence | PSR of capped returns, hurdle 0.03 a day, ≥ 10 days, mapped (PSR − 0.5)/0.45 |
| quality | recency-weighted gain-to-pain, half-life 45 days, mapped (GPR − 1)/1.2; ×0.5 if ≥ 2 days below −10% in 90 days |
| activity gates | ≥ 10 round trips; ≥ 7 active days; mean equity ≥ $500 (all in W30) |
| coverage target | 0.9 |
| holding multiplier | `0.5 + 0.5 × min(1, staked / target)`, target = max(90-day receipts, 30 × A / N_active) |
| migration | once per 90 days, message signed by the old address, the new address and the hotkey; records and holding carry over |
| mark clip | 0.5% |
| mirror thresholds | 60 s, 20% size; exclude > 25%; zero both > 50% |
| emission split | miner 0.33, owner 0.33 (auto-locked), validators 0.34 |
| buyback `β` | 1.0, daily, bought alpha burned |
| collateral | owner-set minimum per miner (start ≈ $500 of alpha) |
| scoring | daily 00:00 UTC; weights every tempo (72 min) |

## 13. Worked example (one day)

Pool `A = 2,376` alpha; token price 0.02 TAO; TAO $266.54; one alpha = $5.33; pool value $12,667.
`fee = 0.00025`, `c_m = 0.4`, `λ = 50`, `m_min = 0.85`, `g_full = 0.65`.

Three miners (daily figures, 30-day totals scaled the same way):

- Anna: volume $5,000,000, 80% taker, profit $500, `g = 0.4`, holding `h = 0.65`.
- Bob: volume $40,000,000, 60% taker, profit $8,000, `g = 0.25`, holding `h = 0.75`.
- Cara: volume $300,000, 90% taker, profit $400, `g = 0.8`, holding `h = 0.75`.

Fee credit (`Φ = fee × volume × (taker + 0.4 × maker)`): Anna 0.00025 × 5,000,000 × 0.88 = 1,100; Bob 0.00025 ×
40,000,000 × 0.76 = 7,600; Cara 0.00025 × 300,000 × 0.94 = 70.5.

Multiplier (`M = 0.85 + 0.15 × min(1, g/0.65) × h`): Anna 0.85 + 0.15 × 0.615 × 0.65 = 0.910; Bob 0.85 + 0.15 × 0.385 × 0.75 = 0.893; Cara 0.85 + 0.15 × 1 × 0.75 = 0.963.

Profit credit (`fee × λ × P × g × h`): Anna 0.00025 × 50 × 500 × 0.4 × 0.65 = 1.6; Bob 0.00025 × 50 × 8,000 × 0.25 ×
0.75 = 18.8; Cara 0.00025 × 50 × 400 × 0.8 × 0.75 = 3.0.

Scores: Anna 1,100 × 0.910 + 1.6 = 1,002.6; Bob 7,600 × 0.893 + 18.8 = 6,805.6; Cara 70.5 × 0.963 + 3.0 = 70.9.
Σ = 7,879.1 → weights: Anna 12.7%, Bob 86.4%, Cara 0.9% (the fee level cancels out of the shares).

Tokens and value: Anna 0.127 × 2,376 = 302 alpha = $1,611; Bob 0.864 × 2,376 = 2,052 alpha = $10,937; Cara 0.009 ×
2,376 = 21 alpha = $114.

Per dollar of fees paid: Bob gets back 109% ($10,937 on $10,000), Anna 129% ($1,611 on $1,250), Cara 152% ($114
on $75): nearly the same rate for everyone, size sets the total. Net cost: Anna pays Hyperliquid $1,950 and Kokoah
$1,250, receives $1,611 → net $1,589 (cheaper than Hyperliquid directly); Bob $13,200 + $10,000 − $10,937 =
$12,263 (cheaper); Cara $126 + $75 − $114 = $87 (cheaper).

Economics: `R` = $11,325; buyback $11,325; with Anna selling 70%, Bob and Cara 50%, validators 20%: sold = 211 +
1,026 + 11 + 490 = 1,738 alpha = $9,263. Buyback ÷ selling = 1.22. `price*` = 11,325 / (1,738 × 266.54) = 0.0244
TAO: at this volume ($45M a day) the buyback alone holds a little above today's price.

## 14. Attack arithmetic (what every number must satisfy)

For a strategy with no trading edge, expected pay from the subnet per unit of notional must stay below the fees it
cannot avoid. With the pool fully paid and `M ∈ [0.85, 1]`, a zero-edge account earns 85 to 90% of an honest
trader's rate per unit of fee credit, so Hyperliquid's fees and the maker credit must carry the defense: at 2.5 bps
a side the cheapest zero-edge strategy per unit of credit (a maker-based top-tier market maker with rebates on both
legs) costs 9.5 bps per round trip per credit, 190% of the builder fee; a taker hedge with a rebate on the hidden
leg 13.4. Fake volume breaks even only once the pool pays that much per credit, by which time honest traders
receive the same. Costs per round trip of notional (bps): taker-based
`2 × 4.5 + F + hidden`, maker-based `2 × 1.5 + F + hidden`, top-tier maker `2 × (−0.3) + F + hidden`, with `F = 2 × fee`
and `hidden` the other leg's fees. The design makes those costs mostly Kokoah revenue (§11) and keeps the
subnet's pay to zero-edge accounts small by: the taker/maker credit (§4), `g = 0` for negative-PnL accounts and the
length-of-record confidence and the blow-up rule (§6), the holding multiplier (§7), the mark clip and mirror
exclusions (§3), collateral per account, migration-only account changes (§16) and proportional allocation (§8).

## 15. Simulator column map (`sim/index.html`)

| Column | Formula |
|---|---|
| Volume $/day | input `V_i` (daily) |
| Taker % | input, `t` share |
| Skill g | input `g_i` |
| Profit $/day | input `P_i` (daily; negative allowed, counted as 0 in the profit credit) |
| Sells % | input `s_i`; `h_i = 0.5 + 0.5 × (1 − s_i)` when the holding multiplier is on |
| Hidden leg bps | input `hidden_bps_i` |
| Fees to Kokoah | `b_i = V_i × fee` |
| Fees to HL | `hl_i = V_i × (t × 4.5 + (1 − t) × 1.5) bps` |
| Fee credit | `Φ_i = fee × V_i × (t + 0.4 (1 − t))` |
| Multiplier | `M_i = m_min + (1 − m_min) × min(1, g_i/g_full) × h_i` |
| Profit credit | `fee × λ × max(0, P_i) × g_i × h_i` |
| Weight | `w_i = (Φ_i × M_i + profit credit_i) / Σ_j (…)` |
| Tokens/day | `w_i × A`, `A = 7,200 × miner_slot` |
| Earns $/day | tokens × `price_0 × tao_usd` |
| Net cost $/day | `hl_i + b_i + V_i × hidden_bps_i − earns` |
| Verdict | honest: paid to trade if net < 0; cheaper than HL direct if net ≤ `hl_i`; else pays (net − hl) over HL. Cheat: wins if net < 0, else loses |
| Tiles | revenue `R = Σ b_i`; buyback `β R`; selling `sold × price × tao_usd`; ratio; `price*`; price after N days (§10.3); pool; volume needed to hold the start price = `sold × price_0 × tao_usd / (β × fee)` |

## 16. Account switching and migration

- A miner's record (returns, receipts, staking) belongs to its binding. Migration to a new Hyperliquid address is
  allowed once per 90 days by a message signed by the old address, the new address and the hotkey; the record and
  the holding target carry over.
- A fresh binding is a new miner: confidence from zero, `staked = collateral` only, activity gates, registration
  burn and collateral.
- Addresses funded from the same bridge depositor or transferring between each other, and sub-accounts of a bound
  master, are one cluster for the mirror rule and the collateral.

## 17. Assumptions and open points

1. Whether the builder fee is charged on maker fills (Hyperliquid docs silent; if not, maker credit is moot).
2. Builder attribution needs node data; the public API lacks the builder address and keeps 10,000 fills per address.
3. The fee is the app's 0.025% target; the launch promotion currently charges 0, so there is nothing to rebate until
   the fee is live.
4. Root must enable `subnet_emission_enabled` for a new subnet; criteria undocumented.
5. Legal review of paying tokens for trading (pending for the app).
6. Vesting via an on-chain contract (later) to lower the sold share further.
