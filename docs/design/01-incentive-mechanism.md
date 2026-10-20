# Incentive mechanism, version 3.1

Date: 2026-10-20. Status: proposal for the owner's review. Version 3.1 (owner's decision): similar volume means
similar pay; skill and holding move a miner's rate only within 0.85 to 1.0 times, so anyone can trade without fear,
and the fee level does the protecting. Version 3 replaces the drawdown-based skill score with a
statistical one (confidence × quality, no drawdown anywhere), replaces the fixed 50/50 pool split with a skill
multiplier on the fee credit, ties track records and holding to the miner's binding so switching accounts never
helps, and measures holding as alpha staked on the hotkey. Version 2 (whole pool paid daily; "revenue beats sell
pressure" through fee level, holding and emission split) stands. Versions 0 and 1 (capped payouts) are superseded;
their cheat pricing is kept in `scripts/sim_incentives.py`; the skill-score comparison is `scripts/sim_skill.py`. Research: `docs/research/02` (platform), `03` (other trading subnets), `04`
(what Kokoah has), `05` (reward-design precedents).

## 1. What the subnet is

Miners are traders. A miner binds a Hyperliquid address to a subnet hotkey and trades perps through Kokoah, so every
scored fill carries Kokoah's builder code. Validators read the chain (Hyperliquid) and split the day's miner pool
among the bound traders by how much they traded and how well. The subnet's product is a verified, live ranking of
Hyperliquid traders built from their own fills; Kokoah's revenue is the builder fee on that trading, all of which
buys back alpha. Miners run nothing: register, bind, trade.

## 2. The accounting that everything follows

Every day the chain mints a fixed amount of alpha for miners, `A` (33% of the subnet's emission in the split below,
about 2,380 alpha a day). All of it is paid out. Miners sell a share `s` of what they get. Kokoah collects builder
fees `R` and spends a share `β` of them buying alpha back.

```
revenue beats sell pressure   ⟺   β × R  >  s × A × price
```

`A` is fixed by the chain and `price` is set by the market, so this holds when:

1. **the builder fee is high.** `R = volume × fee`. Hyperliquid allows up to 10 bps a side for perps. **Decision
   (owner, 2026-10-19): 6 bps a side**, to be set on the Kokoah platform when the subnet launches. At 6 bps the
   builder fee is bigger than Hyperliquid's own taker fee (4.5 bps), so most of what any trader (honest or not)
   spends to earn alpha comes to Kokoah, not to Hyperliquid. At today's 2.5 bps it is the other way round and most
   of the money leaks.
2. **miners keep alpha.** `s` falls with the holding multiplier, the registration collateral and, later, vesting.
3. **less alpha reaches sellers.** The miner slot is at its 33% minimum; the owner's 33% is auto-locked into
   conviction and never sold; validators take 34%.

The price the buyback can hold on its own, with no other buyers: `price* = β × R / (s × A)`. Above it the market
needs stakers; below it the buyback lifts the price. At 6 bps a side (TAO $266.54 on 2026-10-19, `A` = 2,380,
`β` = 1):

| Daily volume | Fees a day | `s` (share sold) | Price the buyback holds |
|---|---|---|---|
| $10M | $6k | 1.0 | $2.5 = 0.009 TAO |
| $10M | $6k | 0.5 | $5.0 = 0.019 TAO |
| $20M | $12k | 1.0 | $5.0 = 0.019 TAO |
| $20M | $12k | 0.5 | $10.1 = 0.038 TAO |
| $30M | $18k | 0.4 | $18.9 = 0.071 TAO |
| $50M | $30k | 0.4 | $31.5 = 0.118 TAO |
| $100M | $60k | 0.4 | $63 = 0.236 TAO |

To hold 0.02 TAO: $21M a day if every miner sells everything, $10.6M a day if half is held, $8.5M a day at 40%
sold. So the fee level is the main lever, holding the second, volume the third. Nothing is held back from miners.

## 3. How the pool is split

Per miner `i`, per daily scoring. One score, all paid out in proportion:

```
M_i      =  m_min + ( 1 − m_min ) × min( 1 , g_i / g_full ) × h_i          m_min = 0.85, g_full = 0.65
score_i  =  Φ_i × M_i   +   fee × λ × P_i⁺ × g_i × h_i                      λ = 50
weight_i =  score_i / Σ_j score_j
```

In words: every trader's fee credit is the base, and the multiplier `M` moves it only between 0.85 (no skill, or
everything sold) and 1.0 (a strong, persistent trader who keeps the tokens). Similar volume means similar pay; the
best possible trader earns at most 18% more per dollar of fees than a losing one. Dollars made add a small
tie-breaker (about 1% of the score at `λ = 50`); the leaderboard shows them in full. A newcomer starts at 0.85 on
day one: no waiting period for pay, only for the bonus.

