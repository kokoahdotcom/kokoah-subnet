# Incentive mechanism, version 0

Date: 2026-10-19. Status: proposal for the owner's review. Research behind it: `docs/research/02` (platform),
`03` (other trading subnets), `04` (what Kokoah has).

## 1. What the subnet is

Miners are traders. A miner binds a Hyperliquid address to a subnet hotkey and trades perps through Kokoah, so every
scored fill carries Kokoah's builder code. Validators read the chain (Hyperliquid) and pay each miner by how much
real, held trading it did, multiplied by how well it traded. The subnet's product is a verified, live ranking of
Hyperliquid traders built from their own fills; Kokoah's revenue is the builder fee on all of that trading, which buys
back and burns the subnet's alpha.

Nothing the miner does is off-chain: no miner server, no uptime, no model. Register, bind, trade.

## 2. The objective

Maximize sustainable builder-fee volume through Kokoah. "Sustainable" rules out paying for volume that only exists to
collect emissions (wash trading, hedged account pairs, lottery accounts), because that volume sells alpha faster than
fees can buy it back and ends the subnet. So the mechanism pays for **volume that survives a skill multiplier and a
holding-time filter**, and scale (how much a miner trades) carries most of the weight.

## 3. The formula

For each miner `i`, over the scoring window `W` (trailing 30 days, recomputed daily at 00:00 UTC):

```
weight_i  =  V_i × g_i  /  Σ_j (V_j × g_j)          then capped per miner (§3.4), excess burned
```

### 3.1 Scale: held builder volume `V_i`

`V_i` is the notional of the miner's fills that carry Kokoah's builder code, weighted by how long the resulting
position was held:

```
V_i = Σ_fills  notional_f × h(hold_f)          h(t) = min(1, t / T_hold)          T_hold = 10 minutes
```

Hold time comes from FIFO lots per address and coin: an opening fill starts a lot; a reducing fill closes lots in
order, and both the opening and the closing notional of the matched quantity are credited with the lot's hold time.
Lots still open at the window end count with their age so far. A round trip closed in seconds earns almost nothing;
a position held ten minutes or more earns in full. Scalpers still pay fees and get partial credit; wash round trips
get none.

Two exclusions: fills whose counterparty is the same address, another address of the same miner coldkey, or an
address with a flagged mutual-counterparty pattern (§5); and fills without the builder code. Two caps: a miner's
counted volume is at most `turnover_cap × average equity × days` (sybils cannot claim volume on dust), and notional
is marked at the fill price.

Linear in volume on purpose: splitting volume across accounts neither helps nor hurts, so sybils gain nothing and
pay the registration burn for each extra UID.

### 3.2 Skill multiplier `g_i` in [0, 1]

Scale-free, computed on the whole address (so the track record is real), from the daily equity curve:

```
r_d        = (E_d − E_{d−1} − flows_d) / E_{d−1}      daily time-weighted return; flows = net deposits that day
R          = Π (1 + r_d) − 1                           return over W
MDD        = max drawdown of the cumulative curve over W
skill_raw  = R / max(MDD, 0.05)                        Calmar-like; the 5% floor stops low-volatility tricks
g_raw      = clip(skill_raw / 2, 0, 1)                 R = 2 × MDD earns the full multiplier
g_i        = g_raw × coverage_i × newcomer_i
```

- `E_d` is the account value including unrealized PnL (so a trader cannot hide losers by never closing them);
  deposits and withdrawals are removed from the return.
- `coverage_i = min(1, C_i / 0.9)` where `C_i` is the share of the address's notional in `W` that carries Kokoah's
  builder code. A miner who trades mostly elsewhere earns a reduced multiplier.
- `newcomer_i = min(1, days_since_binding / 14)`: a fresh account cannot claim a full multiplier on one lucky week.
- Once an address has 60 days of history, `g_raw` becomes the mean of the 30-day and 90-day values, so persistence
  pays and one month of luck fades.

Gates (any failure sets `g_i = 0` for the window): `R ≤ 0`; `MDD > 50%`; fewer than 10 completed round trips;
fewer than 7 active days; average equity below `$500`.

Why this shape: ratios reward skill regardless of account size, so the small trader with a real edge ranks at the
top of the leaderboard. But the ratio is a multiplier between 0 and 1, while volume spans orders of magnitude, so
emissions follow size. A whale with a modest edge earns far more than a small star; the star earns more per dollar
traded. Both are what the objective wants.

### 3.3 Where the ranking shows

