# Kokoah subnet incentive mechanism: complete specification (v5, 2026-10-20)

Self-contained. Every symbol is defined here. Changes from v4: the whole miner pool is granted by volume inside the
0.85 to 1.0 range (no stake-based tier), the part of a miner's grant above the Kokoah fee it paid is time-released
and unlocks only against future fees, the anti-farm test is done on liquid plus vesting alpha, and a daily coverage
figure is published.

## 0. Purpose and intention

A Bittensor subnet whose miners are traders. A miner binds one Hyperliquid address to a subnet hotkey and trades
perpetuals through the Kokoah builder code. Validators compute, from Hyperliquid data of the bound addresses and
chain state, each miner's share of the daily miner emission. Kokoah collects the builder fee on every such trade
and spends it buying back the subnet's token (alpha). The intention: maximum trading volume through Kokoah;
similar volume, similar pay; skill and holding a small nudge (two traders with equal volume differ by at most 15%,
one perfect at both, the other worst at both); every day's alpha granted to miners; and no cheat that lets
sell pressure outrun revenue.

## 1. Chain facts used

- Blocks 12 s; tempo 360 blocks = 72 min. Validators set weights every tempo; scores are recomputed once a day.
- Emission `E = 7,200` alpha per day, split `miner_slot = 0.33` (`A = 2,376` alpha a day), `owner_slot = 0.33`
  (auto-locked, never sold), `validator_slot = 0.34` (validators and their stakers; outside this mechanism).
