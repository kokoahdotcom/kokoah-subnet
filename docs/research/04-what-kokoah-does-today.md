# What Kokoah does today (2026-10-19)

A factual picture of the Kokoah product and system as it runs now, written so the subnet can be designed to fit it.
Sources: `kokoah-dev/agent.md` (status), `kokoah-dev/specs/`, `kokoah-dev/reviews/2026-10-03/`, and the READMEs and
`docs/` of each repo under `/kokoah/`. Nothing here is a plan for the subnet; section 5 only lists where its output
could land, and section 7 what only the owner can decide.

## 1. Product in one page

**What it is.** Kokoah (kokoah.com) is a non-custodial trading app for Hyperliquid perpetual futures (including HIP-3
builder markets) and, in Pro, spot. It is a web app, responsive on phones and installable as a PWA; there are no
native apps. It has run on Hyperliquid **mainnet since 2026-10-06** (before that, testnet). The product spec is
`kokoah-dev/specs/FRONTEND_SPEC.md`; the user docs are rendered from the frontend's `src/site/docs/`.

**Non-custodial model.** Funds stay in the user's own Hyperliquid account. Three kinds of keys exist:
- the user's **main wallet** (an external wallet via WalletConnect/Reown, the Dynamic embedded "Kokoah wallet" with
  email/Google/Apple/passkey sign-up, or the Kokoah browser extension built by another developer). It signs only
  readable EIP-712 messages: approve an agent key, approve the builder fee, withdraw, and the signed automation policy.
  It never signs an order (Hyperliquid L1 actions are opaque hashes).
- a **browser trading key** per device (a Hyperliquid "agent" key named `kokoah-xxxx`, valid at most 30 days), created
  and held by the signer frame on its own origin; it can trade but not withdraw. Manual trading goes browser → signer
  frame → Hyperliquid directly, with no Kokoah server in the path.
- a **server automation key** (`kokoah-bot`) per user who turns on automation, held encrypted by the signer service,
  used only inside limits the user signed with their main wallet (per-order and daily notional, leverage cap, allowed
  markets and origins, stop-loss requirement, daily loss limit, expiry). Guardians, bots and Telegram trades go
  through it. The backend never holds a signing key and has no code path to Hyperliquid `/exchange`.

**What a user can do.**
- Trade with an order ticket (market, limit, TP/SL, Protect presets; Pro adds advanced order types and spot), with a
  pre-trade risk summary (liquidation distance, maximum loss, fees, funding) computed in code; a "Check before I
  confirm" step adds warnings (thin book, funding against the side, important news, top traders on the other side).
- See positions, orders, balances, a full history and an automatic trade journal (round trips, statistics, behaviour
  flags). Kokoah keeps every fill; Hyperliquid's API serves only a user's newest 10,000.
- Fund the account from Arbitrum, Base and Ethereum (Circle CCTP, the legacy bridge, Relay, Across) with a transfer
  tracker; withdraw; move funds between the main account and sub-accounts ("strategy accounts").