**Why this is safe (owner's requirement: encourage everyone to trade, generate volume).** The protection against
fake volume is the fee level and the taker/maker credit, not the skill score. At 6 bps a side, the cheapest
zero-edge strategy per unit of fee credit is a taker hedge with a rebate on the hidden leg: 9 + 12 − 0.6 = 20.4 bps
per round trip for 1.0 credit (a maker-based top-tier market maker: −0.6 + 12 − 0.6 = 10.8 bps for 0.4 credit, 27
per credit). So fake volume cannot break even until the pool pays about 20 bps per round trip of taker credit, which
is 170% of the builder fee; honest market-order traders are paid at least that much back before any farm appears,
and a farm's spend is mostly Kokoah revenue anyway (§3.5). What the narrow range gives up: skilled traders are no
longer paid several times more per dollar than luck, so the "pay for skill" story rests on the leaderboard, and the
holding incentive is mild, so the price floor sits near "fees back" rather than above it (§2). If the builder fee is
ever lowered below Hyperliquid's taker fee, this range must be widened again or the farms win.

### 3.1 Fee credit `Φ_i` (everyone who trades)

```
Φ_i = 1.0 × builder fees on taker fills  +  0.4 × builder fees on maker fills       over the last 30 days
```

Market orders count fully, limit orders at 40%. The taker/maker split is the anti-wash rule: to earn taker credit a
cheat must pay Hyperliquid's taker fee on top of ours; maker credit is kept low because a market maker's cost can
fall to its rebate.

### 3.2 Dollars made `P_i⁺`

Profit of the whole address over the last 30 days, equity-based (unrealized included, deposits and withdrawals
removed), zero if negative. `fee × λ × P_i⁺` turns it into fee-credit units: at 6 bps and `λ = 50`, $1 of profit
counts like $50 of volume, which is 3 cents of fee credit.

### 3.3 Skill score `g_i` ∈ [0, 1]: confidence × quality, no drawdown

Computed on the whole address's daily returns since its binding (or since migration, §3.6), up to 180 days. Every
daily return is capped at ±10% before use, so one disaster counts as one capped loss while a repeated pattern keeps
counting.

```
r_d        = (E_d − E_{d−1} − flows_d) / E_{d−1}                  daily return, capped to [−0.10, +0.10]
confidence = clip( (PSR − 0.5) / 0.45 , 0, 1 )                      PSR = probabilistic Sharpe ratio of the capped
                                                                    daily returns against a hurdle of 0.03 a day
                                                                    (Bailey & López de Prado 2012: skew- and
                                                                    kurtosis-adjusted; needs ≥ 10 days, else 0)
quality    = clip( (GPR_ew − 1) / 1.2 , 0, 1 )                      GPR_ew = recency-weighted gain-to-pain: Σ w_d × gains
                                                                    / Σ w_d × losses, w_d = 0.5^(age_d / 45 days);
                                                                    1.0 (break-even) → 0, 2.2 → 1
quality   ×= 0.5   if 2 or more days below −10% in the last 90 days (a blow-up pattern, not one accident)
g_i        = confidence × quality × coverage_i                      coverage_i = min(1, share of the address's
                                                                    notional carrying our builder code / 0.9)
g_i        = 0   if fewer than 10 round trips or fewer than 7 active days in the last 30 days, or mean equity < $500
```

Why this shape: confidence asks "is the edge real or luck?" and grows with the length of the record (a fresh account
cannot score high for weeks); quality asks "how good is it?" using every day, not a single worst dip. Nothing is a
cliff: a bad day lowers the score and fades with a 45-day half-life; no drawdown rule, no monthly test, no single-day
rule. On 6,000 simulated accounts per profile (`scripts/sim_skill.py`), at 90 days: a coin-flip account scores 0.07
on average (the old drawdown design: 0.19), a martingale that wins small and loses big 0.07, a steady low-volatility
grinder 0.30, a real edge 0.38, a strong edge 0.65, and a real edge with one −20% day 0.20 (recovering to 0.32 by
180 days instead of being zeroed). Real edge is 5.2 times luck, against 3.1 before.

### 3.4 Holding multiplier `h_i`

```
staked_i = alpha staked on miner i's own hotkey by its own coldkey (kept rewards, collateral, bought alpha)
target_i = max( alpha received by i in the last 90 days ,  30 × A / N_active )
h_i      = 0.5 + 0.5 × min( 1 , staked_i / target_i )
```

Unstaking lowers it immediately; moving alpha to another coldkey counts as unstaking. A new hotkey starts at 0.5 and
earns the full multiplier only by holding (or buying) about a month of an average miner's pay. Rewards land as stake
on the hotkey, so "keeping" is simply not unstaking.

### 3.5 Why no caps, and what happens early

Everything is proportional: `Φ` is linear in fees, `P⁺` linear in profit, `g` and `h` bounded multipliers that do
not change when an account is split. Proportional allocation is the one rule that is both sybil-proof and
collusion-proof (Chen, Papadimitriou and Roughgarden, 2019); per-miner caps and concave transforms are deliberately
absent. With the whole pool always paid, the reward per dollar of fees is high while the subnet is young; zero-edge
traders then enter until the reward per round trip of taker credit falls to about 20 bps (170% of the builder fee),
and honest traders are paid the same rate, times 0.85 to 1.0. At a 6 bps builder fee most of that early spend
is Kokoah revenue (a taker-based cheat pays Kokoah 12 bps and Hyperliquid 9 per round trip, 57% to us; maker-based
80%; a top-tier market maker 111%).

### 3.6 Account switching

A miner's track record, receipts and holding belong to its binding, not to an address.

- **Migration:** once per 90 days a miner may move to a new Hyperliquid address by a message signed by the old
  address, the new address and the hotkey. The record carries over, good and bad; so does the holding target.
- **Rebinding without migration** (a fresh address, a fresh hotkey) is a new miner: confidence from zero (weeks of
  trading before any skill pay), `h = 0.5`, the activity gates, a new registration burn and collateral. Switching to
  escape a bad month therefore costs more than the bad month.
- **Clustering:** addresses funded from the same bridge depositor or by transfers between each other are one cluster
  for the mirror rule (§5) and the collateral; sub-accounts of a bound master are the same miner.

### 3.7 Registration collateral and emission split

- Miners register by burn with a minimum collateral in alpha locked on the hotkey (owner-set): demand at entry,
  locked supply, a cost for every extra account. The collateral counts as `staked_i` in §3.4.
- Emission split: miner 33%, owner 33% with `owner_cut_auto_lock_enabled`, validators 34%.
- Buyback: 100% of builder fees, every day, on chain, published.

## 4. Data, binding, epoch, consensus

- **Binding:** the miner publishes `{hotkey, hl_address, nonce}` signed by the Hyperliquid address (EIP-191) and by
  the hotkey, as an on-chain commitment (Commitments pallet, `set-commitment`). One address per hotkey and one
  hotkey per address; sub-accounts of a bound master count as the same miner; rebinding has a 30-day cooldown and
  resets `newcomer`.
- **Inputs per address:** fills with the taker flag (`crossed`), `fee` (which already includes `builderFee`),
  `builderFee`, `closedPnl`; daily equity; deposits and withdrawals; funding; mark prices per minute. The public
  fill record carries no builder address and `userFillsByTime` keeps only an address's last 10,000 fills, so
  validators use a fill index built from node data (Kokoah's nodes have one; whether `replica_cmds` preserves the
  order's builder field is to verify). Sub-accounts (up to 50 per master) are separate addresses with self-trade
  prevention per address, so the master's sub-account list is part of the dataset.
- **Epoch:** scores recomputed daily at 00:00 UTC; validators set weights every tempo (72 minutes on mainnet) from
  the latest score, so miners are paid continuously; the 30 and 90 days are only the look-back. Commit-reveal on.
- **Determinism:** Kokoah's validator publishes the daily input dataset per address with a hash; other validators
  recompute and spot-check against Hyperliquid's public API or their own node.
- **Consensus:** Yuma with several validators from day one (Kokoah's plus borrowed stake via childkeys from a root
  validator); Null consensus as the fallback with one validator.

## 5. Attacks and answers

| Attack | Answer |
|---|---|
| Wash trading (instant round trips, self or paired) | Pays Hyperliquid's taker fee to earn taker credit; maker credit low; PnL negative so no skill half; at a 10 bps builder fee most of its spend is our revenue anyway. |
| Hedged pair or basket, other leg bound, unbound or on another venue | Skill score 0.07 on average and only on the lucky leg; no dollars made; holding multiplier halves it if sold; registration collateral per account; the fee level keeps its spend mostly ours. |
| Lottery farm of small accounts | Confidence needs weeks of data; 10 round trips, 7 active days, $500 equity; collateral and burn per account; a fresh account starts at `h = 0.5`. |
| Switching accounts to escape a bad record | Records belong to the binding; migration carries them; a fresh binding starts from zero confidence and zero holding. |
| One lucky streak, then quitting | Quality is recency-weighted and confidence needs a long record; a short streak scores little. |
| Martingale (small steady wins, rare big loss) | Capped returns keep every −7% day counting; two or more blow-up days halve the quality; the skew-adjusted confidence falls. Scores 0.07 on average. |
| Sybil splitting or merging | Proportional allocation: nothing gained, collateral and burn lost. |
| Hiding losers (realize winners, hold losers) | Equity-based returns include unrealized PnL. |
| Deposits dressed as returns | Flows removed from daily returns. |
| Profit transfer by off-market self-trades | Fills clipped to the mark price for volume and profit. |
| Trading elsewhere, token volume through Kokoah | `coverage` scales `g` down. |
| Selling rewards from another hotkey or coldkey | Holding is alpha staked on the miner's own hotkey; anything moved out counts as sold. |
| Mirror pairs among bound addresses | Opposite-side fills in the same coin within 60 s and 20% of size: mirrored volume excluded above 25%, both zeroed above 50% and reported; funding-source clustering later; collateral stranded for reported pairs. |
| Selling the rewards | Holding multiplier; collateral; the owner cut is locked. |
| Data provider (Kokoah) manipulation | Hash-committed datasets, spot checks against public data, more validators over time. |
| Validator collusion or laziness | Yuma consensus, commit-reveal weights, scores reproducible from public data. |
| Lookahead | None: inputs are past on-chain events. |

## 6. Parameters

| Parameter | v3 value | Why |
|---|---|---|
| Builder fee | 6 bps a side (owner, 2026-10-19; Hyperliquid's maximum is 10), approved by the user in the wallet | the lever that makes revenue beat sell pressure |
| Pool rule | `score = Φ × M + fee λ P⁺ g h`, `M = 0.85 + 0.15 × min(1, g/0.65) × h`, paid in proportion | similar volume, similar pay; skill and holding a small bonus |
| m_min, g_full | 0.85, 0.65 | the rate range 0.85 to 1.0; a strong trader reaches the top |
| λ (dollars) | 50 | $1 of profit counts like $50 of volume |
| Fee credit, taker / maker | 1.0 / 0.4 | anti-wash |
| Return cap | ±10% a day | one accident is one capped loss |
| Confidence | PSR of capped returns, hurdle 0.03 a day, ≥ 10 days, (PSR − 0.5)/0.45 | is the edge real |
| Quality | recency-weighted gain-to-pain, half-life 45 days, (GPR − 1)/1.2 | how good, every day counts |
| Blow-up rule | ≥ 2 days below −10% in 90 days halves quality | pattern, not accident |
| Activity gates | ≥ 10 round trips, ≥ 7 active days in 30 days, mean equity ≥ $500 | anti-dust |
| Coverage target | 0.9 | trade through Kokoah |
| Holding | `0.5 + 0.5 × min(1, staked / target)`, target = max(90-day receipts, a month of average pay) | selling or switching halves the multiplier |
| Migration | once per 90 days, signed by old and new address | records follow the person |
| Mark clip | fills > 0.5% from mark scored at mark | profit-transfer guard |
| Mirror thresholds | 60 s, 20% size; exclude > 25%, zero both > 50% | |
| Emission split | miner 33%, owner 33% auto-locked, validators 34% | least alpha to sellers |
| Buyback | 100% of builder fees, daily, on chain | the price floor |
| Scoring | 00:00 UTC daily; weights every tempo | |

## 7. The leaderboard

Public, sorted by skill (`g`, with its confidence and quality parts, the 30- and 90-day returns), showing dollar
profit, volume, coverage and holding next to it. Emissions follow §3.

## 8. Open points

1. Builder attribution (node data only) and whether the builder fee is charged on maker fills (docs silent; a
   57,000-fill sample showed none). If not, maker credit is moot.
2. The 6 bps builder fee needs the user's approval in the wallet (`approveBuilderFee`) and a platform change (not
   implemented yet; today's target is 2.5 bps and the launch promotion is 0): the alpha paid to traders is the
   offset for the higher fee.
3. The burn or recycle path for bought-back alpha and the fees → TAO path; how the collateral minimum is set.
4. Root's `subnet_emission_enabled` switch: criteria unknown; the "verified trader ranking" is the pitch.
5. Legal review of paying tokens for trading (pending for the app).
6. The builder fee is 0 under the launch promotion; nothing to rebate until it is not.
7. Vesting through an on-chain contract (the chain runs an EVM with staking precompiles) as a later way to lower `s`.
8. Adoption: dYdX's rebate was ignored at 12% of what traders paid; ours pays the whole miner pool to traders, but
   the alpha's price risk is theirs; watch adoption on testnet before mainnet.
9. Greevils ran real Hyperliquid trading through a builder code and lost its slot: the alpha must find stakers or
   the emission gate ends the subnet. The daily buyback from real fees is the answer and must be visible from day one.

## 9. Next steps

1. `kokoah_subnet.scoring`: pure functions (lots, taker/maker split, daily returns with flows, drawdown, test F,
   holding, the two halves) with unit tests on synthetic fills.
2. Replay harness on public Hyperliquid addresses; look at the distribution of `g`, `Φ` and `Q`; tune §6.
3. Validator loop on the localnet with a fake data source; then testnet with real node data.
