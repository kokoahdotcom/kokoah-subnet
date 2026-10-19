# Incentive mechanism, version 2

Date: 2026-10-19. Status: proposal for the owner's review. Version 2 pays the whole miner pool out every day (no
caps, no recycling) and makes "revenue bigger than sell pressure" a matter of fee level, holding and emission split,
not of withholding alpha. Versions 0 and 1 (capped payouts) are superseded; their cheat pricing still applies and
is kept in `scripts/sim_incentives.py`. Research: `docs/research/02` (platform), `03` (other trading subnets), `04`
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

1. **the builder fee is high.** `R = volume × fee`. Hyperliquid allows up to 10 bps a side for perps; at 10 bps
   the builder fee is bigger than Hyperliquid's own taker fee (4.5 bps), so most of what any trader (honest or not)
   spends to earn alpha comes to Kokoah, not to Hyperliquid. At 2.5 bps it is the other way round and most of the
   money leaks.
2. **miners keep alpha.** `s` falls with the holding multiplier, the registration collateral and, later, vesting.
3. **less alpha reaches sellers.** The miner slot is at its 33% minimum; the owner's 33% is auto-locked into
   conviction and never sold; validators take 34%.

The price the buyback can hold on its own, with no other buyers: `price* = β × R / (s × A)`. Above it the market
needs stakers; below it the buyback lifts the price. Examples (TAO $266.54 on 2026-10-19, `A` = 2,380, `β` = 1):

| Daily volume | Builder fee | Fees a day | `s` | Price the buyback holds |
|---|---|---|---|---|
| $20M | 2.5 bps | $10k | 1.0 | $4.2 = 0.016 TAO |
| $20M | 5 bps | $20k | 0.5 | $16.8 = 0.063 TAO |
| $30M | 10 bps | $60k | 0.4 | $63 = 0.24 TAO |
| $100M | 10 bps | $200k | 0.4 | $210 = 0.79 TAO |

So the fee level is the main lever, holding the second, volume the third. Nothing is held back from miners.

## 3. How the pool is split

Per miner `i`, per daily scoring window (trailing 30 days, with a 90-day component). Two halves, both paid in full:

```
weight_i = 0.5 × Φ_i / Σ_j Φ_j   +   0.5 × Q_i / Σ_j Q_j
```

### 3.1 Fee half: `Φ_i`, the fee credit (everyone who trades)

```
Φ_i = 1.0 × builder fees on taker fills  +  0.4 × builder fees on maker fills       over W30
```

Half the pool is shared in proportion to the builder fees each miner paid, counting market orders fully and limit
orders at 40%. No skill test; losing traders are most of the volume and they are paid too. The taker/maker split
is the anti-wash rule: to earn taker credit a cheat must pay Hyperliquid's taker fee on top of ours; maker credit is
kept low because a market maker's cost can fall to its rebate.

### 3.2 Skill half: `Q_i = (V_i + λ × P_i⁺) × g_i × h_i`

- `V_i`: notional of builder-code fills in W30 (all fills), each marked at the fill price but clipped to the mark
  price when the fill is more than 0.5% away (neutralizes profit transfers through off-market self-trades).
- `P_i⁺`: dollar profit in W30 if positive, else 0 (equity-based, net of fees and funding, deposits and withdrawals
  removed). `λ = 50`: one dollar made counts like $50 traded.
- `g_i` in [0, 1]: the skill multiplier (§3.3).
- `h_i = 0.5 + 0.5 × held_i`: the holding multiplier; `held_i` is the share of the alpha the miner received in the
  last 90 days that is still staked on its own hotkey. Selling the rewards halves the next bonus.

Big traders earn the most in total; skilled traders earn the most per dollar traded; dollars made count in both the
ranking and the pay; holders earn more than sellers.

### 3.3 The skill multiplier `g` (percentage return, risk-adjusted, persistent)

Computed on the whole address from its daily equity curve, deposits and withdrawals removed:

```
r_d   = (E_d − E_{d−1} − flows_d) / E_{d−1}
R_W, MDD_W = window return and max drawdown of the equity curve
g30   = clip( R30 / max(MDD30, 0.05) / 2 , 0, 1 )       +20% on a 10% drawdown earns the full 30-day score
g90   = clip( R90 / max(MDD90, 0.05) / 3 , 0, 1 )       +30% on a 10% drawdown over 90 days earns it
g_i   = ½ (g30 + g90) × coverage_i × newcomer_i          if at least 2 of the last 3 months are positive, else 0
```

Gates (any failure gives `g_i = 0`): `R30 ≤ 0`; `MDD30 > 50%`; fewer than 10 round trips; fewer than 7 active
days; mean equity under $500; best single day above 50% of the window's positive PnL (the prop-firm consistency
rule). `coverage_i = min(1, C_i / 0.9)` with `C_i` the share of the address's notional that carries our builder
code. `newcomer_i = min(1, days_since_binding / 14)`; under 90 days of history `g30` is used alone.

Why this test: over 20,000 simulated 90-day accounts a zero-edge account (one leg of a hedge, a coin flip) scores
0.18 on average and a trader with a modest real edge 0.58, a strong one 0.79; honest traders pass 86 to 97% of
windows (`scripts/sim_incentives.py`, test F).