- Set alerts (price cross, % move, price steps, liquidation distance, order events, funding, PnL, account margin,
  market alerts from the node's positioning signals) delivered in-app, by web push and in Telegram, with per-type
  mutes, quiet hours and an inbox.
- Run **Guardians**: nine server-side rule types (protect partial fills, breakeven, take-profit ladder, trailing
  stop, time exit, OCO entries, conditional, liquidation guard, daily loss lock) that work with the app closed.
- Run **Bots** (Pro): grid, DCA, copy-trading (follows a leaderboard trader), TradingView signal bots (a webhook) and
  indicator signal bots (rules on chart indicators read at candle closes; alert, trade or both), each with a backtest.
- **Discover** (Pro): Top traders (Hyperliquid's leaderboard with quality filters, follow alerts, trader pages with a
  year of fills), Market scanner (crowd vs best traders vs whales, liquidation clusters, TWAPs, whale trades), Pulse
  (the twice-daily market report and its archive), Funding board, News (market news every 6 hours, rated headlines,
  Hyperliquid announcements). The chart has a liquidation map, entry profile, order flow and an "Analyze" mode that
  names who traded a range.
- Use the **Telegram bot**: commands, trades with a preview → Confirm → PIN, alert buttons, and an AI assistant
  (plain-language questions and trades, voice notes, a morning brief, trade reviews, follow digests).
- Ask the in-app **Kokoah assistant** (help bot): platform questions from the public docs, the user's own account,
  market moves from live data and news, drafted trades, order management and guardian drafts, all confirmed by the
  user in the app.
- Link a phone as a second device, name other traders' addresses, read Kokoah announcements, share a PnL card, invite
  friends (a referral ledger).

**Monetization.** A builder fee on every order Kokoah causes (target about 0.025%; a launch promotion currently sets it
to 0). Native TWAP and the native trailing stop are denied by the signing policy because they carry no builder fee.
Kokoah is listed on DefiLlama as a Hyperliquid builder. No subscriptions; the proposed agent marketplace would pay
authors a share of the builder fee (section 4).

**Languages and platforms.** English plus ten languages (ko, zh-CN, zh-TW, ja, es, pt-BR, fr, ru, tr, vi;
`kokoah-frontend/docs/I18N.md`); server-side texts (notifications, Telegram, Pulse translations) follow. Platforms:
web, PWA (service worker, web push; iOS push only after Home Screen install), the Chrome extension wallet, Telegram.

## 2. System map

All repos are private on GitHub; the local checkouts are `/kokoah/<repo>`. Node 24, strict ESM TypeScript, pnpm
10.34.5 via corepack, vitest with in-memory PGlite; the subnet is the first Python project.

| Repo | What it is | Talks to |
|---|---|---|
| `kokoah-frontend` | React/Vite app and PWA: everything the user sees. Simple and Pro layouts | Hyperliquid directly (REST + WebSocket), the signer frame (iframe + postMessage), the main wallet, the Kokoah API |
| `kokoah-signer-frame` | Tiny static page on a separate origin holding the browser agent key; policy check, sign, submit to Hyperliquid | Hyperliquid only; embedded only by the frontend |
| `kokoah-backend` | Fastify API (`docs/API.md` is the contract; OpenAPI at `/docs/openapi.json`) plus leased worker jobs: fill ingest, alert and guardian engine, notifier, bots, candle store, discover jobs, market intelligence, assistant, Pulse store, admin | Postgres; Hyperliquid reads (weight-budgeted); the own nodes; the signer service (HMAC over pinned TLS); push, Telegram, Discord ops alerts |
| `kokoah-signer-service` | Isolated service holding server agent keys (KMS envelope encryption in production); `/v1/execute` signs one intent after re-verifying the user-signed policy; kill switch; audit trail | Called only by the backend; calls Hyperliquid and the node's fill index (for the signed daily loss limit) |
| `kokoah-shared` | Library every service imports so they compute identical numbers: network config and mainnet write guard, markets, precision, risk, intents, action compiler, signing, policy, guardians, bots, indicators, backtests. No network I/O | – |
| `kokoah-agent` | Kokoah Pulse: the market-analysis agent (section 3). Reads public mainnet data, never trades, publishes to the API | Hyperliquid public `/info`, news feeds, a model provider, the Kokoah API's ingest endpoints |
| `kokoah-infra` | Conventions, security baseline, headers, CI templates, local compose, the self-host stack, the Hyperliquid node kit (`node/`), Vercel build/deploy scripts, Terraform for AWS (never applied) | – |
| `kokoah-dev` | `agent.md` (status), `specs/`, `reviews/` | – |
| `agent-hub` | The team's Discord room for coding agents: one always-on runner per agent, pings start headless runs, owners approve gated actions | Discord |
| `kokoah-extension` | The Kokoah wallet extension (another developer's; read-only checkout) | – |

**Where it runs.** The app and the signer frame are static sites on Vercel. The API, its workers, the signer service
and both Postgres databases run as a Docker Compose stack on one server of the team's in the eastern US, behind
Cloudflare (DNS and a rate limit on the API). Two **Hyperliquid non-validator nodes** run on two rented servers, one
in Europe and one in Tokyo (each `--serve-info`, with a fill index and a history store from `kokoah-infra/node/`),
reachable only by the backend through a token-protected TLS proxy; the backend fails over between them and to the
public API. The Pulse agent runs on the API server as systemd timers (reports 00:02 and 12:02 UTC, news every 15
minutes, market news every 6 hours) with a local dashboard on loopback port 9991. Telegram reaches the API by webhook;
every market news edition is also posted to a public Telegram channel. Operator alerts go to Discord. Nothing runs on
AWS.

## 3. Data Kokoah already has or produces

**Market data paths.**
- The browser reads Hyperliquid itself: prices, candles, books, trades, its own account state (REST and WebSocket).
- The backend reads Hyperliquid `/info` under token-bucket budgets (1,200 weight per minute per IP, split per job), and
  keeps one WebSocket per worker for the markets its alerts and bots need (`activeAssetCtx`: mark, mid, oracle,
  funding, open interest).
- The **own nodes** answer the account and metadata reads with no rate limit (`clearinghouseState`, open orders,
  agents, sub-accounts, `perpDexs`, `spotMeta`). They do not give books, WebSockets, history queries or order
  submission. What the node kit adds on top (`kokoah-infra/node/README.md`, `node/fills/`):
  - **Fill index**: every fill on the exchange, by user, answering `userFillsByTime` for the backend and the signer
    (coverage is vouched for and gaps are reported; a comparison with the public API runs every 5 minutes).
  - **History store**: a year of every fill on the exchange (about 70 bytes a fill, about 0.57 GB a day on mainnet),
    serving `GET /v1/traders/:address/fills` (any address, newest first, with `coverageFrom` and `gaps`).
  - **Order flow** per coin and minute, 30 days: trades, taker buys and sells, longs and shorts opened and closed,
    liquidations by side, taker volume by order size bucket; per address for 7 days (who opened, closed, was
    liquidated, still holds). Served as `GET /v1/markets/:coin/flow`, `/flow/series`, `/flow/addresses`.
  - **Position snapshots**: each large open position's entry, liquidation price, leverage, value and PnL, read from the
    node every 1 to 30 minutes by size (about 84,000 accounts followed on mainnet), plus leaderboard accounts. Served as
    `GET /v1/markets/:coin/positioning` (cohorts all / smart / whales, liquidation and entry bins, coverage of open
    interest) and `/positioning/history` (5-minute cohort points, 30 days).
  - **Position events** (orders that opened, closed, flipped or moved a position by $25k+; 2 days) → `GET /v1/whales`
    (changes of $100k+, 7 days). **TWAPs** filling now (3 days) → `GET /v1/twaps`.
  - **1-minute candles of every perp, HIP-3 and spot market** built from the fills, matching Hyperliquid's own.
- **Candle store** (`kokoah-backend/src/candles/`, `GET /v1/candles`): the top 100 main-dex perps at 1m/5m/15m/1h/4h,
  polled from Hyperliquid before bars leave its 5,000-bar window (1m kept 90 days, the rest indefinitely), plus the
  node's 1m candles for every market rolled up to the stored intervals; hours a node missed are backfilled from
  Hyperliquid's 1h candles. Sparklines read it. It started on testnet in late September and on mainnet on 2026-10-06.
- **Market signals** (`market_signals`, written every 5 minutes for every listed perp with 20+ positions, about 178
  markets): mark, liquidations within ±2/5/10%, TWAPs filling, whale trades; served as `GET /v1/markets/scanner`
  (cached 60 s) and used by the market alert types `groupFlip`, `crowding`, `liquidationCluster`, `largeTwap`.

**Per-user data** (Postgres, backend): fills, funding, ledger and orders from ingest (cursors per account, our node
first), journal round trips and statistics, alerts and their notices, guardian rules and activity, bots (orders,
events, signals, a per-bot ledger), Telegram activity, settings, sessions, support threads, trader labels. Retention
defaults: 180 days for inbox and activity, 365 for security events. Builder fills are ingested daily from
Hyperliquid's `builder_fills` archive for the admin dashboard.

**Kokoah Pulse** (`kokoah-agent`, stored by the backend in `pulse_reports`, `pulse_news`, `pulse_digests`; served
under `/v1/pulse/*`, public, cached 60 s, 120 requests/min per IP):
- **Reports** twice a day (00:02 and 12:02 UTC; about 42 since 2026-09-29, kept indefinitely, ids `YYYYMMDD-HHmm`): for
  the 20 most traded perps plus BTC, ETH, SOL and HYPE, signals computed in code (`src/signals.ts`: trend score from
  EMA 20/50 and SMA 200, 20- and 55-day breakouts, efficiency ratio, regime, 7- and 30-day realized volatility and its
  one-year percentile, funding level/percentile/flips, Hyperliquid vs Binance/Bybit predicted funding, open interest
  against price, crowding and liquidation flags, breadth and leadership), a market **mood** (risk-on / risk-off /
  mixed), **changes since the previous report**, the day's **news, prediction-market odds and trending coins**, a
  **scorecard** (how each label was followed by the market 7 days later, replayed over the past year in-sample and
  out-of-sample as published labels accumulate), and the analyst's **brief** with its translations. The report
  document shape is `Snapshot` in `kokoah-agent/src/store.ts` and the ingest validation in `docs/API.md` "Pulse".
- **News feed** every 15 minutes: regulators, exchange listings and delistings, crypto and general press, Polymarket
  odds, CoinGecko trending; items tagged by rules and rated by the model (importance 1–3, direction, topic, coins, a
  gist); up to 500 items, replaced on each publish. `GET /v1/pulse/news/top` gives the newest high-impact items.
- **Market news** every 6 hours (about 51 editions, kept indefinitely): a fact sheet of Hyperliquid prices, open
  interest and funding, outside markets (oil, gold, the 10-year yield, the dollar index, index futures, the VIX), the
  economic calendar, Fear & Greed, stablecoin supply, odds and the rated news; the model writes headline, summary,
  drivers by category and "what comes next", checked against the facts and translated.
- A local dashboard renders the latest report, the archive, the scorecard and the research reports
  (`kokoah-agent/docs/research/01..03`).

**Top traders and following.** Hyperliquid's public leaderboard file every 10 minutes (accounts with 100k+ USDC or
10M+ volume), per-trader stats (30-day drawdown, active days), followed traders polled every 30 s or more, trader
pages with live positions from the node. The scanner's "smart" cohort is the leaderboard's 200 best by all-time PnL.

**AI read track record.** Every written AI market read keeps its lean (bullish / bearish / neutral) with the price at
the time (`ai_read_leans`, a year); a worker scores it with the price 4 h, 24 h and 7 d later from the 1m candle
store; public `GET /v1/ai/markets/track-record` shows hits per lean against an up-share baseline.

**Notifications.** One outbox for inbox, web push and Telegram, with dedupe keys, per-type mutes, quiet hours and an
hourly cap. Types include the Pulse ones (`pulse_report`, `pulse_headline` opt-in; `pulse_digest` on by default for
Telegram-linked users), `market_alert`, `bot_signal`, `trader_activity`, `top_traders`, `daily_brief`, `trade_review`,
`weekly_review`, `announcement`.

**Freshness in one table.**

| Data | Fresh every | Depth kept |
|---|---|---|
| Node account reads, 1m node candles | seconds (node lag about 0.4 s when healthy; minutes to hours during incidents) | candles: 1m 90 days, higher intervals indefinitely |
| Order flow per minute | seconds | 30 days (per address 7 days) |
| Position snapshots | 1–30 min by position size | a 30-minute window, cohort history 30 days |
| Whale events, TWAPs | seconds | 7 days, 3 days |
| Market signals, scanner | 5 min | 20 min shown, cohort points 30 days |
| Leaderboard | 10 min | current table |
| Pulse news | 15 min | the latest feed |
| Pulse market news | 6 h | all editions since 2026-10-07 |
| Pulse reports | 12 h | all reports since 2026-09-29 |
| Fills history store | seconds | 1 year rolling (on mainnet since 2026-10-06) |

## 4. AI and agent features today

**Rules that bind every AI surface** (`FRONTEND_SPEC.md` §15, `TELEGRAM_AI.md`, `HELP_ASSISTANT.md`): the AI never
sets numbers or submits anything; it explains, or proposes a structured ticket or rule the user confirms; every number
in an answer is checked against a fact sheet built in code (one retry, then a plain refusal); no predictions, targets
or buy/sell advice; user text, news and screenshots are data, never instructions; nothing the user wrote is stored
beyond counts, tokens and latency. The provider is configurable (Anthropic, OpenAI, Gemini, OpenRouter); production
uses a Gemini Flash model through OpenRouter, capped at 50 requests per user a day, $1 per user and $20 per server a
day.

**Model-based today.**
- Pulse analyst (`kokoah-agent/src/analyst.ts`): writes the brief from a fact sheet of the computed signals; kept only
  if every number is in the sheet and it gives no advice; translated into the 10 languages with the same check.
  Also the news rater and the market-news writer.
- Backend AI (`/v1/ai/*`): explain-risk, plain-English order → ticket (`intent`), plain-English guardian → rule
  (`rule`), weekly journal review, and the **AI reads** of a trader, a market or the scanner (facts in code: bull and
  bear case, what to watch, a lean "a reading, not a forecast"; one fresh answer shared by everyone for 60/15/30
  minutes).
- The **Kokoah assistant** (`POST /v1/ai/help`): call 1 sorts the question (platform, account, market, trade, manage,
  automation, or a fixed refusal) and picks docs sections from the site's `llms.txt` (about 470 sections); call 2
  answers from those sections, the account's FACTS (positions, orders, fills, funding, alerts), MARKET (candle store,
  node order flow, funding and OI, positioning, the 30 largest perps) and NEWS (the feed, odds, the latest edition);
  manage and automation requests become checked actions the app confirms.
