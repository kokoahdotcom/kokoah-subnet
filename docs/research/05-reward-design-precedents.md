# 05 - Reward design precedents for a volume + PnL + ROI miner reward

Date: 2026-10-19. Scope: what other systems learned when they paid people for trading, and the hard facts needed to size a Hyperliquid builder-fee rebate. Every number carries the date it was read or published and a source tag [S#]; the Sources list at the end has the exact URLs. "Read 2026-10-19" means the live page was fetched that day.

Design under review: a base rebate of the builder fee paid in subnet alpha on taker fills (smaller on maker fills), plus a skill bonus allocated by (volume + lambda x dollar profit) x a risk-adjusted-ROI multiplier, with per-miner caps as multiples of fees paid.

## 1. Hyperliquid facts for the math (official docs, read 2026-10-19)

**Perps fee schedule** (base rates, 14-day weighted volume; weighted volume = perps volume + 2 x spot volume; assessed daily at UTC day end) [S1]:

| Tier | 14d volume | Taker | Maker |
|---|---|---|---|
| 0 | - | 0.045% | 0.015% |
| 1 | >$5M | 0.040% | 0.012% |
| 2 | >$25M | 0.035% | 0.008% |
| 3 | >$100M | 0.030% | 0.004% |
| 4 | >$500M | 0.028% | 0.000% |
| 5 | >$2B | 0.026% | 0.000% |
| 6 | >$7B | 0.024% | 0.000% |

- Maker rebate tiers are by share of 14-day weighted maker volume: >0.5% share = -0.001%, >1.5% = -0.002%, >3.0% = -0.003%. "Maker rebates are paid out continuously on each trade directly to the trading wallet." [S1]
- Staking discounts on both sides: Wood >10 HYPE 5%, Bronze >100 10%, Silver >1,000 15%, Gold >10,000 20%, Platinum >100,000 30%, Diamond >500,000 40% (so a Diamond tier-0 taker pays 0.027%). Spot base: 0.070% taker / 0.040% maker at tier 0. [S1]
- "Sub-account volume counts toward the master account and all sub-accounts share the same fee tier. Vault volume is treated separately from the master account." Referral discounts apply to a user's first $25M of volume; referral rewards to the first $1B. Protocol fees go to HLP, the assistance fund and deployers. [S1]

**Builder codes** [S2][S4][S13]:
- Cap: "Builder fees charged can be at most 0.1% on perps and 1% on spot." Fee is set per order as `"builder": {"b": address, "f": n}`, `f` in tenths of a basis point: "if f is 10, 1bp of the order notional will be charged to the user and sent to the builder". So the perps cap is f = 100.
- "Builder codes currently only apply to fees that are collected in the quote or collateral asset ... apply to both sides of perp trades." (Both sides = buy and sell; the docs never say maker vs taker, see "could not confirm".)
- Approval: `approveBuilderFee` action with `maxFeeRate` as a percent string (e.g. "0.001%"), signed by the user's main wallet, not an agent wallet; revocable; max 10 active builder approvals per user; the builder must hold at least 100 USDC in perps account value and use standard account abstraction. Builder fees are claimed "through the usual referral reward claim process"; the ledger delta `RewardsClaim` "is for builder and referrer fees". [S2][S10]
- Builder codes override referral codes for that order; referral codes stop after $1B cumulative user volume. The staking referral program (not live) would let builders keep up to 40% of referred users' fees. [S13]
- Builder-side data: info `maxBuilderFee` (user, builder) returns the approved cap in tenths of bp; `approvedBuilders` (user) lists builder addresses; `referral` returns `builderRewards` (USDC); daily per-builder fill CSVs at `https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/{builder_address}/{YYYYMMDD}.csv.lz4` (address must be lowercase). [S2][S13]

**What a fill record contains** (`userFills`, `userFillsByTime`, WebSocket `WsFill`) [S3][S5]:
- Fields: `coin`, `px`, `sz`, `side` (B/A), `time`, `startPosition`, `dir` ("used for frontend display", e.g. "Open Long"), `closedPnl`, `hash`, `oid`, `crossed` ("whether order crossed the spread (was taker)"), `fee` ("negative means rebate"; "the total fee, inclusive of builderFee"), `feeToken`, `builderFee` ("amount paid to builder, also included in fee"; "optional and will not be present if 0"), `tid` ("unique trade id"), optional `liquidation`. TWAP fills have `hash` 0.
- The builder address is not in the fill. Only the amount is. Per-builder attribution needs the builder's own CSV feed or the order actions.
- Limits: `userFills` returns at most the 2,000 most recent fills; `userFillsByTime` at most 2,000 per response and "only the 10000 most recent fills are available"; `aggregateByTime` merges partial fills of one crossing order. Fetch continuously; the API is not an archive.

**Sub-accounts and self-trades** [S6][S7]:
- "Up to 10 sub-accounts can be created after reaching $100,000 in volume. Every additional $100M in volume enables ... 1 additional sub-account, up to a maximum of 50." Same fee tier as master; API wallets 3 + 2 per sub-account. Sub-accounts are separate addresses that sign through the master.
- Self-trade prevention: "Trades between the same address cancel the resting order instead of causing a fill. No fees are deducted, nor does the cancel show up in the trade feed." It is keyed on the address, so two sub-accounts (or two wallets) can fill each other; that is the wash-trading channel.

**Public leaderboard** (`stats-data.hyperliquid.xyz/Mainnet/leaderboard`, 39.0 MB JSON, last modified 2026-10-19 17:39 UTC) [S8]: one row per address with `accountValue` and `windowPerformances` for `day`, `week`, `month`, `allTime`, each with `pnl`, `roi` and `vlm`, plus `prize` and `displayName`. The app ranks by any of PnL, ROI or volume over those four windows. ROI is reported as a fraction (e.g. 0.0129913 = 1.3%). The file is public and refreshes continuously, so validators can cross-check miner-reported PnL against it.

**Node data** [S9][S10]: `--write-fills` "Streams fills in the API fills format to ~/hl/data/node_fills/hourly/{date}/{hour}" (so `fee`/`builderFee` as above; `deployerFee` added for HIP-3). `node_trades` records have `side_info` for buyer and seller (`user`, `start_pos`, `oid`, `twap_id`, `cloid`) and no builder field; order statuses have no builder field; `replica_cmds` stores the signed actions (`--replica-cmds-style actions` is the default), and the order action schema carries the optional `builder {b,f}`, so builder attribution per order should be recoverable from `replica_cmds` (not verified on a live node, see section 11). Default logs are ~100 GB/day.

**Points and HYPE** [S11][S12]: Season 1 ran 2023-11-01 to 2024-05-01 at 1,000,000 points/week; the L1 phase ran 2024-05-29 to November 2024 at 700,000/week; "points criteria was updated on a recurring basis" and were never published; Hyperliquid "reserves the right to modify previous point distributions". Genesis on 2024-11-29 sent 310M HYPE (31% of 1B supply) to points holders, worth >$1.6B at the $5.40 peak (Decrypt, 2024-11-29).

## 2. dYdX trading rewards

**v3 (Ethereum, 2021-2024).** 3,835,616 DYDX per 28-day epoch (25% of supply earmarked) [S17][S19]. The trader score was Cobb-Douglas in fees paid (f), average open interest (d) and staked DYDX (g): weights a = 0.67 on fees and b = 0.28 on open interest until a community vote on 2022-04-18 moved them to a = 0.8 and b = 0.15 (the stkDYDX weight was not found in a primary source; 0.05 by subtraction) [S15]. DIP-13 (2022-08-09) replaced the whole thing with `w = fees`, stating the old formula "set a high bar ... by requiring a participant to have a high Open Interest as well as lots of DYDX tokens" and encouraged open interest that was "not organic" and added "instability during volatility" [S16]. DIP-16 (2022-09-21) cut emissions 25% to 2,876,712 per epoch, calling them "excessively high relative to the growth in platform activity" (Snapshot 99.13% for) [S17]; a 2023 vote cut a further ~44-45% [S85]; DIP-29 (2023-11-12) wound them down: 1,054,795 (epoch 30), 527,398 (epoch 31), 0 from epoch 32 on 2024-01-16 [S18]. The dYdX forum review (2023-08-04) states: "There have been points in time where the Trader Rewards program paid more in rewards than was accrued in fees", i.e. the protocol paid people to trade; the effective fee discount over the preceding six months was 50-85% [S14]. No formal wash-trading incident report was found, but a reward above fees is by itself a wash-trading subsidy.

**v4 (dYdX Chain, 2023-).** Per-block rule: `A = min(C * S / p, T)` and per trader `r_i = min(C * s_i / p, T * s_i / S)`, where S is net fees paid in the block (after maker rebates), s_i the trader's share, p the DYDX oracle price, T the DYDX vested into the rewards treasury that block, and C in [0, 1] a governance constant; "Traders can no longer receive more in rewards than they pay in fees" [S14]. Official docs: "Users earn trading rewards up to, but not exceeding, 90% of a fill's net-trading-fees"; per fill, taker reward = taker volume x (taker fee rate - max maker rebate - max possible affiliate taker fee share) x C x (1 - protocol revenue share rate); maker reward = maker volume x (positive maker fees) x C x (1 - protocol revenue share rate); block rewards are "capped at the dollar equivalent of the total net trading fees" [S20]. The launch blog lists "Self-trading should not be profitable" as a design goal [S21]. C went 0.33 at launch (2023-12) [S25], to 0.9, then 0.5 (2024 vote, 81.77% for) [S24]. Nethermind's DRC (2025-09-11) reported August 2025 rewards of $229K (363K DYDX) to 5,850 wallets, 12.3% of fees, 0.24 bp of volume for small accounts and 0.05 bp for large, and proposed C = 0 in favour of the off-chain "Surge" program [S23]. The docs now say "the community has voted to set the C parameter to 0" and traders get 50% fee rebates directly [S20][S22]. Takeaway: even a reward strictly below fees ended up as a small, ignored discount; dYdX concluded it was not changing behaviour.

## 3. FCoin trans-fee mining (2018-2020)

FCoin launched in May 2018 and refunded 100% of each trading fee in its FT token, then paid 80% of daily fee revenue to FT holders; 51% of FT supply was earmarked for this "mining" [S28]. Daily volume was reported above $5.6B in June 2018 (CoinDesk via The Block) [S28]; one day's dividend reached 6,000 BTC and the founder said he personally made $150-200M in the first months [S27][S28]; Binance's CEO called it a "reverse ICO" [S26]. FT fell about 95%; in April 2019 refunds switched to the traded coin with 20% paid at once and 80% locked a year [S26]. On 2020-02-17 the exchange said it could not pay 7,000-13,000 BTC ($67-130M) it owed, blaming "a data error + a decision error" and dividend data that was "polluted" and "multiplied" [S27][S28]. The mechanism paid out more than fees (refund 100% + dividend) so volume was manufactured to mint tokens; once FT's price fell, mining stopped and the token had no buyer.

## 4. Points programs and wash trading

- **Blur (2023).** Season 2 (2023-02-21) put 300M BLUR behind a "loyalty score" (100% only if you list exclusively on Blur) plus listing and bidding points [S29]. Season 1 recipients included clusters of "intertwined" wallets manipulating the airdrop [S30]; Nansen counted about 16.7k wash traders among 213k lifetime users and called one large Season 2 holder's 1,010 sales / 991 buybacks in 48 h "one big wash trade" [S31][S30]; the top two farmers reportedly held about a third of Season 2 points [S32]. Fix adopted: loyalty and bid-depth weighting rather than raw volume, but no published formula.
- **Hyperliquid (2023-2024).** Criteria never published and changed weekly [S11]; no official statement on sybil or wash filtering was found [S11][S12]. Lesson is in the opacity: an unpublished, re-tunable rule is what kept farmers from optimising against it.
- **Aevo (2024).** Rewards = share of weekly AEVO proportional to "boosted volume" = order value x base boost (1x-4x by trailing 7-day volume, decaying) x lucky boost (10x-100x) [S34]. Volume went from ~$100M to ~$4.5B/day and collapsed; the founder said users "were inflating volumes to over $1 billion to qualify for airdrops" before a snapshot [S35]. From epoch 6 weekly rewards were cut from 1.1M to 400k AEVO [S36]. Current docs (read 2026-10-19): 1,000,000 AEVO/week pro rata to volume, market makers excluded, "wash trading zero tolerance" naming self-trading and "circular trading between related wallets", forfeiture plus permanent ban, no appeals [S33].
- **Lighter (2025).** Season 1 ended 2025-09-30; Season 2 paid 200,000 points/week to retail every Friday on volume, open interest, fundings, liquidations/deleverages and PnL; "points are not linear with the metric", markets are weighted unequally, metrics undisclosed, "up to 10 accounts per user" tolerated, sybil detection "automated and semi-automated" [S37][S38]. Market makers get 20% (50,000/week) with volume score = volume + max(0, (volume - 2.5B) x 0.25), a convex bonus for size [S39]. On 2025-12-23 Lighter ran a mass sybil/self-trade/wash screen and redistributed the slashed points; Season 2 ended 2025-12-26 [S40][S39].
- **Paradex.** Seasons: ~100M XP (2024-02-01 to 2025-01-02), ~236M XP (2025-01-03 to 2026-01-29), Season 3 from 2026-02-01 at 4,000,000 XP/week with a "new XP earning mechanic" not yet documented; linking X/Discord is used to separate users from sybils [S41].
- **Backpack.** Season 3 (2025-09-11): "one metric dictates your rank: Volume." Season 4 (2025-11-20): weekly points, Thursday snapshots, KYC required, ten weeks [S42][S43]. No anti-wash rule published.
- **Aster (2025).** On 2025-10-05 DefiLlama delisted Aster's perps volume after it tracked Binance's almost exactly and the venue exposed no maker/taker data; ASTER fell ~10% [S45]. On 2025-10-18 Aster re-based Stage 3 points on "fee contributions and liquidity provision", updated hourly, banned wash trading and reserved the right to "adjust, freeze, or revoke" points [S44].
- **Pattern across venues (2026 commentary):** the public tell is volume relative to open interest; venues with turnover many multiples of deposits are hosting points farming [S84]. Fixes seen: pay on fees rather than notional (Aster, dYdX v4), weight open interest and holding time (Lighter, Paradex, dYdX v3), exclude market makers (Aevo, Binance, Bybit), KYC (Backpack), undisclosed and re-tuned weights (Hyperliquid, Lighter, Paradex), retroactive sybil slashing with redistribution (Lighter), zero-tolerance bans (Aevo).

## 5. Exchange trading competitions

- **Binance.** 2019-12-31 futures tournament: `USDT Profit = final balance (incl. unrealized) - initial balance + withdrawals - deposits - referral bonuses`; `ROI = Profit / (initial balance + time-weighted deposits)`, `time-weighted deposit = deposit x remaining seconds / 86,400`; 100 USDT minimum for the daily ROI league; "Binance reserves the right to disqualify trades that are deemed to be wash trades, illegal bulk registration account, self-dealing or display attributes of market manipulation" [S46]; the 2019-09 "Battle" ranked by volume with wash trades excluded [S47]. Traders League 2024 ($10M pool): solo league by ROI, team league by PnL, $500 minimum volume, regular and VIP 1-6 only [S48]; 2025 ($3M futures pool): market makers excluded, regional teams by KYC country [S49].
- **Bybit WSOT.** 2019 edition ranked by "profit/loss ratio, regardless of trading volume". 2024 allowed one main account plus up to four sub-accounts [S54]. 2025 (2025-08-27 to 2025-09-15, $8M main pool, 71,765 sign-ups in 24 h) ranks squads by PnL%, individuals by profit, regions by volume and a daily PnL% board; accounts under $50,000 volume are excluded [S50][S52][S53]; the on-chain event bars sub-accounts, institutions and market makers [S51].
- **OKX Global Trading Championship (2025-11-20 to 2025-12-21).** Top 20 teams by P&L%, top 15 individuals by absolute profit, top 30 new traders by P&L%; at least 30,000 USDT perp volume to be ranked and 200 USDT in the main account; pool up to 1M USDT (team 40%, individual 20%, new traders 22%); within a team the leader gets 20%, top 10 traders 35%, others 40%; disqualification for "laundering trades or wash trades", multiple accounts, borrowed accounts, false KYC; block and spread trades do not count; sub-accounts excluded [S55].
- **Bitget KCGI 2023 (2023-09-30 to 2023-10-30).** Individual `ROI = profit / (initial net value + total deposits)`; rewards need 30,000 USDT volume and positive PnL; 300 USDT minimum assets; individual split 40/20/10% and 4th-10th share 30% capped at 10% each; team PnL = sum of top ten members, one team at most 50% of the pool, captain 10%; immediate disqualification for "wash trading, improper matched orders, bulk registration, and self-dealing", for placing and cancelling maker orders too often, for all accounts sharing an IP, for API trading, copy trading, market makers; sub-accounts not allowed [S56].
- **Others.** MEXC requires 20,000 USDT volume for the PnL board and 6,000,000 USDT for the PnL-rate board [S57]; Kraken excludes sub-account results from rankings [S58]; BingX counts only positions held over one minute [S83]. No exchange was found that documents a hedge-across-accounts detector; they rely on IP/KYC clustering, volume floors, sub-account bans and discretionary disqualification.

## 6. Prop-firm rules

- **FTMO (read 2026-10-19).** 2-step: 10% then 5% profit target, 5% max daily loss, 10% max loss (static), 4 minimum trading days; 1-step: 10% target, 3% daily loss, 10% end-of-day trailing loss, and a consistency rule for orders from 2026-10-12: "your best day can't exceed 50% of total profit" [S59]. Forbidden practices (page modified 2026-10-12): manipulative trades alone or with others "across connected accounts", "accounts held with other providers" and accounts of other Program Group members; "simultaneously entering into opposite positions" except within one account; opposite positions in "highly correlated instruments" used to spread profit across days to dodge the consistency rule; sanctions are removal of trades, disqualification and forfeiture of rewards [S60]. FTMO does not say how it detects any of this.
- **Topstep.** Combine consistency target 55% (page 2026-09-17): "Your best day's profit must be at or below 55% of your Profit Target"; Express Funded "Consistency" payout path: largest day / total net profit <= 40% with at least 3 trading days; targets $3,000/$6,000/$9,000 on $50K/$100K/$150K [S61]. Single-profile policy; trade copiers across one's own accounts are allowed [S62]. Third-party summaries describe a ban on long/short the same or correlated instrument across accounts with auto-liquidation and closure, but no official page was reachable [S65].
- **The5ers (page 2026-07-28).** Bans "Hedge Arbitrage", defined as when a trader "simultaneously buys and sells the same currency pair on different accounts", "Reverse Arbitrage", coordinated opposite positions across accounts at different firms, copy trading with other traders, account sharing, "artificially spreading profits across days"; penalty: termination, permanent ban, no refund or profit [S63].
- **FundedNext (page 2026-09-22).** "hedging is allowed only within the same account. Hedging across multiple accounts is prohibited"; the stated detection heuristic is behavioural: "taking the risk of a full or close to daily loss limit in one trade" is treated as a suspected multi-platform hedge; copy trading only between one's own accounts; device sharing forbidden; "FundedNext actively monitors trading behaviour across all accounts" [S64]. Third parties report a 40% consistency rule on Rapid accounts.
- Common thread: profit target + daily loss + max drawdown + consistency (one day <= 40-55% of profit) + explicit cross-account hedge ban, enforced by behaviour heuristics and account/device/KYC linkage rather than by fill matching. A drawdown-dominant score is their equivalent of a risk-adjusted multiplier.

## 7. Bittensor precedents: Taoshi/Vanta (SN8) and Greevils (ex-SN58)

- **SN8 payout rule (docs read 2026-10-19, "active December 2025+").** Debt ledger: Needed Payout = previous week's PnL scaled by penalties (USD); Actual Payout = emissions received this week (USD); weights are proportional to Remaining = Needed - Actual, aiming to finish by midnight Sunday UTC; Average Daily PnL carries 100% weight, Calmar/Sharpe/Omega/Sortino 0%; recency weighting (first 10/30/70 days = 40/70/87% for PnL); dust floors 3x/2x/1x for main/probation/challenge; unassigned weight goes to burn UID 229 (mainnet) [S66][S67]. Risk-profile penalties hit averaging into losers, >50% of available leverage, and non-TWAP order timing; drawdown elimination at 5% intraday / 8% end-of-day (main). Challenge: 90 days, 61 trading days, +10% (crypto) and top-25 rank per asset class [S66].
- **Collateral.** Each Theta unlocks $500 of simulated capacity, minimum 300 and maximum 1,000 Theta; withdrawals are slashed in proportion to current drawdown; an eliminated miner's collateral "is slashed and burnt" [S66]. P22 (mainnet V1 2025-07-21, V2 2025-08-04) priced Theta at $175 (1,429 Theta for $250K), 2% drawdown leaves 90% withdrawable, 6% leaves 70%, >10% drawdown = elimination with 50% slashed [S69].
- **Emissions burn, the "30x" cap (P20, integrated 2025-05-19).** "Today, top miners can receive the equivalent of $3M/year in emissions" on simulated returns, "overpaying by up to 100x"; Adjusted payout weight = annualized dollar returns of the top miner x 30 / total dollar emissions per tempo; Burn % = 1 - adjusted / full weight; example $4.80 x 30 / $2,700 = 0.05 vs 0.15 full weight = 67% burned [S68]. Note section 9: a burn routed to an owner hotkey now lowers the subnet's TAO share.
- **Copy detection (P25, integrated 2025-08-22).** Correlation of two miners' rasterised net leverage per pair; above threshold, 10-day probation with a defence; code kept private so it cannot be "exploited" [S70].
- **Greevils (real Hyperliquid perps through a builder code, repos last pushed 2026-07, gone from SN58 by 2026-10).** Every fill had to carry the ~5 bp builder fee or the account was disqualified; eligibility 60 days since first trade, 40 active days, lifetime executed value >= 25x time-weighted equity, sum of daily returns >= 1.5%, $1,000 equity floor; utility `U = ln(1+R) + 0.08 ln(1+X/1000) - 0.308 (D/0.10)^2 - 0.15 softplus(V/0.02 - 1) - 0.75 min(1,(1+non_profit_days)/14) max(0, K - 0.14)` with R compounded return, X dollar profit, D max drawdown, V downside volatility, K Herfindahl concentration of profit days; human pool = min(50%, 0.1 x human PnL$ / emission$); no wash or cross-account rule at all [S71][S72]. It is the closest precedent to this design and it did not retain enough miners to keep its slot.
- No other live subnet was found (search 2026-10-19) that pays for real exchange volume or fees; Tenex (ex-SN67) was an LP protocol and ended with a reported $2.8M loss, Candles (SN31) is a forecasting subnet [S72].

## 8. Theory, briefly

- **Sybil-proof allocation.** Chen, Papadimitriou and Roughgarden (2019) define an allocation rule as sybil-proof if replacing one participant of size h_i by a set S with total size <= h_i never raises S's total reward above x_i(h); Theorem 1: the proportional rule x_i = h_i / sum h_j is the unique rule that is non-negative, strongly budget-balanced, symmetric, sybil-proof and weakly collusion-proof; "generalized proportional" rules c(total) x h_i / sum h_j stay sybil-proof; proportional-to-square-roots is collusion-proof "However, it is not sybil-proof"; proportional-to-squares is sybil-proof but "not even weakly collusion-proof" [S73]. Translation: anything concave in a splittable quantity (per-account caps, square roots, diminishing tiers, lottery entries per account) pays account-splitters; anything convex (Lighter's MM bonus above $2.5B, winner-take-most leagues) pays mergers and colluders; only reward linear in the splittable quantity is neutral. dYdX v3's f^0.67 d^0.28 is degree 0.95 in the two splittable inputs (fees and open interest), so k-way splitting earned about k^0.05 more; after 2022-04-18 a + b = 0.95 again (our derivation, not in a source).
- **Pay no more than the fee.** No paper states it as a theorem, but dYdX v4 (C <= 1 on net fees, maker rebate subtracted) is the operational form [S20][S14]: a round-trip self-match costs both legs' exchange fees plus spread/slippage, so a rebate strictly below the net fee on each leg makes wash volume a guaranteed loss. On Hyperliquid at tier 0 with a 2.5 bp builder fee the self-match round trip costs 4.5 + 1.5 + 2 x 2.5 = 11 bp of notional; at tier 6 with a -0.3 bp maker rebate it costs 2.4 - 0.3 + 5 = 7.1 bp (fees read 2026-10-19 [S1]).
- **Wash trading is the default, not the exception.** Cong, Li, Tang and Yang (Management Science 2023) estimate wash trading above 70% of reported volume on unregulated exchanges across 29 venues [S74]; Victor and Weintraud (WWW 2021) found $159M of wash volume on IDEX and EtherDelta with "self-trades and two-account structures" the most common forms [S75]; Messias, Yaish and Livshits (2023, rev. 2025) found up to 66% of airdropped tokens sold immediately and name "airdrop farmers" and sybil vulnerability as the main failure [S76].

