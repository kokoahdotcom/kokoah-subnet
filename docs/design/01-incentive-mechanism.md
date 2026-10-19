# Incentive mechanism, version 1

Date: 2026-10-19. Status: proposal for the owner's review. Replaces version 0 (volume × skill with a 1.5× fee cap).
Research behind it: `docs/research/02` (platform), `03` (other trading subnets), `04` (what Kokoah has),
`05` (reward-design precedents). Numbers come from `scripts/sim_incentives.py` (run it: `uv run python
scripts/sim_incentives.py`).

## 1. What the subnet is

Miners are traders. A miner binds a Hyperliquid address to a subnet hotkey and trades perps through Kokoah, so every
scored fill carries Kokoah's builder code. Validators read the chain (Hyperliquid) and pay each miner in the subnet's
alpha for the trading it did and how well it traded. The subnet's product is a verified, live ranking of Hyperliquid
traders built from their own fills; Kokoah's revenue is the builder fee on that trading, which buys back and burns
alpha. Miners run nothing: register, bind, trade.

## 2. The objective and the one law

Objective: the most sustainable builder-fee volume through Kokoah, with the biggest weight on trading size, a real
reward for skill, and a reward that visibly follows dollars made.

The one law every number below obeys: **a strategy with no trading edge must expect less from the subnet than it
pays in fees it cannot avoid.** Hyperliquid's base tier charges 4.5 bps taker and 1.5 bps maker a side; maker
rebates of 0.1 to 0.3 bps exist only above 0.5 to 3% of 14-day maker volume (docs read 2026-10-19, research 05).
The cheapest zero-edge strategy is therefore a top-tier market maker hedging a Kokoah position on another venue at
rebates: about 3.8 bps of notional per round trip (our 5 bps builder fee minus its rebates); if it must take
liquidity on our side it pays 13.4 bps. As long as the subnet pays a zero-edge account
less than that, wash trading, hedged pairs across accounts or venues, account splitting, lottery farms and profit
transfers all lose money, whatever we cannot see.

## 3. The reward, in three parts

Per miner `i`, per daily scoring window (trailing 30 days, with a 90-day component), everything in USD and converted
to alpha at the snapshot's pool price.

### 3.1 Base rebate, for everyone who trades

```
base_i = 1.0 × builder fees on taker fills  +  0.4 × builder fees on maker fills
```

Every trader who routes through Kokoah gets the builder fee on taker fills back in full, in alpha, and 40% of it on
maker fills. No skill test: losing traders are most of the volume and they are paid too. Why the split: a cheat
must pay Hyperliquid's taker fee (4.5 bps a side) to earn the full rebate, which is more than the whole rebate; on
maker fills the cheat's cost can fall to its rebate, so the base there must stay under about half the builder fee.
`Φ_i` (rebate-weighted fees) names the amount in the formula above; the bonus below is capped in multiples of it.

### 3.2 Skill bonus, shared by the skilled

A second slice of the pool is shared in proportion to **size times skill**, where size includes dollars made:

```
share_i  ∝  ( V_i + λ × P_i⁺ ) × g_i               λ = 50: one dollar of profit counts like $50 of volume
bonus_i  =  min( share of the bonus slice,  3 × Φ_i × g_i )
```

- `V_i`: notional of the miner's builder-code fills in the window (all fills, taker and maker), marked at the fill
  price but clipped to the mark price when a fill is more than 0.5% from the mark (that also neutralizes profit
  transfers through off-market self-trades).
- `P_i⁺`: dollar profit in the window if positive (equity-based, net of fees and funding, deposits and withdrawals
  removed), else 0. This is where dollars made show up in the paycheck: a whale making $2M on 6% and a trader making
  $40k on 30% both get credit for what they made, in proportion.
- `g_i` in [0, 1]: the skill multiplier (§3.3).
- The cap `3 × Φ_i × g_i` is the law from §2 applied per miner: a lucky zero-edge account (expected multiplier 0.18
  under the persistence test) can collect at most 3 × 0.18 ≈ 0.54 of its rebate-weighted fees on top of the base,
  which still leaves every cheat in §5 negative.