Public leaderboard: sorted by `g_raw` (skill), with equity band, 30- and 90-day return, drawdown, coverage and
volume shown. Emissions: by `weight_i`. Two orderings, both honest; nobody is told that fees are the score.

### 3.4 Caps and burns

- **Per-miner cap:** `weight_i ≤ 20%`. Excess is burned, not redistributed, so a whale cannot be paid for the whole
  subnet and the cap is not a transfer to competitors.
- **Payout cap:** a miner's emission in a window is at most `k × fees_i` (`k = 1.5`), with `fees_i` the **builder
  fees** it paid in `W` (not Hyperliquid's fees), converted to alpha at the snapshot's pool price and the TAO/USD mark
  from the node (Hyperliquid lists a TAO perp; to verify). Excess is burned. This is a cap, not the score, and it is
  the one defense that works when the other half of a hedge is invisible to us: §5.1 shows that with `k ≤ 1.5` no
  strategy without a trading edge can net positive, whatever the number of addresses. Emissions above the cap are the
  subnet's margin of safety, burned.
- Unpaid miner emission is burned via the chain's burn path (`recycle_or_burn` set to burn).

### 3.5 Paying the pool out fully

The payout cap can leave part of the miner pool unpaid. When that happens and what to do about it:

- **It only bites while the subnet is young.** The cap binds when the miner pool is worth more than 1.5 times all
  the builder fees paid in the window, which is the case when there are few real traders and a big pool. Once honest
  volume is large, the pool is spread over much more volume, every miner's share is below its cap, and the whole
  pool is paid. The cap then protects without costing anyone anything.
- **Unpaid emission is not wasted.** `recycle_or_burn` can send it back into the subnet's pool (which supports the
  alpha price every holder and every paid miner benefits from) instead of burning it. Either way it is not lost to
  attackers.