## 9. Bittensor chain detail: `MinerBurned` (RaoFoundation/subtensor main, read 2026-10-19)

- Rule in `run_coinbase.rs`: during `distribute_dividends_and_incentives` the chain builds `owner_hotkeys` = every hotkey in `OwnedHotkeys` of the subnet owner's coldkey that holds a UID on the subnet, with `SubnetOwnerHotkey` inserted first; for each incentive entry `if owner_hotkeys.contains(&hotkey)` the alpha is withheld and, per `RecycleOrBurn`, either `recycle_subnet_alpha` (Recycle) or `burn_subnet_alpha` (Burn, and also when unset); then `MinerBurned[netuid] = withheld_incentive / total_incentive` is stored. Code comment: "Miner emission directed to an owner (immune) hotkey is withheld from miners whether it is recycled or burned. Count both toward the withheld proportion so the emission penalty cannot be dodged by choosing Recycle" [S79].
- Effect next tempo in `subnet_emissions.rs` / emissions.mdx: `burn_adjusted_share_i = demand_share_i x (1 - miner_burned_i) / sum(demand_share x (1 - miner_burned))`, then the emission gate `gate_i = 1 / (1 + (theta / burn_adjusted_share_i)^h)` with h = 3 and theta = the 32nd-highest positive adjusted share, recomputed every 360 blocks; "Withheld incentive counts whether it is recycled or burned, so changing RecycleOrBurn cannot bypass the scaling" [S77][S80].
- Other sinks: the check is membership in the owner coldkey's hotkeys only. Weight sent to any UID not owned by the owner coldkey is not counted as `MinerBurned`, but it is not burned either; it is paid to that hotkey. So there is no on-chain way to burn miner alpha without losing TAO share, and off-chain "burn UIDs" that a third party controls are just payments to that party. `recycle` returns alpha to the unissued pool (re-emittable, pushes halvings out); `burn` destroys it and it "stay[s] counted in issuance forever" [S77].
- Emission weight vector `[burn, recycle, owner, validator, miner]` defaults to `[0, 0, 0.18, 0.41, 0.41]`; burn and recycle slots are reserved and must be 0 in v1; owner 0-0.33, validator <= 0.50, miner >= 0.33; changeable once per 50,400 blocks; "If an epoch has zero total miner incentive, the miner portion goes to validators" [S78][S82]. `owner_immune_neuron_limit` (default 1, range 1-10) only protects owner neurons from pruning [S81].