### 3.4 Why no caps, and what happens early

Everything is proportional: the fee half is linear in fees, the skill half is linear in volume and dollars with
bounded multipliers. Proportional allocation is the one rule that is both sybil-proof and collusion-proof (Chen,
Papadimitriou and Roughgarden, 2019); the earlier per-miner caps were concave and are gone.

With the whole pool always paid, the reward per dollar of fees is high while the subnet is young (big pool, little
volume). Zero-edge traders then enter until the reward rate falls to their cost divided by their skill score (0.18),
at which point honest traders with `g ≈ 0.6` are paid about three times a hedger's cost per dollar of size. This is
the subsidy phase and it is paid for by the emission, not by Kokoah. What matters for law "revenue beats sell
pressure" is where that early volume's money goes: at a 10 bps builder fee, a taker-based cheat pays Kokoah 20 bps
and Hyperliquid 9 per round trip (69% to us), a maker-based one 20 and 3 (87%), a top-tier market maker 20 and
earns 1.2 in rebates (106%). At 2.5 bps the same shares are 36%, 62% and 132% of a far smaller number. The fee
level decides whether the subsidy phase feeds the buyback or Hyperliquid.

### 3.5 Registration collateral and emission split

- Miners register by burn with a minimum collateral (the chain's miner collateral, owner-set): they must buy and
  lock alpha to mine. That is demand at entry, locked supply while they mine, and a cost for every sybil account.
- Emission split: miner 33%, owner 33% with `owner_cut_auto_lock_enabled`, validators 34%.
- Buyback: `β = 1` at launch, every day, on chain, published. The price floor in §2 only exists if stakers can see
  it happen.

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
| Hedged pair or basket, other leg bound, unbound or on another venue | Skill score 0.18 on average and only on the lucky leg; no dollars made; holding multiplier halves it if sold; registration collateral per account; the fee level keeps its spend mostly ours. |
| Lottery farm of small accounts | `newcomer`, 10 round trips, 7 active days, $500 equity, consistency rule, collateral per account. |
| Sybil splitting or merging | Proportional allocation: nothing gained, collateral and burn lost. |
| Hiding losers (realize winners, hold losers) | Equity-based returns include unrealized PnL. |
| Deposits dressed as returns | Flows removed from daily returns. |
| Profit transfer by off-market self-trades | Fills clipped to the mark price for volume and profit. |
| Trading elsewhere, token volume through Kokoah | `coverage` scales `g` down. |
| Mirror pairs among bound addresses | Opposite-side fills in the same coin within 60 s and 20% of size: mirrored volume excluded above 25%, both zeroed above 50% and reported; funding-source clustering later; collateral stranded for reported pairs. |
| Selling the rewards | Holding multiplier; collateral; the owner cut is locked. |
| Data provider (Kokoah) manipulation | Hash-committed datasets, spot checks against public data, more validators over time. |
| Validator collusion or laziness | Yuma consensus, commit-reveal weights, scores reproducible from public data. |
| Lookahead | None: inputs are past on-chain events. |

## 6. Parameters

| Parameter | v2 value | Why |
|---|---|---|
| Builder fee | 10 bps a side (Hyperliquid's maximum for perps), approved by the user | the lever that makes revenue beat sell pressure |
| Pool split | 50% by fee credit, 50% by size × skill × holding | everyone paid, skill rewarded |
| Fee credit, taker / maker | 1.0 / 0.4 | anti-wash |
| λ (dollars in size) | 50 | a $40k month counts like $2M of volume |
| Skill test | test F: ½(g30 + g90), 2 of 3 months positive | best honest-to-luck ratio |
| g30 / g90 full score | R = 2 × MDD / R = 3 × MDD | |
| Gates | R30 > 0, MDD30 ≤ 50%, ≥ 10 round trips, ≥ 7 active days, equity ≥ $500, best day ≤ 50% of profit | |
| Holding multiplier | 0.5 + 0.5 × share of the last 90 days' rewards still staked | selling halves the next bonus |
| Collateral | minimum alpha locked per miner, owner-set (start around $500 worth) | demand at entry, sybil cost |
| Emission split | miner 33%, owner 33% auto-locked, validators 34% | least alpha to sellers |
| Buyback | 100% of builder fees, daily, on chain | the price floor |
| Mark clip | fills > 0.5% from mark scored at mark | profit-transfer guard |
| Mirror thresholds | 60 s, 20% size; exclude > 25%, zero > 50% | |
| Scoring time | 00:00 UTC daily; weights every tempo | |

## 7. The leaderboard

Public, sorted by skill (`g`, with the 30- and 90-day returns and drawdowns), showing dollar profit, volume,
coverage and holding next to it. Emissions follow §3.

## 8. Open points

1. Builder attribution (node data only) and whether the builder fee is charged on maker fills (docs silent; a
   57,000-fill sample showed none). If not, maker credit is moot.
2. A 10 bps builder fee needs the user's approval in the wallet (`approveBuilderFee`) and a product decision: it is
   four times today's target and the app's own fee promise; the alpha rebate is the offset.
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
