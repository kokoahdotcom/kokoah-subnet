# The record

Every validator keeps a complete record of what it saw and decided, in one relational database: PostgreSQL in
production (`scripts/db.sh`, the project's own container on 127.0.0.1:48110), SQLite for tests and quick local
runs. Dashboards and analysis read it directly; nothing in it affects pay. The schema is `src/kokoah_subnet/
validator/schema.py`; the tables, by what they answer:

| Question | Tables |
|---|---|
| What ran, with what settings? | `runs` (one row per validator start: network, netuid, hotkeys, versions, config) |
| What happened each scored day? | `days` (headline figures, file hash, whether it matched the operator's commitment, the full published artifact as JSON), `builder_files` (hash at scoring and at the later re-check) |
| Every trade, counted or not, and why not | `fills` (every row of the builder file and every builder fill from a bound address's own history: address, hotkey if bound, coin, side, price, size, notional, taker flag, builder fee, total fee, counterparty, closed PnL, `excluded` = spot / unbound / cluster / bound_counterparty / mark_clip / unchecked_counterparty (a linked read failed after the deadline) / no_mark (the coin's minute candles could not be read) / NULL, `source` = file / history / both, trade id, client order id) |
| Did every credited fee reach the builder? | `builder_rewards` (what the exchange reported the builder had earned, at every scoring run; the health check compares it with the builder fees of all counted fills) |
| Every miner's figures and the parts behind them | `miner_days` (credit, fees, notional, PnL, smoothed credit and fees, confidence, quality, raw and smoothed skill, gates with their counts, mirror shares, pay in alpha and USD, exchange fee rates; the two pools (v6): volume and skill pay, the skill weight with its parts (skill above the floor, the volume window and factor), the entry test, agent verified, the copy group, why not in the skill pool; columns of earlier versions (holding, multiplier, caps) stay empty; eligibility: eligible, the reason when not, the day's average account value and its point count, qualification progress and the trade that completed it, the credited part of the day, an identity reset and its reason, the registration block) |
| The smoothed state over time | `state_history` (per day, with the full state as JSON), `states` (latest), `hotkey_swaps` (same UID and coldkey, new hotkey: history follows, credit does not) |
| The equity floor | `equity_points` (the exchange's recorded account values per address, kept as read; the live and window-edge points are not kept), `equity_daily` (the day's time-weighted average, its point count, when the account's history begins, and why it is unknown when it is) |
| The trader's account | `equity` (account value, flows and off-market transfers per day, with the raw margin summary), `ledger_updates` (every deposit, withdrawal and transfer), `positions` (end-of-day positions per coin with size, entry, unrealized PnL, leverage), `activity` (round trips, fills, notional per day), `fee_rates` (the exchange's fee schedule for the address) |
| Who is who | `miners` (per hotkey: coldkey, uid, registration block and estimated time, first and last seen, bound address and when, first and last pay, total emission from the chain and total pay from the scoring, role), `neurons` (the metagraph at scoring time), `commitments` (every commitment seen each day), `bindings` (accepted now), `binding_events` (accepted / migrated / unlinked / refused with the reason), `unlinks` (every unlink seen, signed by the address: hotkey, address, time, signature, effective day, first seen), `penalties` (a mirror report by address, until which day), `clusters`, `cluster_edges` (coldkey, sub-account, transfer links) |
| What the chain paid, tempo by tempo | `epochs`, `epoch_neurons` (per UID per epoch: incentive, emission = alpha paid that epoch, dividends, rank, trust, consensus, stake, registration block) |
| The subnet's own emission, tempo by tempo | `subnet_epochs` (TAO injected per block, alpha in and out per block, pool reserves, the chain's `MinerBurned`, emission enabled): the TAO side of the burn question, over time |
| The inputs to the conversion rate | `price_samples` (pool rate per sampled block), `tao_marks`, `stake_samples` (own stake per hotkey per sampled block) |
| What the validator submitted | `weights_submitted` (uids, values, block, result) |
| What the operator burned | `burns` (per run of `owner burn`: staked, locked, burned, result) |
| Agent accounts | `agent_checks` (each day's check of every agent account: endpoint, code fingerprint, result and reason, image digest, hardware, instance) |
| The operator's agent service (its own database, not a validator's) | `agent_submissions` (every submitted bundle: hotkey, encrypted or not, code fingerprint, size, time), `agent_keys` (each agent account's key copy, encrypted with the miner's secret and wrapped with the service's key; hotkey, master, when stored and released), `agent_permits` (every start permission used, once each), `agent_key_events` (every store, restore and release), `subnet_unlinks` (every unlink accepted: hotkey, address, time, the address's signature, when received) |
| What went wrong | `errors` (day, hotkey, address, stage, message, and the level: `error` for a failure the health check raises, `note` for a routine fact it only records, such as history fills that are not Kokoah orders or a lookup cap reached) |

Conventions: `day` is the scored UTC day (YYYY-MM-DD); money is USD; alpha is in alpha, not rao; addresses are
lowercase; `raw` columns keep the source record as JSON. Registration and binding times are estimated from block
heights at 12 seconds a block; epoch and submission times are wall-clock at the validator.

Connect: `scripts/db.sh psql` opens a shell; the URLs are in `~/.local/share/kokoah/subnet-db.env`:
`KOKOAH_SUBNET_DB_URL`, which the validator and the burn command read when `--db-url` is not given, and
`KOKOAH_SUBNET_DB_READER_URL`, the read-only role `kokoah_reader` that the interface, the health check and Grafana
use (they read the record and never change it).
Back up with `scripts/db.sh backup file.dump`.

Schema changes: `create_all` adds new tables; column changes are numbered steps in
`src/kokoah_subnet/validator/migrations.py`, applied once per database (`meta.schema_version`).

Useful queries:

```sql
-- pay and burn per day
SELECT day, miners, miners_with_fills, volume_usd, builder_fee_usd, paid_alpha, burn_alpha, price_usd FROM days ORDER BY day;
-- a trader's history with the parts behind the multiplier
SELECT day, notional, builder_fee, confidence, quality, skill, entry_psr, entry_pass, skill_weight, volume_pay_alpha, skill_pay_alpha, pay_alpha FROM miner_days WHERE hotkey = '...' ORDER BY day;
-- agent checks and why they failed
SELECT day, hotkey, ok, reason, image_digest FROM agent_checks ORDER BY day DESC;
-- what the chain actually paid each UID per epoch
SELECT e.at, n.uid, n.hotkey, n.emission, n.incentive FROM epoch_neurons n JOIN epochs e ON e.block = n.block ORDER BY e.at, n.uid;
-- who was not eligible, and why
SELECT day, hotkey, ineligible_reason, avg_equity_usd, equity_points, qual_trades, qual_notional FROM miner_days WHERE NOT eligible ORDER BY day;
-- where the counted fills came from (the builder file has covered about the first half of each day)
SELECT day, source, count(*), sum(builder_fee) FROM fills WHERE hotkey IS NOT NULL AND excluded IS NULL GROUP BY day, source ORDER BY day;
-- why fills were not counted
SELECT day, excluded, count(*), sum(notional) FROM fills GROUP BY day, excluded ORDER BY day;
-- the TAO side of burning: the subnet's TAO injection per block against the share of the pool burned
SELECT s.at, s.tao_in_emission, s.alpha_out_emission, s.miner_burned, d.burn_alpha / d.pool_alpha AS burned_share
FROM subnet_epochs s LEFT JOIN days d ON d.day = to_char(s.at, 'YYYY-MM-DD') ORDER BY s.at;
```