## 10. Lessons for our design

1. Pay rebates strictly below the net fee on every leg, dYdX-style: `reward <= C x (fee paid - maker rebate received - builder fee share)` with C < 1, so a self-match is always a loss. Hyperliquid's own fees give 7-11 bp of round-trip cost to lean on [S20][S1].
2. Make the base rebate exactly linear in fees paid, not in notional, and give maker fills a rebate no larger than the maker fee actually paid (0 at tier 4+, negative at MM tiers); otherwise the maker leg of a two-account wash is subsidised [S1][S20].
3. Keep the skill bonus homogeneous of degree one in anything splittable (volume, dollar profit); every per-account cap, lottery or tier must be converted into a per-fee or per-collateral cap, or it pays sybil splitting (Chen et al. Example 5) [S73].
4. Do not let the ROI multiplier be account-level alone: ROI on a tiny account is the lottery-farm vector. Weight it by equity or by fees paid, as Greevils did with a $10k denominator floor and a 25x turnover-to-equity gate, and as Bitget/OKX do with 30,000 USDT volume floors [S71][S56][S55].
5. Cap total reward per miner as a multiple of builder fees paid in the window, and cap the top miner against the pool, like Taoshi's 30x-of-returns burn; Taoshi found a 100x overpayment before adding it [S68].
6. Measure dollar profit from fills marked at oracle/mark price, not from `closedPnl`, so an off-market self-trade between two accounts moves no credited profit; both legs' `closedPnl` sum to about zero, which is itself a detectable signature [S3][S75].
7. Link accounts before scoring: same master/sub-account tree, same API wallet, same builder approval cluster, same IP/device, mirrored opposite positions in the same or correlated coins within seconds. Prop firms ban exactly this ("hedge arbitrage"), exchanges disqualify same-IP clusters and sub-accounts [S63][S64][S56].
8. Add a consistency gate: one day's profit <= 50-55% of the window's profit (FTMO, Topstep); it removes single-lucky-trade farms and "spreading profit across days" tricks [S59][S61].
9. Score on drawdown-dominant risk (max drawdown, downside volatility, profit-day concentration) rather than Sharpe-style ratios; Taoshi weights every ratio 0% after two years of re-tuning and Greevils used D, V and a Herfindahl term [S67][S71].
10. Require tenure and turnover before any skill bonus: 60 days and 40 active days (Greevils), 61 trading days (Taoshi), $50,000 volume (Bybit), positive PnL plus 30,000 USDT (Bitget) [S71][S66][S50][S56].
11. Charge collateral or a registration cost that scales with capacity (Taoshi: 300-1,000 Theta, $500 per Theta, slashed on drawdown); Bittensor registration alone is too cheap to stop account farms [S66][S69].
12. Keep a retroactive slashing clause and use it in public, as Lighter (2025-12-23) and Aevo (zero tolerance, no appeals) do; publish the rule, not the weights (Hyperliquid, Lighter, Paradex all keep weights private) [S40][S33][S11][S38].
13. Watch volume/open-interest and volume/equity ratios per account and per cluster; they are the public tell for points farming and are available from `startPosition` and account value [S84][S3].
14. Budget on fee revenue, not on token price: FCoin paid 100% refund + 80% dividend and died when FT fell; dYdX v3 paid more than fees and cut emissions four times before zeroing them [S26][S14][S18].
15. Never route miner weight to an owner-coldkey hotkey to "burn" unawarded emission; it reduces the subnet's TAO share next tempo through `MinerBurned`. Route unawarded weight to a non-owner sink only if the team accepts that it is a payment, or leave it to the chain's recycle path [S79][S77].

