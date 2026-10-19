# Trading, forecasting and market-intelligence subnets on Bittensor: landscape and design lessons

Date: 2026-10-19. Status: research note for the kokoah-subnet incentive design. Numbers carry the date they were read and a source tag `[Sn]` that resolves in the Sources list at the end. "Could not confirm" means exactly that.

## 1. Summary

- Nine subnets were found that trade, forecast or sell market intelligence. Two are healthy and actively maintained: **SN8 Vanta** (ex Proprietary Trading Network, Taoshi) and **SN50 Synth**. Two are alive but small: **SN6 Numinous** (ex Infinite Games) and **SN123 MANTIS**. **SN41 Sportstensor** is alive but its public code is archived. Four lost or gave up their netuid during 2026: **Precog** (SN55 is now NIOME), **Tenex** (SN67 is now Harnyx, after a reported $2.8M LP loss), **Greevils** (SN58 is now Attune) and **Efficient Frontier** (SN53 is now Engy). Price aggregators still show the old names on those tickers, which is why sources conflict on netuids. [S1][S2][S3][S4]
- Three subnets already touch Hyperliquid: Vanta uses the Hyperliquid order book, funding rates and USDC perps as its reference venue and can mirror a real Hyperliquid address's fills as signals; Synth scores HYPE and all equity/commodity assets against Hyperliquid candles since July 2026; Greevils (now gone from SN58) made miners trade real Hyperliquid perps through a builder code. No subnet was found that consumes verified node data (L1 state, OI, funding) as a product, and none executes through Hyperliquid vaults. [S5][S6][S7][S8]
- Only Synth claims to be profitable (Q2 2026) and has announced a buyback ($100k over 8 weeks). Vanta has three product lines but no disclosed revenue. Nobody else reports revenue. [S9][S10]
- The clearest design lessons: (1) proper scoring rules (CRPS, market-relative squared error) are the only mechanisms that stayed stable; (2) simulated-trading leaderboards were re-tuned 26 times in two years and ended up paying on raw weekly PnL with a weekly debt ledger; (3) every signal subnet had to build anti-copy machinery (timelock encryption, correlation-based plagiarism detection, sandboxed agents); (4) real-money execution subnets had verifiable ground truth but thin participation and both left their slots within about a year.

## 2. Method

Web search plus direct reads of each subnet's GitHub README, docs and config files (raw.githubusercontent.com), the GitHub REST API for stars/forks/last commit (read 2026-10-19), the CoinGecko markets API for the `bittensor-subnets` category (read 2026-10-19 16:48 UTC, 117 tokens) and taomarketcap subnet pages (read 2026-10-19, no timestamp shown on page). taostats, tao.app and bittensor.ai either render client-side or returned HTTP 403, so emission shares could not be read; the only emission proxy is taomarketcap's "owner/day" field. Fetched content was treated as data.

## 3. Subnet profiles

### 3.1 SN8 Vanta Network (Taoshi), formerly Proprietary Trading Network (PTN)

