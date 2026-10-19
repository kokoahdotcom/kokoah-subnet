# Kokoah subnet incentive mechanism: complete specification (v2, 2026-10-19)

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
| `fee` | builder fee rate per fill | 0.0006 (= 0.06% = 6 bps) |
| `E_d` | account value of the whole address at the end of day `d`, including unrealized PnL | USD |
| `flows_d` | net deposits into the address during day `d` (deposits − withdrawals − transfers out) | USD |
| `mark(f)` | Hyperliquid mark price of the coin at the time of fill `f` | |
| `λ` | dollar-profit weight | 50 |
| `c_m` | maker fee credit | 0.4 |
| `σ` | share of the pool split by fee credit | 0.5 |

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

## 4. Fee credit (the "fee half")

```
Φ_i = Σ_{f ∈ W30}  b_f × ( t_f × 1.0  +  (1 − t_f) × c_m )
    = fee × ( taker_notional_i + 0.4 × maker_notional_i )
```

Taker fills earn full credit, maker fills 40%. Reason: to earn taker credit an attacker must pay Hyperliquid's
taker fee (4.5 bps a side), which exceeds anything the subnet returns; a top-tier market maker's maker cost can be a
rebate, so maker credit stays low (§11).

## 5. Volume, profit, size (the "skill half" base)

```
V_i = Σ_{f ∈ W30} n_f                                  (after §3 clipping; all builder-code fills)
P_i = max( 0 ,  Σ_{d ∈ W30} ( E_d − E_{d−1} − flows_d ) )   dollar profit of the whole address, 0 if negative
S_i = V_i + λ × P_i                                     size; λ = 50 means $1 of profit counts like $50 of volume
```

`P_i` is equity-based (includes unrealized PnL, so losers cannot be hidden by never closing them) and net of all
fees and funding (they are already inside `E_d`). Deposits and withdrawals are removed through `flows_d`.

## 6. Skill multiplier `g_i ∈ [0, 1]`

### 6.1 Daily returns and the equity curve

```
r_d = ( E_d − E_{d−1} − flows_d ) / E_{d−1}             time-weighted daily return
C_d = Π_{k ≤ d} ( 1 + r_k )                              cumulative curve, C_0 = 1
R_W = C_end / C_start − 1                                return over window W
MDD_W = max_{d ∈ W} ( 1 − C_d / max_{k ≤ d, k ∈ W} C_k ) max drawdown over W (0 to 1)
```

### 6.2 Calmar-type scores

```
g30 = clip( ( R30 / max(MDD30, 0.05) ) / 2 ,  0 ,  1 )     R30 = 2 × MDD30 earns 1.0
g90 = clip( ( R90 / max(MDD90, 0.05) ) / 3 ,  0 ,  1 )     R90 = 3 × MDD90 earns 1.0
```

The 0.05 floor on drawdown stops near-zero-volatility accounts from scoring infinitely.

### 6.3 Persistence test ("test F")

Let `m1, m2, m3` be the returns of the last three calendar months. The test passes if at least two of them are
positive.

### 6.4 Gates (any failure gives `g_i = 0` for this scoring day)

- `R30 ≤ 0`
- `MDD30 > 0.50`
- fewer than 10 completed round trips in `W30`
- fewer than 7 days with at least one builder-code fill in `W30`
- mean daily equity in `W30` below $500
- consistency: the best single day's PnL exceeds 50% of the window's total positive daily PnL
- persistence test (6.3) fails

### 6.5 Coverage and newcomer factors

```
C_i        = builder-code notional of the address in W30 / all notional of the address in W30
coverage_i = min( 1 ,  C_i / 0.9 )
newcomer_i = min( 1 ,  days_since_binding / 14 )
```

### 6.6 The multiplier

```
g_i = ½ × ( g30 + g90 ) × coverage_i × newcomer_i        if all gates pass and history ≥ 90 days
g_i = g30 × coverage_i × newcomer_i                       if all gates pass and history < 90 days
g_i = 0                                                   otherwise
```

Calibration (20,000 simulated 90-day accounts, `scripts/sim_incentives.py`): a zero-edge account (a hedge leg, a
coin flip; daily drift 0, daily volatility 2 to 5%) passes 39 to 44% of windows with mean `g = 0.18`; a trader with
+0.3% daily drift and 2% volatility passes 86% with mean `g = 0.58`; +0.6% drift, 2.5% volatility: 97%, `g = 0.79`.

## 7. Holding multiplier

```
held_i = alpha received by i in W90 that is still staked on i's own hotkey  /  alpha received by i in W90
h_i    = 0.5 + 0.5 × held_i                                                   (1.0 if everything kept, 0.5 if all sold)
```

Alpha moved to another coldkey counts as sold.

## 8. Pool split

```
Q_i = S_i × g_i × h_i                                          skill-half score

w_i = σ × Φ_i / Σ_j Φ_j   +   (1 − σ) × Q_i / Σ_j Q_j          σ = 0.5
```