## 11. Could not confirm

- Whether a builder fee is charged on maker (`crossed = false`) fills. The docs say "both sides of perp trades" and never mention maker/taker; a sample of 57,409 recent fills from 33 addresses in the first rows of the leaderboard file (2026-10-19) contained no builder fees at all, and the public `builder_fills` CSV for the builder address shown in the docs returned HTTP 403.
- Whether `replica_cmds` files preserve the `builder {b,f}` field of order actions (inferred from the action schema; not checked on a node).
- Whether self-trade prevention applies between a master account and its sub-accounts (the doc says "the same address").
- The stkDYDX exponent in dYdX v3 (0.05 by subtraction) and the date/ID of the governance vote that set C = 0 (docs say it passed; the DRC was posted 2025-09-11).
- The exact 51% FT allocation and the $5.6B/day FCoin volume (both via The Block's account of CoinDesk; CoinDesk page not fetched).
- Topstep's official cross-account hedging text (third-party only); The5ers' $500k copy-trading threshold; FundedNext's 40% consistency figure (third-party).
- Hyperliquid's own sybil point reductions during the points seasons (not documented officially).
- Bybit WSOT 2025 wash-trading and multi-account clauses (official rules page timed out twice).
- Lighter's and Paradex's actual weighting formulas (undisclosed by design).
- Whether Taoshi's burn UID 229 is an owner-coldkey hotkey (if so, its burn now reduces SN8's TAO share under the `MinerBurned` rule).