- **What miners produce.** LONG/SHORT/FLAT signals for forex, crypto, equities and commodities pairs, posted to a local REST server (`POST /api/submit-order`, port 8088) that the miner runs; order types MARKET, LIMIT, STOP_LIMIT, BRACKET, trailing stops; 5-second cooldown per pair; one open position per pair. Trades are simulated by validators, not executed. [S11]
- **Venue and Hyperliquid.** Crypto, commodities and index pairs are Hyperliquid USDC perps and trade 24/7; "Hyperliquid-sourced" pairs pay live Hyperliquid funding instead of the flat carry fee (10.95%/yr crypto, 3%/yr forex); native USD crypto pairs and native XAU/XAG are blocked in favour of the Hyperliquid USDC equivalents; a subset of equities (NVDA, AAPL, COIN, MSTR) is traded as Hyperliquid equity perps. Validators run a Hyperliquid order-book websocket (`wss://api.hyperliquid.xyz/ws`) with REST fallback and may use SOCKS5 proxies to exceed Hyperliquid's 10 addresses per IP; simulated slippage walks the L2 book. Other feeds: Massive (ex Polygon.io, paid), Tiingo (paid), Databento (optional). [S5][S11][S12][S13]
- **Real Hyperliquid accounts.** "Entity miners" (max 10 entities) run subaccounts; an HL-linked subaccount is created with an `hl_address` and "trades detected on Hyperliquid for the given hl_address will be automatically forwarded as Vanta signals" (`max_hl_traders: 100` in the status payload). HL-linked subaccounts trade at reduced leverage during the challenge period and have no "pro" track. [S14][S15]
- **Scoring today (docs dated Dec 2025 onward).** Debt-based: the previous week's PnL scaled by penalties is the "needed payout"; emissions already received are the "actual payout"; weights are proportional to the remaining debt and target completion by midnight Sunday UTC; ledgers checkpoint every 12 h, weights set every 5 min with commit-reveal (360-block epochs). Metric weights: Average Daily PnL 100%; Calmar, Sharpe, Omega, Sortino and a t-statistic are computed but weighted 0%. Recency weighting: last 10/30/70 days carry 40/70/87% for PnL. Dust floors: main competition 3x dust, probation 2x, challenge/plagiarism 1x; weight not assigned goes to a burn UID (229). [S11][S12]
- **Lifecycle rules.** Challenge period up to 90 days: at least 61 trading days, cumulative return above 10% (crypto/equities/commodities) or 8% (forex), rank at or above the 25th miner of the asset class (`CHALLENGE_PERIOD_PERCENTILE_THRESHOLD = 0.75`); probation 90 days; elimination at 5% intraday drawdown from day-open equity or 8% end-of-day drawdown from the high-water mark (5%/5% in challenge); 60 days without an order; immunity 4 h. Eliminated hotkeys are blacklisted forever. [S11][S16]
- **Anti-copy.** A private "plagiarism microservice" (P25, integrated 2025-08-22) scores miners hourly; validators poll `plagiarism.ultron.ts.taoshi.io`. Config constants (read 2026-10-19): 2-minute matching resolution, up to 60 lags, 10-day lookback, follower similarity threshold 0.75, reporting threshold 0.8, max tolerated score 0.9, minimum follow lag 10 s, 14-day review before elimination; evidence is the correlation of two miners' rasterised net leverage per pair; accused miners get a 10-day probation and may submit a defence. Detection code is kept private so miners cannot tune around it. [S16][S17][S18]
- **Collateral and sybil cost.** P22 (mainnet V1 2025-07-21, V2 2025-08-04): deposits in Theta (the SN8 alpha), initially 1,429 Theta for $250k simulated capital at a fixed $175/Theta; slashing scales with drawdown, elimination slashes 50%. Current miner docs: 300 to 1,000 Theta deposit, each Theta unlocks $500 of capacity, registration under 1 TAO. P20 (2025-05-19) burns a share of emissions so top-miner payout is capped at 30x annualised return (example: 67% burned), because top miners could otherwise earn about $3M/yr on $100k simulated accounts. [S11][S19][S20]
- **Requirements.** Miner: 2 vCPU, 8 GB, CPU only. Validator: 4 vCPU, 16 GB, 1 TB disk, 1,000 SN8 alpha staked, paid Massive and Tiingo subscriptions. [S11][S12]
- **Revenue / product.** Request Network (sell trade data; "contact Taoshi"), Glitch Financial SaaS (Oct 2024), Vanta Trading (launched 2026-02-17: simulated prop-firm evaluation, one step, 8% profit target, 100% profit split, fees $199/$349/$549 for $25k/$50k/$100k, launch pricing $149/$249/$349, payouts in USD via Stripe). No revenue figures published. Kraken listed SN8 on 2026-07-16. 0xMarkets (2025-09-11, Base) is a separate Taoshi forex DEX fed by a Bittensor subnet for liquidity. [S21][S22][S23][S24]
- **Market and activity (2026-10-19).** CoinGecko: $8.06, market cap $44.4M, 7th of 117 subnet tokens; taomarketcap: τ156.27K market cap, owner share τ38.83/day. GitHub `taoshidev/vanta-network`: 76 stars, 40 forks, 1,327 commits, last commit 2026-10-15, MIT. [S1][S25][S26]
- **Criticism.** Taoshi's own proposals record the problems: miners harvested incentive right after closing winners (P6, July 2024); softmax temperature 0.06 concentrated emissions too much, raised to 0.125 (P18, Feb 2025); emissions overpaid relative to capital by up to 100x (P20). A third-party critique (not fetched; 403) argues the simulation understates costs and market impact. [S27][S28][S20][S29]

### 3.2 SN50 Synth (co-founded by Mode Network)

- **What miners produce.** Ensembles of 1,000 simulated price paths per request, in three competitions since June 2026: Crypto 1h (BTC, ETH, SOL, XRP, HYPE; 1-minute steps), Crypto 24h (same assets; 5-minute steps) and Commodities/Equities 24h (XAU, SP500, NVDAX, GOOGLX, TSLAX, AAPLX, WTIOIL, SPCX). Paths are due at `start_time`, one minute after the request, which closes the lookahead window. [S6]
- **Scoring.** Empirical CRPS on price changes in basis points: `CRPS = (1/N) sum|y_n - x| - (1/(2N^2)) sum sum|y_n - y_m|`, summed over increments (24h: 5m, 30m, 3h, 24h; 1h: 1, 2, 5, 15, 30, 60 min plus 5-minute gap checkpoints). Crypto 1h adds a realised-volatility CRPS term with λ = 5.25. Per request the sums are capped at the 90th percentile of valid miners, invalid or late submissions get the 90th-percentile score, and the best score is subtracted so the leader sits at 0. Leaderboard = asset-weighted rolling average (window 5 days for 1h, 10 days for 24h; weights e.g. BTC 1.0, HYPE 0.478, SP500 3.44). Emissions = softmax with β 0.3 (1h) or 0.15 (24h), one third per competition. [S6]
- **Ground truth and Hyperliquid.** Since 2026-07 validators score against 1-minute candle closes from Binance spot (BTC/ETH/SOL/XRP), Hyperliquid spot (HYPE) and Hyperliquid perps (equities, commodities); Pyth was dropped. [S6]
- **Anti-gaming.** None beyond the proper scoring rule, the deadline, the percentile cap and relative ranking. No copy detection is described. [S6]
- **Requirements.** Miner: 2 cores, 16 GB RAM, no GPU; validator: 16 cores, 64 GB RAM, Postgres; about 0.1 TAO to register. [S30][S31]
- **Revenue / product.** REST API (percentiles, volatility, option prices, liquidation probabilities at 6/12/18/24 h, LP ranges, Polymarket comparisons); 200+ models compete; meta-leaderboard refreshes every 12 min (1h) and hourly (24h). July 2026 newsletter: "Q2 was profitable" and a $100,000 SN50 buyback over 8 weeks, no figures. A June 2026 analysis puts market cap near $6M and notes Mode is a captive first customer with no public evidence of outside demand and no independent performance audit. [S32][S9][S10]
- **Market and activity (2026-10-19).** CoinGecko: $1.05, market cap $5.8M, 46th; taomarketcap τ21.45K, owner τ5.08/day. GitHub `synthdataco/synth-subnet` (the `mode-network` URL redirects): 102 stars, 47 forks, 336 commits, last commit 2026-10-14. [S1][S33][S26]

