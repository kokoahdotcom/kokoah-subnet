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
10. [Choosing the start day](#10-choosing-the-start-day)
11. [Upgrades](#11-upgrades)
12. [Security](#12-security)
13. [Troubleshooting](#13-troubleshooting)

## 1. Requirements

- **A hotkey registered on the subnet with a validator permit.** The chain grants permits to the subnet's top
  validators by stake; weights from a hotkey without one are not accepted.
- **A small Linux server.** Two CPU cores, 4 GB of memory and 20 GB of disk are plenty. Outbound only: HTTPS to
  Hyperliquid's public API, its explorer and data files and the subnet's service, a websocket to a Bittensor
  endpoint, and plain HTTP to each agent machine at the host and port its binding publishes (the agent check). No
  inbound port has to be open unless you publish the interface (section 8).
- **Python 3.12 and [uv](https://docs.astral.sh/uv/)**; **Docker** for the PostgreSQL record and the optional
  dashboards.
- **A reliable clock** (NTP): days are UTC days.

## 2. Install

```bash
git clone https://github.com/kokoahdotcom/kokoah-subnet
cd kokoah-subnet
uv sync                         # installs the exact versions from uv.lock
scripts/check.sh                # optional: lint, types, the offline tests and the checks below, as CI runs them
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
scripts/db.sh start      # PostgreSQL in Docker on 127.0.0.1:48110; writes the URLs to ~/.local/share/kokoah/subnet-db.env
set -a; . ~/.local/share/kokoah/subnet-db.env; set +a     # exports KOKOAH_SUBNET_DB_URL for the commands below
```

The env file holds two connections: `KOKOAH_SUBNET_DB_URL` for the validator, which writes the record, and
`KOKOAH_SUBNET_DB_READER_URL`, a read-only role for the interface, the health check and the dashboards, which only
read it. Without a database URL the validator uses a SQLite file in its data directory, which is fine for trials.

## 5. Configuration

`kokoah-subnet validator run` takes its settings as options:

| Option | Value | Notes |
|---|---|---|
| `--network` | `test` or `finney` | The Bittensor network. |
| `--netuid` | `385` on testnet | The subnet. |
| `--builder` | `0x43ccc0618e4358494f87a4b8699230203dc4ab67` | Kokoah's builder address: whose builder fills are scored. |
| `--burn-hotkey` | testnet: `5E1ZiPv2T1qTCUjmbkGZr7CZYVcvWSQ25iUd9DMFx5JnrnEz` | The subnet's burn hotkey, published by the operator. It receives a day only when no miner has any weight. |
| `--agent-service` | `https://api.kokoah.com/miners` | The subnet's service: the released agent keys and the unlinks signed by linked addresses. Without it, no account counts as a verified agent and no unlink is applied. |
| `--data-dir` | a directory | Cached builder files and local state. |
| `--db-url` | default `$KOKOAH_SUBNET_DB_URL` | The record; SQLite in `--data-dir` when unset. |
| `--wallet`, `--wallet-hotkey`, `--wallet-path` | your wallet | The validator's hotkey. |
| `--start-day` | `YYYY-MM-DD` | The first day this validator scores (section 10). |
| `--hl-info-url` | default Hyperliquid's public API | Point it at your own Hyperliquid node or endpoint to avoid public rate limits. |
| `--tao-usd-fallback` | a price | TAO/USD used only if the exchange lists no TAO perpetual. |
| `--poll-seconds` | default `60` | How often the loop wakes up. |

## 6. Run

**One pass**, to check the configuration: score the oldest pending day, submit weights, exit.

```bash
uv run kokoah-subnet validator run --network test --netuid 385 \
    --builder 0x43ccc0618e4358494f87a4b8699230203dc4ab67 \
    --burn-hotkey 5E1ZiPv2T1qTCUjmbkGZr7CZYVcvWSQ25iUd9DMFx5JnrnEz \
    --agent-service https://api.kokoah.com/miners \
    --data-dir ~/kokoah-subnet-data --wallet validator --wallet-hotkey default \
    --start-day <YYYY-MM-DD> --once
```

**As a service.** On a server, run the validator, its interface and its health check as a dedicated user with
systemd's sandboxing (the unit files in `deploy/systemd/`, described in its README):

```bash
sudo useradd --system --home-dir /var/lib/kokoah-subnet --create-home --shell /usr/sbin/nologin kokoah-subnet
# in the repository; Python goes under /opt too, so the service user can run it
sudo UV_PYTHON_INSTALL_DIR=/opt/kokoah-subnet/python UV_PROJECT_ENVIRONMENT=/opt/kokoah-subnet/venv \
    uv sync --frozen --no-dev --no-editable --reinstall-package kokoah-subnet --python 3.12 --python-preference only-managed
sudo install -d -m 755 /opt/kokoah-subnet/scripts && sudo install -m 755 scripts/db.sh scripts/backup.sh /opt/kokoah-subnet/scripts/
sudo chmod -R a+rX /opt/kokoah-subnet

# data and the hotkey only (never the coldkey): the wallet's hotkey file and its coldkeypub.txt
sudo install -d -o kokoah-subnet -g kokoah-subnet -m 700 /var/lib/kokoah-subnet/data \
    /var/lib/kokoah-subnet/wallets/validator/hotkeys
sudo install -o kokoah-subnet -g kokoah-subnet -m 600 ~/.bittensor/wallets/validator/hotkeys/default \
    /var/lib/kokoah-subnet/wallets/validator/hotkeys/default
sudo install -o kokoah-subnet -g kokoah-subnet -m 600 ~/.bittensor/wallets/validator/coldkeypub.txt \
    /var/lib/kokoah-subnet/wallets/validator/coldkeypub.txt

# settings (readable by the service's group only) and logs
sudo install -d -m 755 /etc/kokoah-subnet && sudo install -d -g kokoah-subnet -m 775 /var/log/kokoah-subnet
sudo install -g kokoah-subnet -m 640 ~/.local/share/kokoah/subnet-db.env /etc/kokoah-subnet/db.env
grep '^KOKOAH_SUBNET_DB_READER_URL=' ~/.local/share/kokoah/subnet-db.env \
    | sudo install -g kokoah-subnet -m 640 /dev/stdin /etc/kokoah-subnet/db-reader.env
echo 'KOKOAH_SUBNET_ARGS=--network test --netuid 385 --builder 0x43ccc0618e4358494f87a4b8699230203dc4ab67 --burn-hotkey 5E1ZiPv2T1qTCUjmbkGZr7CZYVcvWSQ25iUd9DMFx5JnrnEz --agent-service https://api.kokoah.com/miners --data-dir /var/lib/kokoah-subnet/data --wallet validator --wallet-hotkey default --wallet-path /var/lib/kokoah-subnet/wallets --start-day <YYYY-MM-DD>' \
    | sudo install -g kokoah-subnet -m 640 /dev/stdin /etc/kokoah-subnet/validator-main.env

sudo cp deploy/systemd/kokoah-subnet-validator@.service deploy/systemd/kokoah-subnet-api.service \
        deploy/systemd/kokoah-subnet-check.* deploy/systemd/kokoah-subnet-backup.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kokoah-subnet-validator@main kokoah-subnet-api kokoah-subnet-check.timer kokoah-subnet-backup.timer
```

Logs go to `/var/log/kokoah-subnet/validator-main.log`. Restarts are safe at any time: scoring resumes with the
oldest unscored day, and a re-scored day starts from the state as it was before that day.

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
   agent machine are checked, and the unlinks the addresses signed are read from the subnet's service (each
   signature is checked; every unlink seen is kept).
4. **Scoring.** Eligibility, fee credit, skill, the entry test, the copy rule, agent verification, and the two
   rewards, exactly as the specification states. The day's artifact (every miner's figures) is stored.
5. **Weights.** Submitted every tempo (360 blocks, about 72 minutes) from the latest scored day. The SDK follows the
   subnet's settings, including commit-reveal.

A failed read of a miner's recorded account values, a fill history, the explorer, a linked account, the minute
candles or the unlinks fails that day's scoring, and the validator retries within the hour, until 12 hours after the
day ends, and never sooner than 3 hours after its first attempt at the day; the days of a backlog share the first
one's window, so a validator back from an outage retries for 3 hours in all. After that the day is scored and only
what needed the failed read is lost: without the recorded account values the account is not eligible that day
(unless points an earlier read stored cover the whole day), and fills that needed a failed read go uncounted
(without a coin's candles or the fill history, also the day's return). The candles of a coin the exchange does not
list are settled at once the same way. Other reads (positions, fee rates, stake) fall back. Every failure is recorded.

## 8. The public interface

`kokoah-subnet validator serve` (the `kokoah-subnet-api` unit) serves the record read-only on `127.0.0.1:48100`:

| Path | Content |
|---|---|
| `/` | The leaderboard page |
| `/summary` | Headline figures |
| `/health` | Liveness and the latest scored day |
| `/days`, `/days/latest`, `/days/<YYYY-MM-DD>` | The daily artifacts: every miner's figures |
| `/bindings` | The accepted links between hotkeys and accounts |
| `/leaderboard` | Ranks and pay, with each account's record (90-day return, worst drop, losing days, verified agent days) |
| `/accounts/<0x address>` | One account's whole record: every stored day with its return, the growth curve and the scored figures |

To publish it, put it behind a reverse proxy with TLS; `deploy/nginx/kokoah-subnet.conf` is a template with a
request limit. Publishing lets miners and other validators compare figures with yours.

## 9. Monitoring, alerts and backups

- **Health check.** `uv run kokoah-subnet validator check` prints the latest scored day, the last weights, the last
  epoch, errors in the last 24 hours (routine notes are counted apart and are no problem) and the builder-earnings
  check, and exits 1 on a problem. The
  `kokoah-subnet-check.timer` runs it hourly and posts to a webhook only when something is wrong: put
  `KOKOAH_SUBNET_ALERT_WEBHOOK=<Discord or Slack webhook URL>` in `/etc/kokoah-subnet/alert.env`. A run that finds a
  problem also leaves the unit failed, so `systemctl --failed` and any monitor of systemd units show it; a webhook
  that refuses the report is noted in `/var/log/kokoah-subnet/check.log`.
- **The earnings check.** The builder fees credited to miners must never exceed what Hyperliquid reports the builder
  has earned; the health check alarms if they do.
- **Dashboards.** `scripts/dashboard.sh start` runs Grafana on `127.0.0.1:48120` with the record as its data source
  (through the read-only role) and the dashboards in `dashboards/`.
- **Backups.** `kokoah-subnet-backup.timer` dumps the record daily (14 kept) to `/var/backups/kokoah-subnet/`;
  `scripts/db.sh backup <file>` does it by hand. On a host that also runs the agent service, put
  `KOKOAH_SUBNET_AGENT_DATA=<its data directory>` in `/etc/kokoah-subnet/backup.env`: its database and the stored
  agent code are then backed up too, and a wrong path fails the backup instead of skipping them.

## 10. Choosing the start day

Set `--start-day` to a day within the last seven days: the daily average account value is read from the exchange's
recent record of each account. From that day on, the validator scores every day in order and builds its record day
by day; each account's earlier daily values are backfilled from the exchange when the account is first scored.

## 11. Upgrades

```bash
git pull
scripts/db.sh start      # already running: only makes sure the read-only role and its URL exist
grep '^KOKOAH_SUBNET_DB_READER_URL=' ~/.local/share/kokoah/subnet-db.env \
    | sudo install -g kokoah-subnet -m 640 /dev/stdin /etc/kokoah-subnet/db-reader.env
sudo UV_PYTHON_INSTALL_DIR=/opt/kokoah-subnet/python UV_PROJECT_ENVIRONMENT=/opt/kokoah-subnet/venv \
    uv sync --frozen --no-dev --no-editable --reinstall-package kokoah-subnet --python 3.12 --python-preference only-managed
sudo install -m 755 scripts/db.sh scripts/backup.sh /opt/kokoah-subnet/scripts/
sudo chmod -R a+rX /opt/kokoah-subnet
sudo cp deploy/systemd/kokoah-subnet-validator@.service deploy/systemd/kokoah-subnet-api.service \
        deploy/systemd/kokoah-subnet-check.* deploy/systemd/kokoah-subnet-backup.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart 'kokoah-subnet-validator@*' kokoah-subnet-api
```

`--reinstall-package` makes the sync install the pulled code even when the package's version number is unchanged.
The scripts and unit files are copied again, so changes to them take effect too.

- **Parameter changes** are versioned in the code, announced at least seven days before they take effect, and
  published in every daily artifact as the mechanism's version. Nothing changes until you update.
- **New agent harness images** become valid only when their digest is added to the approved list in the code
  (`src/kokoah_subnet/agent/images.py`), so they arrive with an update. Each approved image is a reproducible build:
  `deploy/agent/build.sh <commit>` rebuilds it and prints the same digest.

## 12. Security

- The server holds **only the hotkey**; keep the coldkey offline.
- The services run as their own user in systemd's sandbox, with a memory cap (`deploy/systemd/`).
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
| Different figures from another validator | Compare the `/days/<day>` artifacts, and check that both run the same release. |
| The exchange's history itself was unavailable for a whole day | The operator announces it; close the day with `uv run kokoah-subnet validator skip-day --day YYYY-MM-DD --burn-hotkey <ss58>`. Days are scored and closed in order only: the day must be the one after the latest scored day. |