### 3.3 The skill multiplier `g` (percentage return, risk-adjusted, persistent)

Computed on the whole address from its daily equity curve (so a track record cannot be dressed up by trading
elsewhere), deposits and withdrawals removed:

```
r_d        = (E_d − E_{d−1} − flows_d) / E_{d−1}           daily return
R, MDD     = window return, max drawdown of the equity curve
g30        = clip( (R30 / max(MDD30, 0.05)) / 2, 0, 1 )     +20% on a 10% drawdown earns the full 30-day score
g90        = clip( (R90 / max(MDD90, 0.05)) / 3, 0, 1 )     +30% on a 10% drawdown over 90 days earns it
g_i        = ½ (g30 + g90) × coverage_i × newcomer_i        if at least 2 of the last 3 months are positive, else 0
```

- Gates (any failure gives `g_i = 0`): `R30 ≤ 0`; `MDD30 > 50%`; fewer than 10 round trips; fewer than 7 active
  days; average equity under $500; best single day above 50% of the window's positive PnL (the consistency rule
  every prop firm uses against one lucky day; FTMO set it to 50% on 2026-10-12).
- `coverage_i = min(1, C_i / 0.9)`: `C_i` is the share of the address's notional that carries Kokoah's builder code.
- `newcomer_i = min(1, days_since_binding / 14)`; an address under 90 days old uses `g30` alone and a bonus cap of
  2 instead of 3.
- Why this test ("test F" in the simulation): over 20,000 simulated 90-day accounts, a zero-edge account (one leg of
  a hedge, a coin flip) passes 39 to 44% of windows with an expected multiplier of 0.18; a trader with a modest real
  edge passes 86% with 0.58; a strong trader 97% with 0.79. The plain 30-day test let luck through at 0.26; the
  strictest weekly test cut luck to 0.14 but also cut honest traders to 51%. Test F keeps honest traders paid and
  makes luck worth three times less than skill.

### 3.4 Pool accounting

Each miner's entitlement is `E_i = base_i + bonus_i`. Validators set weights in proportion to `E_i`.

- If the pool is larger than the sum of entitlements (few traders, high alpha price), the surplus goes to the
  subnet's burn or recycle sink, which is the owner's hotkey. The chain counts miner incentive sent to hotkeys of the
  owner coldkey as `MinerBurned`, whether recycled or burned, and scales the subnet's TAO share by `1 − MinerBurned`
  next tempo (subtensor main, read 2026-10-19). Weight to any other UID is simply paid to it, so there is no free
  sink. A lasting surplus is therefore handled by lowering the miner slot of the emission split toward its 33%
  minimum; a treasury UID outside the owner coldkey is a question for the Rao team, not a plan.
- If the pool is smaller than the sum of entitlements, everyone is scaled down in proportion. The subnet then pays
  the whole pool and the caps no longer matter.
- Two slices: the base entitlements are served first; the bonus slice is what remains. If the base alone exceeds the
  pool, bonuses are zero that window and the base is scaled.

### 3.5 Worked numbers

Per round trip of notional, with Hyperliquid's base-tier fees (taker 4.5 bps, maker 1.5 bps a side) and a 2.5 bps
builder fee a side:

| Trader | Pays Hyperliquid | Pays Kokoah | Gets back in alpha | Net cost of trading |
|---|---|---|---|---|
| Losing retail, market orders | 9.0 | 5.0 | 5.0 (base) | 9.0, the same as trading on Hyperliquid directly |
| Decent day trader (g 0.5, 80% taker) | 7.8 | 5.0 | 11.0 | 1.8 |
| Persistent pro (g 1.0, half maker) | 6.0 | 5.0 | 14.0 | −3.0 (paid to trade) |

