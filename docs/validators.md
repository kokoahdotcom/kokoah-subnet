# Validator guide

A Kokoah subnet validator scores every miner once a day from public data and sets weights on chain. It reads each
linked account's own fills and account history on Hyperliquid, the exchange's daily file of Kokoah builder fills,
and the chain; it holds no Hyperliquid keys and never trades. Every validator running the same code on the same
public data computes the same scores.

This guide covers setup, configuration, operation and monitoring. What the validator computes is described in the
[whitepaper](whitepaper.md) and, formula by formula, in the [specification](design/02-mechanism-spec.md).

**Networks.** The subnet runs on the Bittensor test network as **netuid 385** today. The mainnet netuid will be
published at launch. Examples use the test network (`--network test`); on mainnet use `--network finney`.

## Contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Wallet and registration](#3-wallet-and-registration)
4. [The record (database)](#4-the-record-database)
5. [Configuration](#5-configuration)
6. [Run](#6-run)
7. [What a validator does each day](#7-what-a-validator-does-each-day)
8. [The public interface](#8-the-public-interface)
9. [Monitoring, alerts and backups](#9-monitoring-alerts-and-backups)
10. [Joining late: how a new validator converges](#10-joining-late-how-a-new-validator-converges)
11. [Upgrades](#11-upgrades)
12. [Security](#12-security)
13. [Troubleshooting](#13-troubleshooting)

## 1. Requirements

- **A hotkey registered on the subnet with a validator permit.** The chain grants permits to the subnet's top
  validators by stake; weights from a hotkey without one are not accepted.
- **A small Linux server.** Two CPU cores, 4 GB of memory and 20 GB of disk are plenty. Only outbound HTTPS is
  needed (Hyperliquid's public API, its explorer and data files, and a Bittensor endpoint); no inbound port has to
  be open unless you publish the interface (section 8).
- **Python 3.12 and [uv](https://docs.astral.sh/uv/)**; **Docker** for the PostgreSQL record and the optional
  dashboards.
- **A reliable clock** (NTP): days are UTC days.

## 2. Install

```bash
git clone https://github.com/kokoahdotcom/kokoah-subnet
cd kokoah-subnet
uv sync                         # installs the exact versions from uv.lock
scripts/check.sh                # optional: lint, types and the offline test suite, as CI runs them
uv run kokoah-subnet verify     # optional: the specification's numerical checks
```

## 3. Wallet and registration

```bash
uv run btcli wallet create -w validator -H default                      # on a secure machine
uv run btcli tx burned-register --netuid 385 -w validator -H default --network test
uv run btcli stake add --help                                           # stake to reach a validator permit
```

The server needs **only the hotkey**: copy `~/.bittensor/wallets/validator/hotkeys/default` (and the coldkey's
*public* file, `coldkeypub.txt`) to the server, and keep the coldkey itself offline. Setting weights and reading the
chain use the hotkey alone.

## 4. The record (database)

Every validator keeps a complete record of what it read and decided: every fill and why it counted or not, every
miner's figures with the parts behind them, every account value point, every agent check, every weight submission.
The tables are described in the [record guide](database.md).

```bash
scripts/db.sh start      # PostgreSQL in Docker on 127.0.0.1:48110; writes the URL to ~/.local/share/kokoah/subnet-db.env
set -a; . ~/.local/share/kokoah/subnet-db.env; set +a     # exports KOKOAH_SUBNET_DB_URL for the commands below
```

Without a database URL the validator uses a SQLite file in its data directory, which is fine for trials.

## 5. Configuration

`kokoah-subnet validator run` takes its settings as options:

| Option | Value | Notes |
|---|---|---|
| `--network` | `test` or `finney` | The Bittensor network. |
| `--netuid` | `385` on testnet | The subnet. |
| `--builder` | `0x43ccc0618e4358494f87a4b8699230203dc4ab67` | Kokoah's builder address: whose builder fills are scored. |
| `--burn-hotkey` | testnet: `5E1ZiPv2T1qTCUjmbkGZr7CZYVcvWSQ25iUd9DMFx5JnrnEz` | The subnet's burn hotkey, published by the operator. It receives a day only when no miner has any weight. |
| `--agent-service` | `https://api.kokoah.com/miners` | Where the list of released agent keys is read. Without it, no account counts as a verified agent. |
| `--data-dir` | a directory | Cached builder files and local state. |
| `--db-url` | default `$KOKOAH_SUBNET_DB_URL` | The record; SQLite in `--data-dir` when unset. |
| `--wallet`, `--wallet-hotkey`, `--wallet-path` | your wallet | The validator's hotkey. |
| `--start-day` | `YYYY-MM-DD` | The first day this validator scores (section 10). |
| `--hl-info-url` | default Hyperliquid's public API | Point it at your own Hyperliquid node or endpoint to avoid public rate limits. |
| `--tao-usd-fallback` | a price | TAO/USD used only if the exchange lists no TAO perpetual. |
| `--poll-seconds` | default `60` | How often the loop wakes up. |

## 6. Run

**One pass**, to check the configuration: score the pending days, submit weights, exit.

```bash
uv run kokoah-subnet validator run --network test --netuid 385 \
    --builder 0x43ccc0618e4358494f87a4b8699230203dc4ab67 \
    --burn-hotkey 5E1ZiPv2T1qTCUjmbkGZr7CZYVcvWSQ25iUd9DMFx5JnrnEz \
    --agent-service https://api.kokoah.com/miners \
    --data-dir ~/kokoah-subnet-data --wallet validator --wallet-hotkey default \
    --start-day <YYYY-MM-DD> --once
```

**As a service.** Drop `--once` to run the loop. On a server, run it under systemd with the units in
`deploy/systemd/` (adjust `WorkingDirectory` and the paths in `ExecStart` to where you cloned the repository; the
units read the database settings written by `scripts/db.sh start`):

```bash
sudo cp deploy/systemd/kokoah-subnet-validator@.service deploy/systemd/kokoah-subnet-api.service \
        deploy/systemd/kokoah-subnet-check.* deploy/systemd/kokoah-subnet-backup.* /etc/systemd/system/
mkdir -p ~/.local/share/kokoah
echo 'KOKOAH_SUBNET_ARGS=--network test --netuid 385 --builder 0x43ccc0618e4358494f87a4b8699230203dc4ab67 --burn-hotkey 5E1ZiPv2T1qTCUjmbkGZr7CZYVcvWSQ25iUd9DMFx5JnrnEz --agent-service https://api.kokoah.com/miners --data-dir /home/<you>/kokoah-subnet-data --wallet validator --wallet-hotkey default --start-day <YYYY-MM-DD>' \
    > ~/.local/share/kokoah/subnet-validator-main.env
sudo systemctl daemon-reload
sudo systemctl enable --now kokoah-subnet-validator@main kokoah-subnet-api kokoah-subnet-check.timer kokoah-subnet-backup.timer
```

Logs go to `~/.local/share/kokoah/subnet-validator-main.log`. Restarts are safe at any time: scoring resumes with
the oldest unscored day, and a re-scored day starts from the state as it was before that day.

## 7. What a validator does each day

All times are UTC.

1. **After midnight**, the previous day becomes pending.
2. **The exchange's daily builder file** (fills carrying Kokoah's builder code) is published within hours. The
   validator fetches it, records its hash and compares it with the hash the operator commits on chain. It waits up
   to 12 hours; after that it scores the day from the miners' own histories alone and records why.
3. **The data plane.** For every linked account: its own fill history (the primary source, merged with the file;
   a fill seen only in the history counts once its order is confirmed as a Kokoah order from the account's own
   signed transactions), its recorded account values (for the $10,000 daily average), flows and positions, and the
   accounts linked to it (to name counterparties and detect self-dealing). Every bound account's link and every
   agent machine are checked.
4. **Scoring.** Eligibility, fee credit, skill, the entry test, the copy rule, agent verification, and the two
   rewards, exactly as the specification states. The day's artifact (every miner's figures) is stored.
5. **Weights.** Submitted every tempo (360 blocks, about 72 minutes) from the latest scored day. The SDK follows the
   subnet's settings, including commit-reveal.

A failed read of a miner's fill history, the explorer or the recorded account values fails that day's scoring
rather than scoring it with missing data; the validator retries within the hour. Other reads (positions, fee rates,
stake) fall back, and the failure is recorded.

## 8. The public interface

`kokoah-subnet validator serve` (the `kokoah-subnet-api` unit) serves the record read-only on `127.0.0.1:48100`:

| Path | Content |
|---|---|
| `/` | The leaderboard page |
| `/summary` | Headline figures |
| `/health` | Liveness and the latest scored day |
| `/days`, `/days/latest`, `/days/<YYYY-MM-DD>` | The daily artifacts: every miner's figures |
| `/bindings` | The accepted links between hotkeys and accounts |
| `/leaderboard` | Ranks and pay |

To publish it, put it behind a reverse proxy with TLS; `deploy/nginx/kokoah-subnet.conf` is a template with a
request limit. Publishing lets miners and other validators compare figures with yours.

## 9. Monitoring, alerts and backups

- **Health check.** `uv run kokoah-subnet validator check` prints the latest scored day, the last weights, the last
  epoch, errors in the last 24 hours and the builder-earnings check, and exits 1 on a problem. The
  `kokoah-subnet-check.timer` runs it hourly and posts to a webhook only when something is wrong: put
  `KOKOAH_SUBNET_ALERT_WEBHOOK=<Discord or Slack webhook URL>` in `~/.local/share/kokoah/subnet-alert.env`.
- **The earnings check.** The builder fees credited to miners must never exceed what Hyperliquid reports the builder
  has earned; the health check alarms if they do.
- **Dashboards.** `scripts/dashboard.sh start` runs Grafana on `127.0.0.1:48120` with the record as its data source
  and the dashboards in `dashboards/`.
- **Backups.** `kokoah-subnet-backup.timer` dumps the record daily (14 kept) to `~/.local/share/kokoah/backups/`;
  `scripts/db.sh backup <file>` does it by hand.

## 10. Joining late: how a new validator converges

A validator that starts today cannot read everything an older validator recorded:

- **Account values.** The daily $10,000 average comes from the exchange's recorded account values, which it keeps in
  detail for about a week. Start with `--start-day` at most **seven days ago**; older days would be scored with
  unknown account values (ineligible).
- **Fee credit** is smoothed day by day; a new validator's credit matches the others' within about two weeks.
- **Skill.** The entry test needs 90 days of daily returns. About 30 days are backfilled from the exchange; the rest
  is the validator's own record. Until a new validator has 90 days, its skill reward joins its volume reward, so its
  weights differ from older validators' by up to the skill share. This is expected and resolves itself.

## 11. Upgrades

```bash
git pull
uv sync
sudo systemctl restart 'kokoah-subnet-validator@*' kokoah-subnet-api
```

- **Parameter changes** are versioned in the code, announced at least seven days before they take effect, and
  published in every daily artifact as the mechanism's version. Nothing changes until you update.
- **New agent harness images** become valid only when their digest is added to the approved list in the code
  (`src/kokoah_subnet/agent/images.py`), so they arrive with an update. Each approved image is a reproducible build:
  `deploy/agent/build.sh <commit>` rebuilds it and prints the same digest.

## 12. Security

- The server holds **only the hotkey**; keep the coldkey offline.
- The validator **never holds Hyperliquid keys** and never writes to the exchange.
- The record and the interface are local by default; publish the interface only through a reverse proxy.
- Keep the server patched and the repository on a tagged release or a commit you have reviewed.

## 13. Troubleshooting

| Symptom | What to check |
|---|---|
| Weights are not accepted | The hotkey needs a validator permit (enough stake); the chain also limits how often weights can be set. |
| A day stays pending | The builder file may be late; after 12 hours the day is scored from the histories. Check the log and `validator check`. |
| Errors about the exchange's API | Public rate limits or an outage; the day is retried within the hour. A dedicated endpoint (`--hl-info-url`) avoids rate limits. |
| "no TAO perpetual" | Pass `--tao-usd-fallback <price>`. |
| No verified agents | `--agent-service https://api.kokoah.com/miners` must be set and reachable. |
| Different figures from another validator | Compare `/days/<day>` artifacts; a late start (section 10) explains differences in skill for up to 90 days. |
| The exchange's history itself was unavailable for a whole day | The operator announces it; close the day with `uv run kokoah-subnet validator skip-day --day YYYY-MM-DD --burn-hotkey <ss58>`. |