- Alpha pool: constant product, `price = tao_reserve / alpha_reserve` (TAO per alpha).
- Registration: burn plus a minimum collateral in alpha locked on the hotkey (owner-set).
- A **vault**: a subnet UID on its own coldkey (a multisig at first, an on-chain contract later; not the owner's
  coldkey, so the chain's `MinerBurned` rule does not apply). It receives the time-released part of the grants and
  releases it to miners by stake transfer as it vests.

## 2. Notation

| Symbol | Meaning | Default |
|---|---|---|
| `i`, `d` | a miner (one bound Hyperliquid address with its sub-accounts, and its hotkey); a scoring day (00:00 UTC) | |
| `f` | a fill of miner `i` carrying the Kokoah builder code; `n_f` notional (USD), `t_f` 1 if taker, `b_f = n_f × fee` the builder fee | |
| `fee` | builder fee rate per fill | 0.00025 (0.025%) |
| `c_m` | maker fee credit (taker is 1.0) | 0.4 |
| `T_hold` | holding time for full credit | 10 min |
| `α` | daily smoothing weight of today's value | 0.3 |
| `E_d`, `flows_d` | end-of-day account value of the bound address (unrealized included); net deposits that day | |
| `p_d` | alpha price in USD on day `d` = pool price (TAO) × TAO/USD mark from the node | |
| `g'_i`, `h'_i` | skill and holding factors, both in [0, 1] | |
| `k` | vesting rate: locked alpha released per dollar of new fee credit, in alpha at the day's price | 0.5 |

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
   both addresses get `g' = h' = 0` for 30 days and are reported (collateral stranded).

## 4. Daily fee credit, holding-time weighted, smoothed

Each fill opens or closes FIFO lots per coin; the hold time of a lot runs from its opening fill to its closing fill
(open lots count their age so far). Both fills of a lot are weighted by `hold(f) = min(1, hold_time / T_hold)`.

```
Φ_i,d  = Σ_{f on day d}  b_f × ( t_f + (1 − t_f) × c_m ) × hold(f)
Φ_i,d  ≤ 30 × Ē_i,d × fee                          turnover cap: at most the fee on 30 × mean equity a day
Φ̂_i,d  = (1 − α) × Φ̂_i,d−1 + α × Φ_i,d              70/30 smoothing; Φ̂ = 0 at the binding
```

Instant round trips earn almost nothing; a position held ten minutes earns in full; market orders count fully,
limit orders at 40%. A day's fees are worth 30% today, 21% tomorrow, 15% the day after; the weights sum to 1, so
over time every dollar of fees is counted exactly once.

## 5. The grant: the whole pool, by volume, within 0.85 to 1.0

```
M_i,d     = 0.85 + 0.075 × g'_i,d + 0.075 × h'_i,d        the rate multiplier, in [0.85, 1.0]
score_i,d = Φ̂_i,d × M_i,d
w_i,d     = score_i,d / Σ_j score_j,d
grant_i,d = w_i,d × A                                     alpha granted today; Σ_i grant_i,d = A
```

Two miners with equal volume are paid within 15% of each other whatever their skill and holding; a miner with
twice the volume is paid about twice as much. Everything is linear in fees with bounded multipliers, so splitting
or merging accounts changes nothing (proportional allocation is the unique sybil- and collusion-proof rule; Chen,
Papadimitriou, Roughgarden 2019).

## 6. Liquid now, the rest time-released

```
cap_i,d     = Φ̂_i,d / p_d                                 the alpha worth the miner's smoothed Kokoah fee credit
liquid_i,d  = min( grant_i,d , cap_i,d )                   paid to the miner's hotkey (weights to the hotkey)
locked_i,d  = grant_i,d − liquid_i,d                       paid to the vault (weights to the vault UID), booked to i
ledger_i,d  = ledger_i,d−1 + locked_i,d − vest_i,d
vest_i,d    = min( ledger_i,d−1 ,  k × Φ_i,d / p_d )       released from the vault to i's hotkey as stake
```

What this does:

- **No one is paid less than their volume share.** The grant is the full proportional share; only its liquidity is
  staged. Alpha above the fee paid is the miner's, booked in the vault, and unlocks at `k` alpha-dollars per dollar
  of new fee credit: keep trading and it flows; stop and it waits (after 90 days without any fee credit, the
  remaining ledger is forfeited to the buyback-and-burn).
- **Farms cannot monetize the excess.** A farm's income per round trip is at most its Kokoah fee (liquid) plus
  `k` times its Kokoah fee (vesting), `1.5 ×` the builder fee at `k = 0.5`, while its unavoidable cost is
  Hyperliquid's fees on both legs plus the builder fee. It loses at every pool size (§11).
- **Liquid supply is bounded by fees.** Alpha that can be sold on day `d` is at most
  `Σ_i (liquid_i,d + vest_i,d) ≤ (1 + k) × Σ_i Φ̂_i,d / p_d`: worth at most 1.5 × the day's fees. The buyback is
  1.0 × the day's fees, so the buyback covers the liquid supply whenever miners keep at least a third of what they
  receive; the holding factor pays them to.
- When fees are large next to the pool (`Σ cap ≥ A`), nothing is locked: the subnet is a plain volume rebate.

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

No drawdown anywhere; one bad day is one capped loss that fades in weeks. Calibration (mean `g_raw` after 60
days, 6,000 simulated accounts per profile): zero-edge 0.05 to 0.10, martingale 0.08, low-volatility grinder
0.30, real edge 0.35, strong edge 0.56.

## 8. Holding factor `h'`

```
staked_i,d   = alpha staked on i's own hotkey by its own coldkey (collateral, kept rewards, bought alpha)
target_i,d   = liquid alpha received by i in the last 60 days (liquid grants plus vested releases)
h'_i,d       = min( 1 , staked_i,d / max(target_i,d, collateral_min) )
```

Keep what you were paid and the factor is 1; sell it all and it is 0; buying alpha counts. Vault-held alpha is not
staked by the miner and does not count. A fresh hotkey has `staked = collateral` and a small target, so it starts
near 1 and only loses the factor by selling.

## 9. Activity gates (for the multiplier's skill and holding parts, and the leaderboard)

Over the last 30 days: at least 10 completed round trips, at least 7 days with a builder-code fill, mean account
value at least $500. A miner failing a gate is paid at `M = 0.85`.

## 10. Economics and the coverage figure

```
R_d         = Σ_i Σ_f b_f                                   builder-fee revenue of the day (USD)
buyback_d   = β × R_d                                       β = 1, daily, on chain, bought alpha burned
liquid_d    = Σ_i (liquid_i,d + vest_i,d) × p_d             alpha that became sellable today, in USD (≤ 1.5 × R)
coverage_d  = buyback_d / sold_d                            sold_d = alpha actually sold by miners that day (chain data)
```

`coverage_d` is published daily on the leaderboard. The design guarantees `liquid_d ≤ 1.5 × R_d`; whether coverage
stays above 1 depends on how much of the liquid alpha miners keep, which the holding factor rewards. The validator
slot (34%) and its stakers are outside the mechanism; the owner cut is locked.

## 11. Attack accounting (per round trip of notional, bps, fee 2.5 a side, k = 0.5)

Income counts liquid plus vesting alpha, as GPT asked: at most `(1 + k) ×` the credited builder fee.

| Strategy | Pays Hyperliquid | Pays Kokoah | Hidden leg | Income at most | Net |
|---|---|---|---|---|---|
| Wash: taker through Kokoah vs own maker elsewhere | 9 + 3 | 5 | 0 | 1.5 × 5 = 7.5 | −9.5 |
| Two bound accounts, taker vs maker, both through Kokoah | 9 + 3 | 5 + 5 | 0 | 1.5 × (5 + 2) = 10.5, and mirrored → 0 | −11.5 or worse |
| Hedge, taker on Kokoah, hidden leg maker elsewhere | 9 | 5 | 3 | 7.5 | −9.5 |
| Top-tier market maker, maker both legs, one through Kokoah | −0.6 | 5 | −0.6 | 1.5 × 2 = 3 | −0.8 |
| Instant round trips in one account | 9 | 5 | 0 | ≈ 0 (hold weighting) | −14 |
| Lottery farm of small accounts | 9 | 5 | 0 | 7.5 | −6.5 per account |
| Whale staking a lot, trading little | its fees | its fees | | grant ∝ its own volume; holding adds ≤ 7.5% | nothing to extract |
| Sybil splitting or merging | same | same | | linear everywhere | 0 gain, burn lost |

Every row is negative at every pool size. The tightest is the top-tier market maker at −0.8; `k ≤ 0.9` keeps it
negative, and lowering the maker credit to 0.25 would widen it to −1.9.

## 12. Parameters

| Parameter | Value |
|---|---|
| builder fee | 0.025% per fill (owner) |
| maker credit | 0.4 |
| `T_hold` | 10 min |
| turnover cap | credit ≤ fee on 30 × mean equity per day |
| smoothing `α` | 0.3, for credit and skill |
| multiplier | `0.85 + 0.075 g' + 0.075 h'` |
| liquid cap | 100% of the smoothed Kokoah fee credit, in alpha at the day's price |
| vesting `k` | 0.5 alpha-dollars per dollar of new fee credit; ledger forfeited after 90 days without fees |
| skill windows | confidence 60 days (PSR, hurdle 0.03, ≥ 10 days), quality 30 days (gain-to-pain, half-life 10 days), blow-ups 60 days |
| `g'` full score | g = 0.65 |
| return cap | ±10% a day |
| holding target | liquid alpha received in the last 60 days (floor: the collateral minimum) |
| activity gates | ≥ 10 round trips, ≥ 7 active days, mean equity ≥ $500 (30 days) |
| mirror rules | 60 s / 20% size; position correlation < −0.8 on ≥ 10 days; exclude > 25%, zero > 50% |
| mark clip | 0.5% |
| migration | once per 60 days, signed by old address, new address and hotkey; credit, skill, ledger and target carry over |
| emission split | miner 33%, owner 33% auto-locked, validators 34% |
| buyback | 100% of fees, daily, on chain |

## 13. Worked examples

**Young subnet.** `A = 2,376` alpha at $5.33 (pool $12,667); fees today $2,000 (volume $8M). Two miners, equal
volume $4M, 100% taker, `Φ̂ = $1,000` each. Miner A: strong skill (`g' = 1`) and holds everything (`h' = 1`):
`M = 1.0`. Miner B: no skill, sold everything: `M = 0.85`. Grants: A 2,376 × 1.0/1.85 = 1,284 alpha ($6,846); B
1,092 alpha ($5,821). Caps: $1,000 / $5.33 = 188 alpha each. Liquid: 188 each ($1,000, exactly the fee each paid).
Locked to the vault: A 1,096 alpha, B 904 alpha, booked to each. Tomorrow, if each pays another $1,000 in fees,
each vests `0.5 × 1,000 / 5.33 = 94` alpha from the ledger. Liquid alpha that day: 188 + 94 = 282 each, worth
$1,500 = 1.5 × fees, while the buyback is $1,000 each. A keeps hers (factor stays 1); B sells (factor 0, and B's
pay stays 15% below A's). B's unvested ledger keeps waiting for B's future fees. Same volume, pay within 15%, and
no way to turn fake volume into more than 1.5 × the fee it costs.

**Busy subnet.** Fees $87,700 a day (volume $350M). Σ caps = 16,450 alpha > `A`: every grant is below its cap,
nothing is locked, the pool is a volume rebate paying about 14% of fees back, the vault only releases old ledgers.

## 14. Account switching and migration

A miner's smoothed credit, skill, ledger and holding target belong to the binding. Migration to a new address is
allowed once per 60 days by a message signed by the old address, the new address and the hotkey; everything
carries over, including the vault ledger. A fresh binding starts from zero with `staked = collateral`. Clusters
(§3) cover linked addresses.

## 15. Assumptions and open points

1. The vault: a multisig-held UID at launch, an on-chain contract later; stake transfers from the vault to miners
   (`transfer-stake` / `move-stake`) are the release path, to verify on the localnet.
2. Whether the builder fee is charged on maker fills (Hyperliquid docs silent; if not, maker credit is moot).
3. Builder attribution needs node data; the public API lacks the builder address and keeps 10,000 fills per address.
4. The alpha price for the caps: pool price at the scoring block times the node's TAO/USD mark.
5. Legal review of paying tokens for trading (pending for the app).