Pool size (2026-10-19, TAO $266.54): at an alpha price of 0.02 TAO the miner pool is about 2,950 alpha or $15,700
a day. The base alone absorbs it at about $63M of daily taker volume; with typical bonuses, the pool is fully paid
from roughly $40M a day. Halve those for a 5 bps builder fee. Until then the surplus is recycled and the miner slot
can be lowered.

## 4. Data, binding, epoch, consensus

- **Binding:** the miner publishes `{hotkey, hl_address, nonce}` signed by the Hyperliquid address (EIP-191) and by
  the hotkey, as an on-chain commitment (Commitments pallet, `set-commitment`). One address per hotkey and one
  hotkey per address; sub-accounts of a bound master count as the same miner; rebinding has a 30-day cooldown and
  resets `newcomer`.
- **Inputs per address:** fills with the taker flag (`crossed`), `fee` (which already includes `builderFee`, so
  net PnL must not subtract it twice), `builderFee`, `closedPnl`; daily equity; deposits and withdrawals; funding;
  mark prices per minute. The public fill record carries no builder address, and `userFillsByTime` keeps only an
  address's last 10,000 fills, so validators need a fill index built from node data (Kokoah's nodes have one;
  whether `replica_cmds` preserves the order's builder field is to verify). Sub-accounts (up to 50 per master) are
  separate addresses with self-trade prevention per address, so a master and its sub-accounts are one miner and
  the master's sub-account list is part of the dataset.
- **Epoch:** scores recomputed daily at 00:00 UTC; validators set weights every tempo from the latest score;
  commit-reveal stays on.
- **Determinism:** Kokoah's validator publishes the daily input dataset per address with a hash; other validators
  recompute and spot-check against Hyperliquid's public API or their own node. Trusted-but-verifiable at the start;
  independent nodes as validators arrive.
- **Consensus:** Yuma with several validators from day one (Kokoah's plus borrowed stake via childkeys from a root
  validator). Null consensus as the fallback if there is one validator at launch.
- **Emission split:** chain default 18% owner / 41% validators / 41% miners; the owner cut funds operations and
  buybacks; the miner slot is the lever for a lasting surplus (§3.4).

## 5. Attacks and answers