- The **Telegram assistant** (`src/assistant/`): classifies a message, answers from FACTS, drafts trades and alerts
  that ask back for what is missing, transcribes voice notes; one sentence per trade review.

**Rule-based today** (no model call): Pulse signals, regimes and the scorecard; guardians (`@kokoah/shared/guardians`
`evaluateRule` is deterministic, cloids derived from the rule id); bot planners (grid, DCA, copy, signal, indicator
rules on shared indicator math); market alerts; price-step alerts with a "why it moved" line; the pre-trade check
(`/v1/precheck`: range, noStop, leverage, thin, slippage, funding, news, topTraders); the morning brief (templates over
the shared report plus the user's numbers); help near liquidation; follow digests and "top traders turning" notices.

**Designs on the table, partly built.**
- `reviews/2026-10-03/agentic-trading.md`: a signals pipeline (families `trend-pullback`, `crowding-fade`,
  `squeeze-break`, `news-gate`; a `Signal` schema with `strength` from a Wilson lower bound of past hit rate, a per-family
  scorecard showing losers too, a backtester with leakage traps), three modes Watch / Propose / Act, a deterministic
  sizer and critic, the signers as the only authority. Not built.
- `reviews/2026-10-03/agent-os.md` ("Kokoah Agents"): any external agent (an LLM over MCP, a TradingView alert, a
  script) trades a user's account under a per-agent signed limit card; a scoped bearer token, a machine API
  `/v1/agent-api/*` (markets, candles, funding, Pulse report and news, account, intents, plans, decisions, SSE events,
  a webhook door), a local MCP server and CLI (`@kokoah/agent-kit`), decision records and a per-agent scorecard, and
  in Phase B published agents others subscribe to, paid from the builder fee. **Adopted on 2026-10-12.** Built so far:
  policy format 3 (the agent fields in the signed limits) is on `kokoah-shared` main and the extension's wallet decoder
  is merged; nothing is deployed and the machine API, MCP server and marketplace do not exist yet.
- `agent-hub` is the team's own agent infrastructure (Discord room, approvals, limits), not a product feature.

## 5. Where a subnet's output could plug in

Concrete surfaces that exist, with the mechanism each already uses:
- **Discover → Pulse and Simple's Trade strip.** Reports arrive through authenticated ingest endpoints
  (`PUT /v1/pulse/ingest/reports/:id`, `/news`, `/digests/:id`; bearer token; a validated minimum shape, extra fields
  kept as sent) and the app reads them publicly. The strip (`kokoah-frontend/src/features/PulseStrip.tsx`) shows the
  newest report's mood and headline. A subnet result could be a new section of the report document, a new document
  type behind the same ingest pattern, or a card on the Pulse page next to the scorecard ("how past reads did").
- **Inbox, push and Telegram notices.** `pulse_report` and `pulse_headline` are opt-in notices created in the ingest
  transaction with dedupe keys; `pulse_digest` goes to Telegram-linked users by default; each edition is posted to the
  public Telegram channel. A subnet notice would be one more notification type with the same mutes and caps.
- **Market scanner and the chart's positioning panel.** Both show AI read cards and signal chips computed in code; a
  subnet reading per market could be a card or a column with its own "as of" and track record.
- **Pre-trade check and the assistants.** The check's items are codes with a severity; the assistants answer from
  FACTS and NEWS built in code. A subnet output that is a number or a label with a timestamp can enter both; free text
  cannot (every number must be verifiable against a fact sheet).
- **Bots.** TradingView signal bots take an order-shaped alert at `POST /v1/hooks/tv/:token` (`SignalAlertSchema`,
  per-user token, once per alert id, under the user's signed limits and the loss lock); indicator bots act at candle
  closes. A subnet signal delivered to a user's own signal bot is the only path that exists today for an outside
  signal to trade, and it is opt-in per user. The agentic-trading `Signal` schema and the Agent OS strategy fan-out
  (`POST /v1/agent-api/strategies/:id/signals`, Phase B) are the designed homes for signals and are not built.
- **Machine API and MCP (proposed).** The read tools list Pulse report and news; a subnet feed would be one more read
  tool and resource.

**What Kokoah could serve validators.** Everything below derives from public Hyperliquid chain data, so anyone with a
node can recompute it, and the backend already serves it without a session (per-IP limits of 20 to 120 requests a
minute, `Cache-Control` of 5 to 60 s; Cloudflare blocks an IP above 150 requests per 10 s):
- objective price and flow ground truth: 1m candles of every market built from fills and matching Hyperliquid's own
  (`GET /v1/candles`), order flow per minute with liquidations (`/flow`, `/flow/series`), position snapshots,
  liquidation and entry bins and cohort history (`/positioning`, `/positioning/history`), whale events, TWAPs, the
  scanner; the fill index states its coverage and gaps, which a scoring rule can respect;
- labels and leans with outcomes: Pulse reports with their scorecard, AI read leans scored at 4 h / 24 h / 7 d, the
  rated news feed with `publishedAt`, market news editions, the leaderboard and trader stats;
- depth today: candles since late September 2026 (1m 90 days), fills and flow since the mainnet start on 2026-10-06
  (flow 30 days, fills a year rolling), Pulse since 2026-09-29. Latency: seconds for node-derived data when the nodes
  are current, 5 minutes for signals, 15 minutes for news, 6 and 12 hours for the editions and reports. Node lag
  incidents of minutes to hours happened several times between 2026-10-07 and 2026-10-19 (peering is being fixed; a
  paid peering decision is open), so a validator must treat node-derived data as sometimes stale and prefer sources
  that carry their own timestamps.

## 6. Constraints and standing decisions the subnet must respect

- **Never trade from a server except through the signer service.** The backend has no `/exchange` code path; orders
  and cancels are signed only by agent keys, in the signer frame (browser) or the signer service (server) after a
  policy the user signed. The subnet repo's own rule (`kokoah-subnet/.claude/CLAUDE.md`): it reads public market data
  only, never trades, never holds Hyperliquid keys, never writes to `/exchange`. Anything that acts for a user goes
  through the existing signers under signed limits (Agent OS is the designed door).
- **Testnet by default, mainnet writes behind flags.** `HL_NETWORK` defaults to testnet; a server-side process refuses
  to sign or submit to mainnet unless `ALLOW_MAINNET_WRITES=true`; the shared `assertWriteAllowed` sits at the point of
  signing (`kokoah-infra/docs/ENGINEERING_CONVENTIONS.md` §7). Reads on mainnet need no flag; the Pulse agent reads
  mainnet read-only with no keys, which is the pattern for the subnet. For Bittensor the same habit applies: localnet,
  then testnet, mainnet only with the owner's go-ahead per transaction (registration costs hundreds of TAO).
- **AI rules.** Numbers come from code; AI text never sets sizes, prices or leverage; no predictions or buy/sell
  advice on any user-facing surface; every number verified; outside text is fenced data. Market analysis is labelled
  "for information only, not investment advice"; readings carry "a reading, not a forecast" and an "as of" time.
- **Legal posture.** Tool, not adviser. Region gating exists (`copyTrading` off in the EU and GB pending counsel; the
  proposed `agentTrading` follows it, and the US is blocked for it); a legal review and a pentest are listed as
  pre-launch items that are still open; MiCA and CFTC questions on signals and paid strategy publishing are open in
  `agentic-trading.md` §9.5 and `agent-os.md` §10.
- **Privacy.** No wallet addresses other than the user's own reach a model; no personal data, prompts or intentions in
  code, commits or docs; nothing from `/kokoah/.env` is printed or logged; logs never carry keys, signatures, tokens
  or bodies of signing endpoints. User-facing text ships in 11 languages.
- **Repos and process.** The app repos are read-only for this project; nothing is imported either way. Commits by the
  one project author, short imperative messages, the repo's exact CI steps before a push, no deploys, cloud
  infrastructure, GPU rental or paid API keys without asking (every time). Redeploying kokoah.com after verified
  changes is the one standing permission. The old test domain is never named.
- **Shared machine.** Not ours: Postgres on 5432/5433, the `vocence-pg` container, port 8080. Kokoah's: the local stack
  on 5173, 58080, 47802, 55432, 55433 and 58081, the live API on 58180, the Pulse dashboard on 9991. The subnet owns
  9944/9945 (localnet) and 48100–48199; its containers are named `kokoah-subnet-*`.
- **Hyperliquid budgets.** The backend's 1,200-weight-per-minute budget is fully allocated across its jobs and shares
  the IP with the signer's orders; the nodes' proxies admit the backend's address only. A subnet process must use its
  own IP budget against the public API (as the Pulse agent does) or a read path the owner opens on purpose.
- **Retention and shape.** Public reads are cached and rate-limited; ingest documents are validated and kept as sent;
  reports are kept indefinitely, user activity 180 days.

## 7. Open questions only the owner can answer

1. **What the subnet produces.** Directional market readings per market and horizon, rated news, written briefs,
   data serving, or something else? Today code-based labels are allowed on every surface and model text is never a
   signal; a subnet that emits forecasts would need a decision on how (and whether) they are shown.
2. **Where it shows first.** A section in the Pulse report, a Discover tab, a card on the scanner and chart, a notice
   type, the assistants' facts, or signal bots. Each has a different legal and UX weight.
3. **Whether miners may act.** Information only, or trading agents under signed limits through Agent OS Phase B
   (publish and subscribe, paid from the builder fee), which is adopted in principle but unbuilt.
4. **Data for validators.** Whether validators use the public API endpoints as they are, a dedicated read endpoint
   with keys and quotas, or must run their own Hyperliquid nodes; and whether the fill-index history store becomes
   public (cost and abuse are the concerns).
5. **Ground truth and horizons.** Which price source (the node's 1m candles, Hyperliquid's public candles, marks)
   and which horizons (4 h / 24 h / 7 d match the AI read track record; the Pulse scorecard uses 7 days).
6. **Money.** TAO for registration and operations, who runs the owner's validator and where, whether builder-fee
   revenue flows to the subnet, and how emissions relate to the marketplace's author share.
7. **Legal.** Whether subnet outputs are "advice" or a collective scheme in the EU and UK, which regions see them,
   and the wording and disclaimers, together with the pending legal review.
8. **Relation to the Pulse analyst.** Whether subnet outputs replace, feed or sit beside the twice-daily model brief
   and the AI reads, and whether the 11-language requirement applies to them (a translation pipeline costs paid
   calls per language).
9. **Node operations.** The nodes are the richest data source and the least stable component right now; the paid
   peering decision and whether Tokyo becomes the main node affect what the subnet can promise about freshness.
10. **Identity.** The subnet's public name and whether miners and validators are Kokoah users, outside operators, or
    both; and whether the agent-hub room is where subnet operations are run.