If `Σ_j Q_j = 0` (no miner passes the skill gates), the whole pool is split by fee credit: `w_i = Φ_i / Σ_j Φ_j`.
`Σ_i w_i = 1` always. Validators submit `w_i` as the on-chain weights every tempo; the chain pays
`alpha_i = w_i × A` per day (spread over the day's 20 tempos).

Properties: `Φ_i` is linear in fees, `S_i` is linear in volume and profit, `g_i` and `h_i` are bounded multipliers
that do not change when an account is split. Hence `w` is homogeneous of degree one in the splittable quantities:
splitting one miner into two identical halves gives the same total (sybil-neutral), merging gives the same total
(collusion-neutral). Proportional allocation is the unique rule with both properties (Chen, Papadimitriou,
Roughgarden 2019); any per-miner cap or concave transform would reward splitting and is deliberately absent.

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

Per round trip of notional, in bps, with `fee = 6 bps` a side (`F = 12` per round trip): a taker-based wash or hedge
pays Hyperliquid 9 and Kokoah 12 on our leg (57% of its spend is our revenue; 50% counting its hidden leg's fees);
a maker-based one 3 and 12 (80%; 67%); a top-tier market maker −1.2 and 12 (111%; 118%). The subnet pays a
zero-edge account only through the fee half (its `g` is 0 for a wash bot, 0.18 on average for a hedge leg), so the
cheapest attack must out-earn `Φ`-share pay that is itself bounded by the pool. In the young-subnet phase the pool
per dollar of fees is high and zero-edge traders enter until pay per unit of size ≈ cost / 0.18, at which point
honest traders with `g ≈ 0.6` are paid about three times a hedger's cost per unit of size; at 6 bps most of that
early money is Kokoah revenue, not Hyperliquid's.

## 12. Parameters (defaults)

| Parameter | Value |
|---|---|
| builder fee `fee` | 0.06% per fill (6 bps a side); Hyperliquid maximum 0.1% |
| Hyperliquid fees | taker 4.5 bps, maker 1.5 bps a side (base tier); maker rebates −0.1 to −0.3 bps above 0.5–3% of 14-day maker volume |
| maker fee credit `c_m` | 0.4 |
| pool split `σ` | 0.5 by fee credit, 0.5 by `Q` |
| `λ` | 50 |
| `g30`, `g90` full score | `R = 2 × MDD`, `R = 3 × MDD`; drawdown floor 0.05 |
| persistence | 2 of the last 3 months positive |
| gates | `R30 > 0`; `MDD30 ≤ 0.5`; ≥ 10 round trips; ≥ 7 active days; mean equity ≥ $500; best day ≤ 50% of positive PnL |
| coverage target | 0.9 |
| newcomer ramp | 14 days; `g30` only under 90 days of history |
| holding multiplier | `0.5 + 0.5 × held` over 90 days |
| mark clip | 0.5% |
| mirror thresholds | 60 s, 20% size; exclude > 25%; zero both > 50% |
| emission split | miner 0.33, owner 0.33 (auto-locked), validators 0.34 |
| buyback `β` | 1.0, daily, bought alpha burned |
| collateral | owner-set minimum per miner (start ≈ $500 of alpha) |
| scoring | daily 00:00 UTC; weights every tempo (72 min) |

## 13. Worked example (one day)

Pool `A = 2,376` alpha; token price 0.02 TAO; TAO $266.54; one alpha = $5.33; pool value $12,667.
`fee = 0.0006`, `c_m = 0.4`, `λ = 50`, `σ = 0.5`.

Three miners (daily figures):

- Anna: volume $5,000,000, 80% taker, profit $500, `g = 0.5`, sells 70% (`h = 0.65`).
- Bob: volume $40,000,000, 60% taker, profit $8,000, `g = 0.3`, sells 50% (`h = 0.75`).
- Cara: volume $300,000, 90% taker, profit $400, `g = 1.0`, sells 50% (`h = 0.75`).

Fee credit (`Φ = fee × volume × (taker + 0.4 × maker)`):
- Anna: 0.0006 × 5,000,000 × (0.8 + 0.4 × 0.2) = 3,000 × 0.88 = 2,640
- Bob: 0.0006 × 40,000,000 × (0.6 + 0.4 × 0.4) = 24,000 × 0.76 = 18,240
- Cara: 0.0006 × 300,000 × (0.9 + 0.4 × 0.1) = 180 × 0.94 = 169.2
- Σ Φ = 21,049.2 → shares: Anna 12.5%, Bob 86.7%, Cara 0.8%

Skill-half score (`Q = (V + λ P) × g × h`, with daily figures used consistently):
- Anna: (5,000,000 + 50 × 500) × 0.5 × 0.65 = 5,025,000 × 0.325 = 1,633,125
- Bob: (40,000,000 + 50 × 8,000) × 0.3 × 0.75 = 40,400,000 × 0.225 = 9,090,000
- Cara: (300,000 + 50 × 400) × 1.0 × 0.75 = 320,000 × 0.75 = 240,000
- Σ Q = 10,963,125 → shares: Anna 14.9%, Bob 82.9%, Cara 2.2%

Weights (`w = 0.5 × Φ-share + 0.5 × Q-share`):
- Anna: 0.5 × 0.125 + 0.5 × 0.149 = 0.137
- Bob: 0.5 × 0.867 + 0.5 × 0.829 = 0.848
- Cara: 0.5 × 0.008 + 0.5 × 0.022 = 0.015

Tokens and value: Anna 0.137 × 2,376 = 326 alpha = $1,736; Bob 0.848 × 2,376 = 2,014 alpha = $10,735;
Cara 0.015 × 2,376 = 36 alpha = $190.

Net cost: Anna pays Hyperliquid 5,000,000 × (0.8 × 0.00045 + 0.2 × 0.00015) = $1,950 and Kokoah $3,000, receives
$1,736 → net $3,214 (pays $1,264 a day more than trading on Hyperliquid directly). Bob: Hyperliquid
40,000,000 × (0.6 × 0.00045 + 0.4 × 0.00015) = $13,200, Kokoah $24,000, receives $10,735 → net $26,465. Cara:
Hyperliquid $126, Kokoah $180, receives $190 → net $116 (cheaper than Hyperliquid directly).

Economics: `R` = $3,000 + $24,000 + $180 = $27,180; buyback $27,180; sold = 0.7 × 326 + 0.5 × 2,014 + 0.5 × 36 +
0.2 × 7,200 × 0.34 = 228 + 1,007 + 18 + 490 = 1,743 alpha = $9,290 at today's price. Buyback ÷ selling = 2.9.
`price*` = 27,180 / (1,743 × 266.54) = 0.0585 TAO: the buyback alone holds about three times today's price.

## 14. Attack arithmetic (what every number must satisfy)

For a strategy with no trading edge, expected pay from the subnet per unit of notional must stay below the fees it
cannot avoid. With the pool fully paid, pay per unit comes from the fee half (share of `Φ`) and, for the lucky leg of
a hedge, from the skill half with `E[g] = 0.18`. Costs per round trip of notional (bps): taker-based
`2 × 4.5 + F + hidden`, maker-based `2 × 1.5 + F + hidden`, top-tier maker `2 × (−0.3) + F + hidden`, with `F = 2 × fee`
and `hidden` the other leg's fees. The design makes those costs mostly Kokoah revenue (§11) and keeps the
subnet's pay to zero-edge accounts small by: the taker/maker credit (§4), `g = 0` for negative-PnL accounts and the
persistence test (§6), the holding multiplier (§7), the mark clip and mirror exclusions (§3), collateral per
account and proportional allocation (§8).

## 15. Simulator column map (`sim/index.html`)

| Column | Formula |
|---|---|
| Volume $/day | input `V_i` (daily) |
| Taker % | input, `t` share |
| Skill g | input `g_i` |
| Profit $/day | input `P_i` (daily; negative allowed, counted as 0 in `S_i`) |
| Sells % | input `s_i`; `h_i = 0.5 + 0.5 × (1 − s_i)` when the holding multiplier is on |
| Hidden leg bps | input `hidden_bps_i` |
| Fees to Kokoah | `b_i = V_i × fee` |
| Fees to HL | `hl_i = V_i × (t × 4.5 + (1 − t) × 1.5) bps` |
| Fee share | `Φ_i / Σ Φ` |
| Skill share | `Q_i / Σ Q` with `Q_i = (30 V_i + λ × 30 × max(0, P_i)) × g_i × h_i` |
| Weight | `w_i = σ × fee share + (1 − σ) × skill share` (or fee share alone if `Σ Q = 0`) |
| Tokens/day | `w_i × A`, `A = 7,200 × miner_slot` |
| Earns $/day | tokens × `price_0 × tao_usd` |
| Net cost $/day | `hl_i + b_i + V_i × hidden_bps_i − earns` |
| Verdict | honest: paid to trade if net < 0; cheaper than HL direct if net ≤ `hl_i`; else pays (net − hl) over HL. Cheat: wins if net < 0, else loses |
| Tiles | revenue `R = Σ b_i`; buyback `β R`; selling `sold × price × tao_usd`; ratio; `price*`; price after N days (§10.3); pool; volume needed to hold the start price = `sold × price_0 × tao_usd / (β × fee)` |

## 16. Assumptions and open points

1. Whether the builder fee is charged on maker fills (Hyperliquid docs silent; if not, maker credit is moot).
2. Builder attribution needs node data; the public API lacks the builder address and keeps 10,000 fills per address.
3. The 0.06% fee is a decision for the Kokoah platform (today's target is 0.025% and the launch promotion is 0).
4. Root must enable `subnet_emission_enabled` for a new subnet; criteria undocumented.
5. Legal review of paying tokens for trading (pending for the app).
6. Vesting via an on-chain contract (later) to lower the sold share further.