Cost versus the most the subnet pays, in bps per round trip of notional, under test F (luck's multiplier 0.18),
from the simulation:

| Attack | Cost | Pays at most | Net |
|---|---|---|---|
| Wash: taker through Kokoah against own maker elsewhere | 17.0 | 7.7 | −9.3 |
| Hedge: normal maker on both legs, other leg hidden | 11.0 | 3.1 | −7.9 |
| Hedge: top-tier market maker on both legs, other leg hidden | 3.8 | 3.1 | −0.7 |
| Hedge: top-tier MM taking on our leg, hidden maker leg | 13.4 | 7.7 | −5.7 |
| Bound pair, both legs through Kokoah | 22.0 | 8.9 | −13.1 |
| Lottery farm of taker accounts, keep the winners | 14.0 | 7.7 | −6.3 |

For comparison, a plain 90% fee rebate on all fills (dYdX's rule) pays a top-tier market maker +0.7 bps to hedge
through us, and raising our maker base to 60% pays it +0.8. The maker base and the bonus multiple are the two
numbers that must not drift up without re-running the table; the taker side has a wide margin.

Why the shape is cheat-proof at all: proportional allocation is the one rule that is both sybil-proof and
collusion-proof (Chen, Papadimitriou and Roughgarden, 2019); concave rules such as per-account caps on pool share,
square roots or per-account lotteries pay splitters, convex ones pay colluders. Here the base is linear in fees, the
bonus share is linear in volume and dollars with an intensive multiplier, and the bonus cap is linear in fees, so
splitting or merging accounts changes nothing. The v0 idea of a 20% cap on any miner's pool share was concave and
is gone.

Other answers:

| Attack | Answer |
|---|---|
| Sybil splitting | Base and size are linear in fees and volume: nothing gained, registration burn lost. |
| Hiding losers (realize winners, hold losers) | Equity-based returns include unrealized PnL. |
| Deposits dressed as returns | Flows removed from daily returns. |
| Profit transfer by off-market self-trades | Fills clipped to the mark price for volume and profit; the bonus cap bounds the rest. |
| Trading elsewhere, token volume through Kokoah | `coverage` scales `g` down. |
| Mirror pairs among bound addresses | Opposite-side fills in the same coin within 60 s and 20% of size: mirrored volume excluded above 25%, both zeroed above 50% and reported; funding-source clustering in v1; chain collateral stranded for reported pairs. |
| Data provider (Kokoah) manipulation | Hash-committed datasets, spot checks against public data, more validators over time. |
| Validator collusion or laziness | Yuma consensus, commit-reveal weights, scores reproducible from public data. |
| Lookahead | None: inputs are past on-chain events. |

## 6. Parameters

| Parameter | v1 value | Why |
|---|---|---|
| Base rebate, taker / maker | 100% / 40% of builder fees | taker: the cheat pays Hyperliquid more than we pay; maker: must stay under ~50% (§5) |
| Bonus cap | 3 × Φ × g (2 under 90 days) | §2 law with luck at 0.18; "too generous" row in §5 shows the limit |
| λ (dollars in the size term) | 50 | a $40k month counts like $2M of volume; dollars matter without dominating |
| Skill test | test F: ½(g30 + g90), 2 of 3 months positive | best honest-to-luck ratio in the simulation |
| g30 / g90 full score | R = 2 × MDD / R = 3 × MDD | |
| MDD floor | 5% | stops near-zero-volatility ratios |
| Gates | R30 > 0, MDD30 ≤ 50%, ≥ 10 round trips, ≥ 7 active days, equity ≥ $500 | |
| Coverage target | 90% | |
| Newcomer ramp | 14 days; `g30` only and cap 2 under 90 days | |
| Mark clip | fills > 0.5% from mark are scored at mark | |
| Mirror thresholds | 60 s, 20% size; exclude > 25%, zero > 50% | |
| Registration | burn, `min_burn` about 1 TAO | prices sybils |
| Scoring time | 00:00 UTC daily | |

## 7. The leaderboard

Public, sorted by skill (`g`, with the 30- and 90-day returns and drawdowns), showing dollar profit, volume and
coverage next to it, so "best traders", "most profitable" and "most active" are all visible. Emissions follow §3.

## 8. Open points

1. Builder attribution (node data only) and whether the builder fee is charged on maker fills: the docs are silent
   and a sample of 57,000 public fills showed none. If it is not, the maker base is moot and maker volume earns only
   the bonus.
2. Hyperliquid's fee tiers and rebates at launch set the §5 table; re-run before choosing the maker base.
3. The burn or recycle path for bought-back alpha and the fees → TAO path.
4. Root's `subnet_emission_enabled` switch: criteria unknown; the "verified trader ranking" is the pitch.
5. Legal review of paying tokens for trading (pending for the app).
6. The builder fee is 0 under the launch promotion; the rebate needs a non-zero fee to rebate.
7. Whether the λ dollar term should use realized profit only (simpler to verify) or equity-based (harder to game).
8. Adoption: dYdX's 90%-of-fees rebate was about 12% of what traders paid in total and was ignored, then set to
   zero. Ours returns the whole builder fee (about a third of a taker's total cost) and pays skilled traders beyond
   their fees, but the alpha's price risk is the trader's; watch real adoption on testnet before mainnet.
9. Greevils ran real Hyperliquid trading through a builder code and lost its slot: the alpha must find stakers or
   the emission gate ends the subnet whatever the mechanism. The buyback from real fees is the answer and must be
   visible from day one.

## 9. Next steps

1. `kokoah_subnet.scoring`: pure functions (lots, taker/maker split, daily returns with flows, drawdown, test F,
   entitlements, caps, pool accounting) with unit tests on synthetic fills.
2. Replay harness on public Hyperliquid addresses; look at the distribution of `g`, `Φ` and bonuses; tune §6.
3. Validator loop on the localnet with a fake data source; then testnet with real node data.