## Sources

Entries marked (snippet) were read through search-result snippets, not fetched in full; all others were fetched on 2026-10-19.

- [S1] https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- [S2] https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes
- [S3] https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- [S4] https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
- [S5] https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- [S6] https://hyperliquid.gitbook.io/hyperliquid-docs/trading/sub-accounts
- [S7] https://hyperliquid.gitbook.io/hyperliquid-docs/trading/self-trade-prevention
- [S8] https://stats-data.hyperliquid.xyz/Mainnet/leaderboard
- [S9] https://github.com/hyperliquid-dex/node
- [S10] https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas
- [S11] https://hyperliquid.gitbook.io/hyperliquid-docs/points
- [S12] https://decrypt.co/294067/hyperliquid-airdrop-1-6-billion
- [S13] https://hyperliquid.gitbook.io/hyperliquid-docs/llms-full.txt
- [S14] https://dydx.forum/t/dydx-v3-vs-v4-new-trader-rewards-overview/881
- [S15] https://dydx.foundation/blog/epoch-11 (snippet)
- [S16] https://github.com/dydxfoundation/dip/blob/master/content/dips/DIP-13.md
- [S17] https://github.com/dydxfoundation/dip/blob/master/content/dips/DIP-16.md
- [S18] https://github.com/dydxfoundation/dip/blob/master/content/dips/DIP-29.md
- [S19] https://enigma-securities.io/wp-content/uploads/2022/01/dydx-261.pdf
- [S20] https://docs.dydx.xyz/concepts/trading/rewards/trading-rewards
- [S21] https://www.dydx.xyz/blog/v4-rewards-and-parameters
- [S22] https://docs.dydx.xyz/concepts/trading/rewards
- [S23] https://dydx.forum/t/drc-simplifying-incentives-ending-protocol-level-trading-rewards-and-setting-c-to-0/3988
- [S24] https://dydx.forum/t/analysis-and-proposals-on-dydx-chain-and-dydx-tokenomics-reduce-the-trading-rewards-c-constant-from-0-90-to-0-5/3116
- [S25] https://www.theblock.co/amp/post/264937/dydx-starts-distributing-trading-rewards (snippet)
- [S26] https://cointelegraph.com/features/fcoin-blames-poor-auditing-for-shutdown-but-others-suspect-exit-scam
- [S27] https://www.financemagnates.com/cryptocurrency/exchange/trans-fee-mining-bubble-finally-popped-as-fcoin-goes-bankrupt/
- [S28] https://www.theblock.co/post/56191/cryptocurrency-exchange-fcoin-expects-to-default-on-as-much-as-125m-of-users-bitcoin
- [S29] https://decrypt.co/121887/blur-to-airdrop-300m-in-extra-tokens-to-loyal-nft-traders
- [S30] https://decrypt.co/121326/nft-traders-flex-blur-airdrops-most-tokens (snippet)
- [S31] https://research.nansen.ai/articles/blur-a-leading-opensea-rival-but-will-it-last (snippet)
- [S32] https://tpan.substack.com/p/253-blurs-season-2-airdrop-strategy (snippet)
- [S33] https://www.aevo.xyz/docs/trading-and-staking-rewards/trading-campaigns/trading-rewards.md
- [S34] https://blockchain.news/news/aevo-launches-incentive-program-to-boost-trading-and-staking
- [S35] https://www.cryptopolitan.com/aevos-volume-drop-triggers-allegations/ (snippet)
- [S36] https://www.chaincatcher.com/en/article/2120868 (snippet)
- [S37] https://docs.lighter.xyz/points-program.md
- [S38] https://docs.lighter.xyz/points-program/retail.md
- [S39] https://docs.lighter.xyz/points-program/market-makers.md
- [S40] https://coinfomania.com/lighter-xyz-sybil-filtering-lit-airdrop-premarket/ (snippet)
- [S41] https://docs.paradex.trade/docs/xp-referrals/xp
- [S42] https://learn.backpack.exchange/blog/backpack-points-season-3-is-live
- [S43] https://learn.backpack.exchange/blog/backpack-season-4
- [S44] https://phemex.com/news/article/aster-dex-updates-trading-points-rules-bans-wash-trading-27850
- [S45] https://www.theblock.co/post/373458/asters-token-drops-10-after-defillama-head-raises-wash-trading-concerns-delists-perpetuals-data (snippet)
- [S46] https://www.binance.com/en/support/articles/360038024992
- [S47] https://www.binance.com/en/support/articles/360032888452
- [S48] https://www.aap.com.au/aapreleases/cision20241008ae25583 (snippet)
- [S49] https://aapnews.aap.com.au/news/cision20250602AE00558 (snippet)
- [S50] https://www.bybit.com/en/learn/trading/what-is-bybit-wsot-main-competition-squad-showdown (snippet)
- [S51] https://www.bybit.com/en/wsot2025/onchain/
- [S52] https://www.bybit.com/en/press/post/bybit-reshapes-wsot-2025-with-10-million-usdt-prize-pool-zero-barriers-to-entry-bltb04008ce207cae30 (snippet)
- [S53] https://aapnews.aap.com.au/news/cision20250908AE67863 (snippet)
- [S54] https://decrypt.co/286366/bybits-world-series-of-trading-wsot-2024-with-dex-integration-is-now-open-for-registration-offering-over-10-million-usdt-in-rewards (snippet)
- [S55] https://okx.com/ul/2B2pjG
- [S56] https://www.bitget.com/support/articles/12560603796046
- [S57] https://www.mexc.com/support/articles/17827791521535 (snippet)
- [S58] https://support.kraken.com/gb/articles/15478742641300 (snippet)
- [S59] https://ftmo.com/en/trading-objectives/
- [S60] https://ftmo.com/en/forbidden-trading-practices/
- [S61] https://help.topstep.com/en/articles/8284208
- [S62] https://help.topstep.com/en/articles/8284197
- [S63] https://www.the5ers.com/faqs/prohibited-trading-practices/
- [S64] https://help.fundednext.com/en/articles/8020351-what-are-the-restricted-prohibited-trading-strategies
- [S65] https://proptradingvibes.com/blog/topstep-hedging-rules (snippet)
- [S66] https://github.com/taoshidev/vanta-network/blob/main/docs/miner.md
- [S67] https://github.com/taoshidev/vanta-network/blob/main/docs/validator.md
- [S68] https://docs.taoshi.io/tips/p20/
- [S69] https://docs.taoshi.io/tips/p22/
- [S70] https://docs.taoshi.io/tips/p25/
- [S71] https://raw.githubusercontent.com/greevils-ai/greevils-validator/main/incentive.md
- [S72] docs/research/03-trading-subnets-landscape.md (this repo, 2026-10-19)
- [S73] https://arxiv.org/abs/1909.10645
- [S74] https://www.nber.org/papers/w30783
- [S75] https://arxiv.org/abs/2102.07001
- [S76] https://arxiv.org/abs/2312.02752
- [S77] https://raw.githubusercontent.com/RaoFoundation/subtensor/main/docs/concepts/emissions.mdx
- [S78] https://raw.githubusercontent.com/RaoFoundation/subtensor/main/docs/concepts/emission-weights.mdx
- [S79] https://raw.githubusercontent.com/RaoFoundation/subtensor/main/pallets/subtensor/src/coinbase/run_coinbase.rs
- [S80] https://raw.githubusercontent.com/RaoFoundation/subtensor/main/pallets/subtensor/src/coinbase/subnet_emissions.rs
- [S81] https://raw.githubusercontent.com/RaoFoundation/subtensor/main/docs/hyperparameters/owner-immune-neuron-limit.mdx
- [S82] https://raw.githubusercontent.com/RaoFoundation/subtensor/main/docs/hyperparameters/miner-emission.mdx
- [S83] https://exchangesinfo.digitalpress.blog/bingx-exchange-offers-best-trading-events/ (snippet)
- [S84] https://decentralised.news/perp-dex-wars-hyperliquid-challengers-ranked-2026 (snippet)
- [S85] https://www.binance.com/en-BH/square/post/16136560445905 (snippet)