### 3.3 Precog (Coin Metrics / Yuma), formerly SN55

- **Netuid, resolved.** The README states mainnet UID 55 (testnet 256) and Coin Metrics announced "Subnet #55". On 2026-01-15 Talos (which had taken over Coin Metrics' role) wrote that it was "stepping back" and "handing its subnet slot to a new project". By April 2026 SN55 was NIOME (synthetic genomics, Genomes.io), which taomarketcap shows today. CoinGecko still labels the SN55 token "Precog" ($0.70, $3.8M, 2026-10-19), which is the source of the conflict. Treat Precog as defunct; its repo's last commit is 2025-10-07. [S34][S35][S36][S37][S2][S1]
- **Mechanism (for the record).** Every 5 minutes miners gave a point forecast of BTC/USD one hour ahead plus a min/max interval for the next hour; constants later added ETH and TAO with `TASK_WEIGHTS` 0.166 per (asset, task). `reward.py`: point error = `|pred - actual| / actual`; interval score = `inclusion_factor * width_factor` (share of 1-second prices inside the band times the effective-range-to-predicted-width ratio); both ranked per asset and converted to weights with `decay = 0.8`; ground truth is the Coin Metrics reference rate at 1-second frequency. Hardware 2 vCPU, 8 GB. 245 miners in Feb 2025, 1,844 registrations by April 2025. [S34][S38][S39][S40]
- **Lesson from its own evaluation (2025-04-22).** A naive $1-per-5-minutes strategy on the top-20 miners' average forecast was positive in 15 of 18 bootstrapped weeks, but the authors warned the window was short and only the point forecast was tested. [S41]

### 3.4 Tenex / Tenexium, formerly SN67 (long-only spot margin)

- **What it was.** Not a forecasting subnet: a Solidity protocol on Bittensor EVM where traders post TAO collateral to lever subnet alpha longs (2x to 10x by Tenex-alpha holding tier) and "miners" are liquidity providers paid pro rata to TAO supplied; no validator scoring formula was published and the validator was described as temporary. Fees: 0.3% trading, borrowing 0.005% per 360 blocks rising at 80% utilisation, maintenance margin 110%, liquidation penalty 2%, 90% of protocol revenue to buybacks. [S42][S43]
- **Repo.** `latent-to/tenex-subnet` is a fork (0 stars, last commit 2025-09-10) of `ambitious922/tenex-subnet` (created 2025-09-08), which clones from `Tenexium/tenex-subnet` (now 404). [S42][S43][S26]
- **Outcome.** SubnetEdge (2026-01-02, paywalled) reports SN67 under the Tenexium brand "reportedly orchestrated an exit" with an estimated $2.8M loss; headline: "When a single private key controls the entire LP". SN67 is Harnyx (deep research) on 2026-10-19. Last price data found: about $1.34 on 2026-08-22. [S3][S44][S45]

### 3.5 SN6 Numinous (Numinous Labs), formerly Infinite Games

- **What miners produce.** A Python agent (`agent_main(event_data) -> {prediction in [0,1]}`, 2 MB limit) that validators run in Docker sandboxes with a signing gateway to OpenAI, OpenRouter, Lightning Rod and Numinous' own news/OSINT signals; 240 s timeout; the agent re-forecasts every live event every 24 h and carries private memory. Events are prediction-market questions (Polymarket-style) with resolution at least 14 days out. [S46][S47]
- **Scoring (current, from `docs/scoring-system.md`).** `S = (prediction - target)^2 - (market_price - target)^2`, target = market price 7 days later or the realised outcome; matching the market scores 0; lower is better. Rolling 7-day average over events the miner forecast; coverage below 85% of (event, interval) cells over 14 days earns zero. New miners are ranked on a pessimistic bound `ucb = mean + pen(T)` with `z = 1.6449`, 3 days full protection, decay τ = 2.5 days. Payout: `take = max(baseline - ucb, 0)^2` with baseline = 21st-best bound, so roughly the top 20 earn; if nobody clears the gate the whole emission burns. The older Brier and peer-score (`log p_i - mean log p_j`) systems and winner-takes-all pools are documented as inactive. [S48][S49]
- **Anti-gaming.** Sandbox with no key exposure, deterministic prompts (no timestamps or random seeds) so validators get identical outputs, code updates at most every 3 days. [S46]
- **Requirements.** Validator 4 cores, 16 GB (32 recommended), 100 GB SSD, and a stated 10,000 TAO minimum stake on mainnet. Miner pays its own API usage. [S50]
- **Market and activity (2026-10-19).** CoinGecko $0.60, $2.6M, 78th. GitHub `numinouslabs/numinous`: 20 stars, 15 forks, 117 commits, last commit 2026-09-22. The old `infinite_games` repo (368 commits) is a redirect. [S1][S26]

### 3.6 SN41 Sportstensor

- **What miners produce.** Since the 2025 Polymarket partnership, miners trade on Almanac, which routes orders to the Polymarket CLOB through proxy wallets; every trade is a scored prediction. Earlier (Oct 2024) scoring used "closing edge" versus market odds. [S51][S52]
- **Scoring (archived README).** Rolling 30-day window, daily epochs scored hourly; ROI = profit / volume; "qualified volume" = winning-trade volume after fees; gates on minimum ROI, minimum volume and a build-up period; two-phase optimisation (fund qualified volume, then tilt to higher ROI); diversity caps, volume decay and ramp constraints. No formula is published, and the public repo `sportstensor/sn41` was archived on 2026-08-03 (last commit 2026-07-20, 6 stars); a third-party wiki says the code is now closed. [S51][S26][S52]
- **Market (2026-10-19).** CoinGecko $1.86, $9.2M, 29th. [S1]

### 3.7 SN123 MANTIS (financial signal salience)

- **What miners produce.** Encrypted payloads hosted on Cloudflare R2 (object key = hotkey) containing embeddings per challenge: 1-hour binaries on ETH, CADUSD, NZDUSD, CHFUSD, XAGUSD; ETH hit-first; 17-dim LBFGS vectors on ETH (1h) and BTC (6h); MULTI-BREAKOUT and XSEC-RANK over 33 assets; FUNDING-XSEC over 20 assets (predict which perps' funding-rate change exceeds the cross-sectional median over the next 8 h); FLOW-BTC capital-at-risk bracket trades (launched 2026-08-17, emission turn-on 2026-09-14). [S53][S54]
- **Scoring.** Walk-forward salience: validators fit L2 logistic / ElasticNet meta-models on miners' embeddings to predict the target and credit each miner with its coefficient importance (|β| or β², AUC-scaled), normalised per challenge, mixed by challenge weights, EMA α = 0.15; near-uniform distributions are rejected. Burn: README says 35%, `config.py` says `BURN_PCT = 0.45` (read 2026-10-19). [S53][S55]
- **Anti-gaming.** Dual encryption: owner path (X25519 + ChaCha20-Poly1305) and Drand timelock (BLS12-381) so nobody can read a prediction before maturity; a SHA-256 binding over (hotkey, round, keys) defeats relay and substitution; L1/uniqueness penalty (85% argmax overlap) drives clones to zero; label-leak embargo on funding; stale-signal filter. [S53]
- **Ground truth.** Prices come from an owner-published `latest_prices.json` on R2, a centralised feed. [S55]
- **Market and activity (2026-10-19).** CoinGecko $0.61, $2.6M, 79th, 24h volume $2.9k. GitHub `Barbariandev/MANTIS`: 20 stars, 18 forks, 212 commits, last commit 2026-10-18. Hardware/stake not stated. [S1][S26]

### 3.8 Greevils, formerly SN58 (the Hyperliquid-native precedent)

- **What it was.** "Agents against the humans": AI agents (sealed in a Confidential Space TDX TEE with a trade-only key) and human traders trade real Hyperliquid perps; ownership is proven by an EIP-191 `personal_sign` commitment (hl_address + signature, 128-byte limit) on chain; every fill must carry the Greevils builder fee (~5 bps, tolerance 0.5 bps) or the account is permanently disqualified; perps only, whitelist pairs, $1,000 equity floor, 14 idle days = DQ. [S8][S56][S57]
- **Scoring (from `incentive.md`).** Pairwise tournament on each pair's overlap window (floored at 30 days). Daily return is flow-adjusted with a $10k denominator floor on profit days. Utility `U = ln(1+R) - 0.308 (D/0.10)^2 - 0.15 softplus(V/0.02 - 1) - 0.75 ramp * max(0, K - 0.14)` (R compounded return, D max drawdown, V downside RMS, K Herfindahl of profit days); `G = (e^{1.5 U+} - 1)/(e^{1.5} - 1)`; haircut `M = 0.5` if trailing-30-day PnL < 0 or 14-day executed value < 4x equity; `score = G * M` plus a longevity bonus with 90-day half-life. Eligibility: 60 days runtime, 40 active days, lifetime executed value ≥ 25x average equity, 1.5% return hurdle; agents must be open-sourced and approved. Human pool = `min(50%, 0.1 x human PnL$ / emission$)`, agents take the rest; unawarded emission burns. Validator snapshots Hyperliquid clearinghouse equity at 00:00 UTC into SQLite and re-sets weights every epoch. [S57]
- **Outcome.** Repos last pushed July 2026 (validator 2026-07-23). A 2026-10-07 community post calls SN58 "Attune, previously Greevils"; subnetalpha and taomarketcap show Attune (Robotensor) on 2026-10-19; CoinGecko labels the SN58 token "Handshake" ($1.35M). When and why Greevils left could not be confirmed. [S26][S58][S59][S4][S1]

### 3.9 Efficient Frontier (SignalPlus), formerly SN53

- Miners linked real CEX accounts through SignalPlus's platform and ran options, futures and spot strategies; an earlier aggregator description gives the score as exponentially weighted daily returns divided by the rolling 14-day peak-to-trough drawdown, computed daily at 08:00 UTC after an observation period, with capital size as a factor; registered 2024-11-06; a claimed $2M cumulative profit for the top 50 strategies is unverified. No public repository was found. By mid-2026 netuid 53 was Engy (Hanlin AI, verified inference, 18% owner cut). CoinGecko still shows "EfficientFrontier SN53" at $56.5M (4th) on 2026-10-19; that is Engy's token. [S60][S61][S62][S1]

### 3.10 Smaller or adjacent

- **SN79 τaos / MVTRX** (`taos-im/sn-79`): agent-based limit-order-book simulation in C++; trading pool (~95%) scores "making" (spread captured and held) and "skill" (profit net of inventory drift), GenTRX training pool ~5%. $4.0M market cap, 59th (2026-10-19). Relevant as a simulated-market-making design, not a forecasting one. [S63][S1]
- **SN31 Candles** (`thealligatorking/Candles`): hourly/daily/weekly candle colour, close and confidence; reference miner picks at random; 0 stars, last push 2025-08-20; $1.7M, 87th. [S64][S26][S1]

## 4. Hyperliquid touchpoints found

| Subnet | How Hyperliquid is used | Source |
|---|---|---|
| SN8 Vanta | Reference venue for crypto/commodity/index/equity perps; live funding as carry; L2 book for simulated slippage; HL-linked subaccounts forward a real address's fills as signals; Vanta Trading sells simulated evaluations | [S5][S12][S14][S15] |
| SN50 Synth | Ground-truth 1-minute candles for HYPE (spot) and equities/commodities (perps) since 2026-07 | [S6] |
| SN58 Greevils (gone) | Real execution on Hyperliquid perps with mandatory builder fee; validator reads clearinghouse state daily | [S57] |
| SN123 MANTIS | Cross-sectional perp funding-change challenge (venue of the feed not stated) | [S54] |

Nothing was found that serves or verifies Hyperliquid node data (OI, funding history, L1 state) as a product, and no vault-based execution.

## 5. Market snapshot, 2026-10-19

CoinGecko `bittensor-subnets` category, 117 tokens, read 16:48 UTC; rank is by market cap inside the category. Total category market cap $1.21B. [S1][S65]

| Token (netuid) | Price | Market cap | Rank | Note |
|---|---|---|---|---|
| lium (SN51) | $29.72 | $146.7M | 1 | not trading |
| Chutes (SN64) | $17.98 | $111.4M | 2 | not trading |
| Targon (SN4) | $14.31 | $84.8M | 3 | not trading |
| "EfficientFrontier" (SN53) | $9.88 | $56.5M | 4 | now Engy |
| Vanta Network (SN8) | $8.06 | $44.4M | 7 | |
| Sportstensor (SN41) | $1.86 | $9.2M | 29 | |
| Synth (SN50) | $1.05 | $5.8M | 46 | |
| τaos (SN79) | $0.81 | $4.0M | 59 | |
| "Precog" (SN55) | $0.70 | $3.8M | 63 | now NIOME |
| Numinous (SN6) | $0.60 | $2.6M | 78 | |
| Mantis (SN123) | $0.61 | $2.6M | 79 | |
| Candles (SN31) | $1.05 | $1.7M | 87 | |
| "Handshake" (SN58) | $1.81 | $1.35M | 94 | now Attune |
| SN67 | not listed | | | now Harnyx |

Emission: taomarketcap "owner/day" (read 2026-10-19, undated on page): SN8 τ38.83, SN53 τ47.74, SN58 τ8.71, SN67 τ4.10, SN50 τ5.08, SN55 τ3.38. With the protocol's 18% owner share this implies roughly τ216/day for SN8 and τ28/day for SN50 at subnet level; treat as an estimate. Emission ranks could not be confirmed. [S25][S33][S66]

## 6. Scoring-design lessons

1. **Proper scoring rules survived; leaderboard ratios did not.** Synth's CRPS and Numinous' market-relative squared error have been stable for a year or more. Vanta re-tuned its simulated-equity scoring through 26 proposals (short-term returns harvested at position close, P6; softmax too sharp, P18; Calmar/Sharpe/Omega/Sortino now weighted 0%) and finally pays on weekly PnL through a debt ledger. Risk-adjusted ratios on simulated capital reward tiny low-volatility positions and are easy to game. [S6][S48][S27][S28][S11]
2. **Pay relative to a baseline and cap the tails.** Synth subtracts the best score and caps at the 90th percentile; Numinous sets "match the market" to zero and burns emission if nobody beats it; Greevils scores against a zero-utility floor. A subnet that pays for merely participating ends up paying noise. [S6][S48][S57]
3. **Close the lookahead window explicitly.** Synth's deadline is the path start time; MANTIS uses Drand timelock encryption and its FLOW challenge voids bets posted after the trade opens; Numinous forbids non-deterministic prompts. Anything scored on public prices must commit before the window and reveal after. [S6][S53][S46]
4. **Copy-trading is the main attack on signal subnets.** Vanta needed a private correlation-with-lag detector (thresholds 0.75/0.8/0.9, 10-day lookback, 10-second minimum lag) and still runs a 14-day review; MANTIS's binding hash plus L1 uniqueness penalty makes copies worthless automatically. Prefer designs where copying earns nothing by construction over detectors with appeals. [S16][S18][S53]
5. **Emission must scale with something scarce.** Vanta found top miners earning about 100x what their simulated capital could justify and added burns (P20) and collateral (P22); the result was sybil pressure and churn. Collateral or real capital per miner is now standard in trading subnets. [S20][S19]
6. **Real-money execution is verifiable but fragile.** Greevils (real Hyperliquid accounts, 60-day eligibility, builder-fee exclusivity, $1,000 floor) and Efficient Frontier (real CEX accounts via a trusted intermediary) both left their slots within about a year; Tenex, where miners were LPs behind a single key, ended with a reported $2.8M loss. If real capital is involved, the mechanism needs low entry friction, no custodial key, and a reason for traders to stay. [S57][S60][S3]
7. **Ground truth provenance is a differentiator.** Synth moved off Pyth to exchange candles (2026-07); Precog used a 1-second Coin Metrics reference rate; MANTIS relies on an owner-published JSON; Vanta pays for Massive and Tiingo. Serving verified Hyperliquid mark/oracle, funding and OI from the team's own nodes, with a deterministic candle spec every validator can reproduce, is something no subnet offers today. [S6][S38][S55][S12]
8. **Coverage and tenure gates reduce churn.** Numinous zeroes miners below 85% coverage and handicaps newcomers for about two weeks; Greevils requires 40 active days and 25x turnover; Vanta requires 61 trading days and a top-25 rank. [S48][S57][S11]
9. **Revenue, not emissions, keeps a slot.** Four of nine trading slots turned over in 2026. Synth is the only one with a stated profit and buyback; its critics still ask for usage outside Mode and an independent audit. [S9][S10]
10. **Closed scoring code draws criticism.** Sportstensor's closed source and Vanta's private plagiarism service are both flagged by third parties as unauditable; MANTIS and Synth publish their full scoring and a backtester. [S52][S18][S6]

## 7. Candidate mechanisms for a Hyperliquid-native subnet

All scoring below uses data the team can serve from its own Hyperliquid nodes: 1-minute mark and oracle candles per perp, each funding settlement, hourly OI snapshots, and (optionally) L2 book snapshots. Validators should fetch the same deterministic datasets (hash-committed per window) so weights agree.

**A. Per-market probabilistic forecasts of mark price (Synth-style, but Hyperliquid-wide).** Miners submit 1,000-path ensembles or quantile sets for every listed perp (including the long tail) at 5 min, 1 h and 24 h horizons; scored by CRPS (or pinball loss on quantiles) on basis-point changes against mark candles, relative to a GBM/GARCH baseline, capped at the 90th percentile, softmax-weighted.
- Pros: objective, cheap to verify, proper scoring rule; directly feeds liquidation-risk bands and position sizing in the app; miners' deadline closes lookahead.
- Cons: overlaps Synth (BTC, ETH, SOL, XRP, HYPE, equities on Hyperliquid perps); differentiation must come from market coverage (all perps), oracle-vs-mark basis, and horizons; Synth could add the same assets quickly.

**B. Funding and basis forecasting ("carry desk").** Miners predict the next k funding settlements (or the change versus the last settlement, and the cross-sectional rank across markets) per perp; scored by CRPS/Brier against the node's recorded funding, relative to a naive last-value predictor, plus an OI-change component.
- Pros: unique (only MANTIS has one cross-sectional funding challenge, with a centralised feed); funding is a first-class perps variable and the app can expose expected carry and funding-arb opportunities; data comes straight from the nodes.
- Cons: funding is highly autocorrelated (MANTIS cites φ ≈ 0.97), so levels are trivially predictable; score changes or ranks and subtract the naive baseline or emissions go to noise; smaller "market" than price forecasting.

**C. Liquidation and tail-risk forecasting.** Miners submit probabilities that mark touches ±x% bands (or the realised max adverse excursion) over 1 h/4 h/24 h per market; scored by Brier against mark-candle highs/lows; add a realised-volatility CRPS term as Synth does.
- Pros: maps one-to-one onto app features (liquidation warnings, stop placement, margin suggestions); binary targets are easy to audit.
- Cons: Synth already derives liquidation probabilities from its paths; a dedicated subnet must beat that derived product, which is only shown by publishing calibration versus Synth's API.

**D. Verified execution tournament on Hyperliquid with builder-code attribution (Greevils-style).** Miners prove address ownership (EIP-191), route every fill through the app's builder code, and are scored daily on risk-adjusted flow-adjusted returns marked at oracle price, with drawdown dominant and a dollar cap tied to realised PnL; the builder fee is subnet revenue.
- Pros: real ground truth from the node's clearinghouse state; revenue from day one; natural leaderboard for the app.
- Cons: Greevils ran exactly this and left SN58 within a year; capital and 60-day gating thin the field; wash and self-trading across accounts must be detected from fills; emissions subsidise trading on a venue, which is the most regulatory-sensitive option (section 8).

**E. Timelocked short-horizon signal salience on node order-flow (MANTIS-style, Hyperliquid data).** Miners publish timelock-encrypted embeddings; validators train a walk-forward meta-model to predict 1 to 15 minute mark moves from the node's L2 and trade stream and pay by coefficient salience.
- Pros: copying earns nothing by construction; captures private information; output is directly usable by the market-analysis agent.
- Cons: validator compute and complexity; scoring is less legible to miners; MANTIS already exists (though not Hyperliquid-specific); the meta-model becomes the product's quality bottleneck.

Combination worth testing: B + C as two competitions with equal emission split (Synth's structure), with A deferred unless the long-tail coverage proves to be demand the app actually has. D only with a TEE or fill-level verification and after the regulatory review.

## 8. Regulatory sensitivity (factor, not advice)

Mechanisms that broadcast executable signals to app users or auto-copy them are the most exposed. ESMA's 30 March 2023 supervisory briefing treats copy trading as portfolio management, investment advice or order transmission under MiFID II depending on set-up (building on its 2012 Q&A on automatic execution of trade signals), and in February 2026 ESMA stated that perpetual futures fall under existing EU CFD product-intervention rules regardless of naming, with limits on retail leverage and mass marketing. In the US the CFTC approved the first domestically listed bitcoin perpetual on 29 May 2026 and issued staff letters on offshore perpetual access, which brings perps closer to the regulated perimeter. Forecast and risk-data products (B, C, A) look less exposed than signal or copy mechanisms (D), but the team should get counsel before choosing. [S67][S68][S69]

## 9. Could not confirm

- Emission shares and emission ranks (taostats, tao.app and bittensor.ai did not render or returned 403).
- Exact dates and reasons for the SN53, SN55, SN58 and SN67 handovers; the Greevils wind-down; the Tenex loss details (paywalled).
- Any revenue figure for Vanta, Synth (beyond "Q2 profitable") or Numinous.
- Efficient Frontier's original scoring formula and repository.
- Whether Vanta's P24 weights (PnL 30% + five metrics at 14%) ever shipped; the current docs show PnL 100%.
- The venue behind MANTIS's funding feed.

## Sources

- [S1] https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category=bittensor-subnets&order=market_cap_desc&per_page=250&page=1 (read 2026-10-19 16:48 UTC)
- [S2] https://taomarketcap.com/subnets/55
- [S3] https://subnetedge.substack.com/p/exploit
- [S4] https://taomarketcap.com/subnets/58
- [S5] https://github.com/taoshidev/vanta-network (README)
- [S6] https://github.com/synthdataco/synth-subnet (README, read 2026-10-19)
- [S7] https://subnetalpha.ai/subnet/greevils/
- [S8] https://www.greevils.ai/
- [S9] https://www.abittensorjourney.com/p/navigating-bittensor-july-2026
- [S10] https://www.abittensorjourney.com/p/synth-sn50-the-subnet-that-sells (2026-06-25)
- [S11] https://github.com/taoshidev/vanta-network/blob/main/docs/miner.md
- [S12] https://github.com/taoshidev/vanta-network/blob/main/docs/validator.md
- [S13] https://github.com/taoshidev/vanta-network/blob/main/vali_objects/vali_config.py (Hyperliquid endpoints, slippage audit)
- [S14] https://github.com/taoshidev/vanta-network/blob/main/docs/entity_miner.md
- [S15] https://github.com/taoshidev/vanta-network/blob/main/docs/entity_miner_rest_server.md
- [S16] https://github.com/taoshidev/vanta-network/blob/main/vali_objects/vali_config.py (challenge, drawdown and plagiarism constants, read 2026-10-19)
- [S17] https://github.com/taoshidev/vanta-network/blob/main/vali_objects/plagiarism/plagiarism_manager.py
- [S18] https://docs.taoshi.io/tips/p25/
- [S19] https://docs.taoshi.io/tips/p22/
- [S20] https://docs.taoshi.io/tips/p20/
- [S21] https://finance.yahoo.com/news/taoshi-announces-vanta-trading-decentralized-150000841.html (2026-02-17)
- [S22] https://simplytao.ai/blog/your-simple-guide-to-vanta-sn8 (2026-03-25)
- [S23] https://blog.kraken.com/product/asset-listings/sn8-is-available-for-trading (via search; listing live 2026-07-16)
- [S24] https://www.dlnews.com/articles/defi/taoshi-taps-bittensor-to-bring-forex-market-to-defi-with-new-exchange/ (2025-09-11)
- [S25] https://taomarketcap.com/subnets/8
- [S26] GitHub REST API, https://api.github.com/repos/{owner}/{repo} and /commits?per_page=1, read 2026-10-19
- [S27] https://docs.taoshi.io/tips/p6/
- [S28] https://docs.taoshi.io/tips/p18/
- [S29] https://medium.com/@0xai.dev/how-can-trading-network-subnets-improve-a1e2652663a3 (not fetched, HTTP 403; seen via search summary)
- [S30] https://raw.githubusercontent.com/synthdataco/synth-subnet/main/min_compute.yml
- [S31] https://github.com/synthdataco/synth-subnet/blob/main/docs/validator_guide.md
- [S32] https://docs.synthdata.co/
- [S33] https://taomarketcap.com/subnets/50
- [S34] https://github.com/coinmetrics/precog
- [S35] https://coinmetrics.io/company-news/precog-coin-metrics-bittensor-subnet/ (via search)
- [S36] https://www.talos.com/insights/precog-community-update-january-2026 (2026-01-15)
- [S37] https://subnetalpha.ai/subnet/niome/
- [S38] https://github.com/coinmetrics/precog/blob/master/precog/validators/reward.py
- [S39] https://raw.githubusercontent.com/coinmetrics/precog/master/precog/constants/__init__.py
- [S40] https://coinmetrics.io/?p=24524 and https://coinmetrics.io/?p=26145 (roadmap updates Feb and Apr 2025, via search)
- [S41] https://coinmetrics.substack.com/p/state-of-the-network-issue-308 (2025-04-22)
- [S42] https://github.com/latent-to/tenex-subnet
- [S43] https://github.com/ambitious922/tenex-subnet
- [S44] https://taomarketcap.com/subnets/67
- [S45] https://beincrypto.com/convert/tenex-to-usd/ (via search, 2026-08-22 rate)
- [S46] https://github.com/numinouslabs/numinous
- [S47] https://raw.githubusercontent.com/numinouslabs/numinous/main/docs/subnet-rules.md
- [S48] https://raw.githubusercontent.com/numinouslabs/numinous/main/docs/scoring-system.md
- [S49] https://github.com/niels-ma/infinite_games (legacy Brier and peer score)
- [S50] https://raw.githubusercontent.com/numinouslabs/numinous/main/docs/validator-setup.md
- [S51] https://github.com/sportstensor/sn41 (archived 2026-08-03)
- [S52] https://pm.wiki/uk/projects/sportstensor
- [S53] https://github.com/Barbariandev/MANTIS
- [S54] https://raw.githubusercontent.com/Barbariandev/MANTIS/main/MINER_GUIDE.md
- [S55] https://raw.githubusercontent.com/Barbariandev/MANTIS/main/config.py
- [S56] https://github.com/greevils-ai/greevils-docs
- [S57] https://raw.githubusercontent.com/greevils-ai/greevils-validator/main/incentive.md
- [S58] https://www.kucoin.com/news/community/TAO/6ac6ca7e38a264000793118d (2026-10-07)
- [S59] https://subnetalpha.ai/subnet/greevils/ (now shows Attune)
- [S60] https://subnetalpha.ai/subnet/efficient_frontier/ (earlier description via search; page now shows Engy)
- [S61] https://subnetalpha.ai/subnet/engy/
- [S62] https://yellow.com/asset/sn53
- [S63] https://raw.githubusercontent.com/taos-im/sn-79/main/README.md
- [S64] https://github.com/thealligatorking/Candles
- [S65] https://www.coingecko.com/en/categories/bittensor-subnets
- [S66] https://www.bittensor.com/docs/concepts/network (18/41/41 emission split, validator permits, 256 UIDs)
- [S67] https://legal.pwc.de/en/news/articles/esma-communicates-supervisory-expectations-on-copy-trading-services (ESMA briefing 2023-03-30)
- [S68] https://www.financemagnates.com/forex/regulation/esma-tells-firms-perpetual-futures-fall-under-eu-cfd-rules (2026-02-24)
- [S69] https://www.dechert.com/knowledge/onpoint/2026/6/cftc-takes-historic-steps-to-bring-digital-asset-perpetual-contr.html (CFTC actions 2026-05-29)