- **Two safe ways to raise the cap** if the owner wants bigger rewards from day one:
  1. **A stricter "keep winning" test.** A lucky hedge leg passes a 30-day test about half the time. If the full
     multiplier needs positive returns in 3 of the last 4 weeks and 2 of the last 3 months, plus the Calmar
     threshold, a zero-edge account passes rarely (about 5 to 10% of windows). The expected payout of a hedge falls
     by the same factor, so a cap of `k = 3` to `4` becomes safe. Honest traders with a persistent edge pass most
     windows and are paid up to the higher cap. Cost: new miners wait about 90 days for the full cap.
  2. **Deposits (the chain's miner collateral).** A miner locks alpha on its hotkey; its payout cap becomes
     `max(1.5 × fees, m × deposit)`. Cheaters need a deposit on every account, and the losing accounts' deposits stay
     locked and useless (the chain releases collateral only against earned emission, and validators can refuse to
     score a flagged hotkey, which strands the lock). Deposits raise the cost of an attack but do not bound it as
     hard as the fee cap, so keep `m` modest (1 to 2) and pair it with the stricter test.
- **Paying by profit instead of volume** (Vanta's current design) does not avoid the problem: a hedge's winning leg
  shows real profit, so Vanta needs collateral and a debt ledger to make losers pay. It is the same medicine with a
  noisier base, so it is not adopted here.

Recommendation: start at `k = 1.5`; let the cap rise automatically to `3` for miners who pass the 90-day
persistence test; offer deposits for miners who want more; recycle unpaid emission into the pool. In steady state the
pool is paid in full to real traders.

## 4. Scoring epoch, data and consensus

- **Epoch:** scores recomputed daily at 00:00 UTC from the trailing 30 (and 90) days. Validators set weights every
  tempo (72 minutes on mainnet at tempo 360) from the latest daily score; commit-reveal stays on (the chain default).
- **Binding:** the miner publishes `{hotkey, hl_address, nonce}` signed by the Hyperliquid address (EIP-191) and by
  the hotkey, as an on-chain commitment (the Commitments pallet, `set-commitment`). One address per hotkey, one
  hotkey per address; rebinding has a 30-day cooldown and resets `newcomer`. Validators read bindings from chain, so
  miners run nothing.
- **Inputs per address:** fills with builder attribution, daily equity, deposits and withdrawals, funding. All of it
  is public Hyperliquid state. Builder attribution is certain in node data (the order action carries the builder);
  whether the public `userFills` endpoint exposes the builder is to verify. Kokoah's nodes already index fills.
- **Determinism:** every validator must compute the same scores or consensus clips them. Kokoah's validator publishes
  the daily input dataset per address (fills, equity, flows) with a hash; other validators fetch it, recompute, and
  spot-check samples against Hyperliquid's public API or their own node. Start trusted-but-verifiable; move to
  independent nodes as validators arrive.
- **Consensus:** Yuma (default), several validators from day one (Kokoah's plus borrowed stake via childkeys from a
  root validator, see research 02 §3). Null consensus is the fallback if only one validator exists at launch.
- **Emission split:** chain default 18% owner / 41% validators / 41% miners; the owner cut funds operations and
  buybacks. Revisit with the Etao bounds once stakers exist.

## 5. Attacks and answers

### 5.1 The hedge attack, with numbers

The attack the mechanism must survive: open long on an address bound to the subnet and short the same size at the
same time somewhere we cannot see (an unbound address, a sub-account, another exchange), or across two bound
addresses, or across N addresses keeping only the winners. No market risk, large held volume, fees paid, emissions
collected. Holding-time weighting does nothing against it (the positions are held), and the skill multiplier only
halves it (one leg always wins), so it is answered by the payout cap.

Per unit of notional, one round trip on the Kokoah leg costs the attacker Hyperliquid's fee plus the builder fee, and
the hedge leg costs Hyperliquid's fee again. Builder fee 2.5 bps per side; Hyperliquid 4.5 bps taker, 1.5 bps maker,
down to a 0.3 bps maker rebate at the top tier. The subnet can pay the winning leg at most `k × 5 bps` (its builder
fees), and only the winning half of the legs are paid:

| Attacker | Cost per round trip, bps | Max payout, bps | `k` where it breaks even |
|---|---|---|---|
| Lone leg, normal maker, hedge elsewhere as maker | 3 + 5 + 3 = 11 | k × 5 × ½ = 2.5k | 4.4 |
| Lone leg, top-tier market maker on both legs | −0.6 + 5 − 0.6 = 3.8 | 2.5k | 1.5 |
| Two bound addresses, both through Kokoah, undetected | 2 × 4.4 = 8.8 | k × 5 (one winner) | 1.8 |
| N addresses, keep the winners | ≥ 3.8 per address | ≤ 2.5k per address on average | 1.5 |

With `k = 1.5` every row is zero or negative before the registration burn, the collateral and the gates, and before
the lucky leg's multiplier falls short of 1 (it usually does). With `k = 1` there is margin. Honest traders are
unaffected by the cap until their emission exceeds 1.5 times what they paid Kokoah, which is the point: the subnet
rebates the builder fee and a half in alpha for a verified track record, and refuses to subsidize risk-free volume.

What the cap does not do is pay large subsidies above fees to attract volume. If the owner wants a bigger draw, raise
`k` only together with the detection below, and read the table again: `k = 2` already pays top-tier market makers to
hedge through us.

### 5.2 Detection, on top of the cap

- **Same coldkey, same address, sub-accounts:** fills between bound addresses of one coldkey are excluded; a
  sub-account of a bound master is treated as the same miner (the master address is in the clearinghouse data).
- **Mirror score across bound addresses:** for each pair of bound addresses, the share of an address's held volume
  with an opposite-side fill in the other address in the same coin within 60 seconds and within 20% of the size.
  Above 25%, the mirrored volume is excluded; above 50%, both get `g = 0` for the window and the pair is reported.
- **Funding clustering (v1):** addresses funded from the same bridge depositor on Arbitrum, or by transfers between
  each other, form one cluster for the cap and the mirror score.
- **Collateral:** miners post the chain's miner collateral (Bittensor 11 supports it; drain rules to verify); a
  reported mirror pair loses it.
- Hedges against unbound addresses or other exchanges stay invisible, and that is fine: the cap already makes them
  unprofitable; detection only raises the cost further for the lazy versions.

### 5.3 Table

| Attack | What happens under this design |
|---|---|
| Wash trading (self or paired, instant round trips) | `h(hold)` near 0, so no volume credit; PnL is minus fees, so `g = 0`; fees still paid to Kokoah and Hyperliquid. Unprofitable. |
| Hedged pair or basket (long in A, short in B, keep the winner; the other leg bound, unbound or on another exchange) | Payout ≤ `k × builder fees` with `k = 1.5`: negative expectation for every variant in §5.1; mirror score zeroes bound pairs; registration burn and collateral per UID. |
| Lottery accounts (many small, high-leverage) | `newcomer`, 10 round trips, 7 active days, `$500` equity, per-UID burn, payout cap. Exponentially expensive to keep a winner. |
| Sybil splitting | Volume is linear; nothing gained, registration burn lost. |
| Hiding losers (realize winners, hold losers) | Equity-based returns include unrealized PnL. |
| Deposits dressed as returns | Flows removed from daily returns. |
| Trading elsewhere, token volume through Kokoah | `coverage` scales `g` down. |
| Counterparty collusion with an unbound address | Same-address and same-coldkey fills excluded in v0; mutual-counterparty share above a threshold is flagged for exclusion in v1 (both sides are visible in node data). |
| Data provider (Kokoah) manipulation | Hash-committed datasets, spot checks against public Hyperliquid data, more validators over time. |
| Validator collusion or laziness | Yuma consensus with stake, commit-reveal weights, scores reproducible by anyone from public data. |
| Lookahead | None: inputs are past on-chain events. |

## 6. Parameters

| Parameter | v0 value | Why |
|---|---|---|
| Window `W` | 30 days (90 for persistence) | long enough to drown luck, short enough to feel |
| `T_hold` | 10 min | kills instant round trips; day traders unaffected |
| `skill_raw` full score | `R = 2 × MDD` | +20% on 10% drawdown earns the full multiplier |
| MDD floor | 5% | stops near-zero-volatility ratios |
| Coverage target | 90% | "trade through Kokoah" without punishing an occasional outside fill |
| Newcomer ramp | 14 days | |
| Gates | `R > 0`, `MDD ≤ 50%`, ≥ 10 round trips, ≥ 7 active days, equity ≥ $500 | |
| `turnover_cap` | 50 × equity per day | scalpers fit; dust accounts cannot claim size |
| Per-miner cap | 20% | |
| Payout cap `k` | 1.5 × builder fees; 3 after the 90-day persistence test; deposits raise it further (§3.5) | §5.1: every zero-edge strategy nets ≤ 0 at 1.5; the stricter test makes 3 safe |
| Mirror thresholds | 60 s, 20% size; exclude above 25%, zero above 50% | catches the lazy two-address hedge |
| Registration | burn, `min_burn` ≈ 1 TAO (owner-set) | prices sybils |
| Scoring time | 00:00 UTC daily | |

All of these are owner-settable in the validator config and should be tuned on testnet with real replayed data.

## 7. Worked examples (30-day window)

| Miner | Equity | Held builder volume `V` | Return `R` | Drawdown | `g_raw` | `V × g` | Share of 4 |
|---|---|---|---|---|---|---|---|
| Small star | $5k | $2M | +30% | 8% | 1.00 | 2.0M | 1.6% |
| Whale | $2M | $400M | +6% | 10% | 0.30 | 120M | 95.8% (capped to 20% and to 1.5 × fees, rest burned) |
| Steady pro | $150k | $20M | +12% | 6% | 1.00 | 20M | 16.0% |
| Wash account | $20k | $500M nominal, ~$0 held | −0.4% (fees) | 0.5% | 0 (gate) | 0 | 0% |

Before the cap the whale takes almost everything, which is the "big weight on scale" the objective asks for; the
20% cap and the burn keep one account from being the subnet. Loosen the cap when there are enough miners that no
single one dominates. The small star leads the public leaderboard and earns 2% per dollar traded of what the whale
earns 0.3 on: skill pays per dollar, scale pays in total.

## 8. What the app gets

- A leaderboard with a verifiable method (Discover → traders), and "top miners' positions" as a signal feed later.
- Every miner is a Kokoah trader by construction; volume and builder fees follow.
- Fees → buyback and burn of alpha (policy and cadence to define; the chain's burn intent is the primitive, to
  verify), which is the value story for stakers and the reason emissions keep coming.

## 9. Open points

1. Builder attribution in the public API (certain in node data).
2. The exact burn primitive for bought-back alpha and the fees → TAO path.
3. Root's `subnet_emission_enabled` switch: criteria unknown; the "verified trader ranking" framing is the pitch.
4. Legal review of paying tokens for trading (pending for the app already).
5. The builder fee is 0 under the launch promotion; the mechanism needs a non-zero fee to have fees to cap against.
6. Whether to reserve a second mechanism (the chain allows two) for forecasting later.
7. Hyperliquid's fee tiers and maker rebates at the time of launch set the §5.1 table; re-run it before choosing `k`.
8. How sub-accounts and the master address appear in fills and clearinghouse data (for §5.2).

## 10. Next steps

1. Implement `kokoah_subnet.scoring` (pure functions: lots and hold times, daily returns with flows, drawdown,
   gates, weights, caps) with unit tests on synthetic fills.
2. Replay harness: pull public fills and equity for a set of public Hyperliquid addresses (top-traders lists) and
   look at the score distribution; tune §6.
3. Validator loop on the localnet: read bindings, score, set weights every tempo; a fake data source first.
4. Testnet: wallets, test TAO, registration, two validators, real data from the nodes.
