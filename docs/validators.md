# Validator guide

A Kokoah subnet validator scores every miner once a day from public data and sets weights on chain. It reads each
linked account's own fills and account history on Hyperliquid, the exchange's daily file of Kokoah builder fills,
and the chain; it holds no Hyperliquid keys and never trades. Every validator running the same code on the same
public data computes the same scores.

This guide covers setup, configuration, operation and monitoring. What the validator computes is described in the
[whitepaper](whitepaper.md) and, formula by formula, in the [specification](design/02-mechanism-spec.md).

**Network.** The subnet runs on Bittensor's main network as **netuid 39** (`--network finney --netuid 39`), and the
examples below use it.

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
- **Docker** for the PostgreSQL record, the setup with automatic updates (section 6) and the optional dashboards;
  **Python 3.12 and [uv](https://docs.astral.sh/uv/)** for the tools and the systemd setup.
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
uv run btcli tx burned-register --netuid 39 -w validator -H default --network finney
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
| `--network` | `finney` | The Bittensor network. |
| `--netuid` | `39` | The subnet. |
| `--builder` | `0x43ccc0618e4358494f87a4b8699230203dc4ab67` | Kokoah's builder address: whose builder fills are scored. |
| `--burn-hotkey` | optional, unset by default | The subnet's burn hotkey. A day on which no miner has any weight goes to it; unset, to the owner hotkey. Every validator uses the same setting. |
| `--agent-service` | `https://api.kokoah.com/miners` | The subnet's service: the released agent keys and the unlinks signed by linked addresses. Without it, no account counts as a verified agent and no unlink is applied. |
| `--data-dir` | a directory | Cached builder files and local state. |
| `--db-url` | default `$KOKOAH_SUBNET_DB_URL` | The record; SQLite in `--data-dir` when unset. A record serves one subnet: the validator refuses one made on another network or netuid. |
| `--wallet`, `--wallet-hotkey`, `--wallet-path` | your wallet | The validator's hotkey. |
| `--start-day` | `YYYY-MM-DD` | The first day this validator scores (section 10); default: the one its record remembers, else yesterday. |
| `--hl-info-url` | default Hyperliquid's public API | Point it at your own Hyperliquid node or endpoint to avoid public rate limits. |
| `--hl-budget` | default `1000` | Request weight a minute the validator uses on Hyperliquid (it allows 1,200 per IP address), counted as the exchange counts it, including the extra weight for the fills and candles a response returns. Lower it when other validators or services share the address, so that each stays inside the limit. |
| `--tao-usd-fallback` | a price | TAO/USD used only if the exchange lists no TAO perpetual. |
| `--poll-seconds` | default `60` | How often the loop wakes up. |
| `--watch` | | Score and record every day, never submit weights: to follow the subnet before or without a permit. |

## 6. Run

**One pass**, to check the configuration: score the oldest pending day, submit weights, exit.

```bash
uv run kokoah-subnet validator run --network finney --netuid 39 \
    --builder 0x43ccc0618e4358494f87a4b8699230203dc4ab67 \
    --agent-service https://api.kokoah.com/miners \
    --data-dir ~/kokoah-subnet-data --wallet validator --wallet-hotkey default \
    --start-day <YYYY-MM-DD> --once
```

**As a service.** On a server, the validator, its interface and its health check run as a dedicated user, either
in Docker with **automatic updates** (recommended: every validator then runs the newest release within minutes of it
being published) or with systemd, updated by hand. Both use the same files: settings in `/etc/kokoah-subnet`, data
and the hotkey in `/var/lib/kokoah-subnet`, and the record of section 4.

First, the user, the hotkey and the settings (both ways):

```bash
sudo useradd --system --home-dir /var/lib/kokoah-subnet --create-home --shell /usr/sbin/nologin kokoah-subnet

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
echo 'KOKOAH_SUBNET_ARGS=--network finney --netuid 39 --builder 0x43ccc0618e4358494f87a4b8699230203dc4ab67 --agent-service https://api.kokoah.com/miners --data-dir /var/lib/kokoah-subnet/data --wallet validator --wallet-hotkey default --wallet-path /var/lib/kokoah-subnet/wallets --start-day <YYYY-MM-DD>' \
    | sudo install -g kokoah-subnet -m 640 /dev/stdin /etc/kokoah-subnet/validator-main.env

# the daily backup of the record (section 9), the same both ways
sudo install -d -m 755 /opt/kokoah-subnet/scripts && sudo install -m 755 scripts/db.sh scripts/backup.sh /opt/kokoah-subnet/scripts/
sudo cp deploy/systemd/kokoah-subnet-backup.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kokoah-subnet-backup.timer
```

**With automatic updates (Docker).** The image `kokoahdotcom/kokoah-subnet` holds the validator, its interface, its
health check and an updater (`deploy/validator/`). The host's settings go in `/opt/kokoah-subnet/docker/.env`; then
`install.sh` checks the published image's signature and starts everything:

```bash
sudo install -d -m 755 /opt/kokoah-subnet/docker
printf 'KOKOAH_UID=%s\nKOKOAH_GID=%s\nKOKOAH_VALIDATOR_ENV=validator-main.env\n' \
    "$(id -u kokoah-subnet)" "$(id -g kokoah-subnet)" | sudo install -m 644 /dev/stdin /opt/kokoah-subnet/docker/.env
echo 'COMPOSE_PROFILES=interface' | sudo tee -a /opt/kokoah-subnet/docker/.env    # optional: the interface (section 8)
sudo deploy/validator/install.sh
```

The containers are `kokoah-subnet-validator`, `kokoah-subnet-check`, `kokoah-subnet-api` (with the interface) and
`kokoah-subnet-updater`; `docker logs -f kokoah-subnet-validator` follows the validator. Each runs as the
`kokoah-subnet` user (the updater as root, for Docker's socket) on a read-only system with no extra privileges and
a memory cap, and sees only the files it needs: the hotkey reaches the validator alone, read-only. Section 11 says
how the updates work.

**With systemd, updated by hand.** The unit files in `deploy/systemd/` (described in its README) run the release
installed in `/opt` with systemd's sandboxing; section 11 has the update steps.

```bash
# in the repository; Python goes under /opt too, so the service user can run it
sudo UV_PYTHON_INSTALL_DIR=/opt/kokoah-subnet/python UV_PROJECT_ENVIRONMENT=/opt/kokoah-subnet/venv \
    uv sync --frozen --no-dev --no-editable --reinstall-package kokoah-subnet --python 3.12 --python-preference only-managed
sudo chmod -R a+rX /opt/kokoah-subnet

sudo cp deploy/systemd/kokoah-subnet-validator@.service deploy/systemd/kokoah-subnet-api.service \
        deploy/systemd/kokoah-subnet-check.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kokoah-subnet-validator@main kokoah-subnet-api kokoah-subnet-check.timer
```

Logs go to `/var/log/kokoah-subnet/validator-main.log`. Run one of the two, never both: one validator writes the
record. Restarts are safe at any time: scoring resumes with the oldest unscored day, and a re-scored day starts from
the state as it was before that day.

## 7. What a validator does each day

All times are UTC.

1. **After midnight**, the previous day becomes pending.
2. **The exchange's daily builder file** (fills carrying Kokoah's builder code) is published within hours. The
   validator fetches it, records its hash and compares it with the hash the operator commits on chain. The exchange
   writes no file for a day without Kokoah fills: when the file is still missing 8 hours after the day and no miner's
   own history shows a builder fee that day, the day is scored then (a note in the record). Otherwise the validator
   waits up to 12 hours; after that it scores the day from the miners' own histories alone and records why (an
   error, shown by `validator check`).
3. **The data plane.** For every linked account: its own fill history (the primary source, merged with the file;
   a fill seen only in the history counts once its order is confirmed as a Kokoah order from the account's own
   signed transactions), its recorded account values (for the $10,000 daily average), flows and positions, and the
   accounts linked to it (to name counterparties and detect self-dealing). Every bound account's link and every
   agent machine are checked, and the unlinks the addresses signed are read from the subnet's service (each
   signature is checked; every unlink seen is kept).
4. **Scoring.** Eligibility, fee credit, skill, the entry test, agent verification, and the two
   rewards, exactly as the specification states. The day's artifact (every miner's figures) is stored.
5. **Weights.** Submitted every tempo (360 blocks, about 72 minutes) from the latest scored day, also while a long
   scoring pass runs: scoring runs beside the loop, on a connection of its own. The SDK follows the subnet's
   settings, including commit-reveal.

A failed read of a miner's recorded account values, a fill history, the explorer, a linked account, the minute
candles, the unlinks or the time of a link's join block fails that day's scoring, and the validator retries within the
hour, until 12 hours after the day ends, and never sooner than 3 hours after its first attempt at the day; the days of
a backlog share the first one's window, so a validator back from an outage retries for 3 hours in all (a day it had
already started gets a new window once on its return). After that the day is scored and only what needed the failed
read is lost: without the recorded account values the account is not eligible that day (unless points an earlier read
stored cover the whole day), and fills that needed a failed read go uncounted (without a coin's candles or the fill
history, also the day's return); without a join block's time, the join day is estimated at 12 seconds a block, which
is never earlier than the real one. The candles of a coin the exchange does not list, or of a day older than the
exchange keeps (about three and a half days), are settled at once the same way. Other reads (positions, fee rates,
stake) fall back. Every failure is recorded.

## 8. The public interface

`kokoah-subnet validator serve` (the `kokoah-subnet-api` container with `COMPOSE_PROFILES=interface`, or unit)
serves the record read-only on `127.0.0.1:48100` (`KOKOAH_API_PORT` in Docker's `.env` changes the port):

| Path | Content |
|---|---|
| `/` | The leaderboard page |
| `/summary` | Headline figures: the scored days' planned allocations (`planned_alpha`, `planned_usd`; also `paid_alpha`, `paid_usd`) and what the chain actually paid the miners (`earned_alpha`, `earned_usd`) |
| `/health` | Liveness and the latest scored day |
| `/days`, `/days/latest`, `/days/<YYYY-MM-DD>` | The daily artifacts: every miner's figures |
| `/bindings` | The accepted links between hotkeys and accounts |
| `/leaderboard` | Ranks and pay, with each account's record (90-day return, worst drop, losing days, verified agent days): the latest scored day's planned allocation (`planned_alpha`, also `pay_alpha`) and what the chain paid the hotkey that UTC day (`earned_alpha`), and both over all days (`total_planned_alpha`, also `total_pay_alpha`; `total_earned_alpha`) |
| `/accounts/<0x address>` | One account's whole record: every stored day with its return, the growth curve and the scored figures, each day's planned and earned alpha, and both in total |

Earned is what the chain actually paid, from the metagraph this validator records once per epoch of the chain (when
the chain's last epoch block changes): each epoch's emission of the hotkey, summed by the UTC day of that block (at
12 s a block) in `earned_days`. A scored day's planned allocation is paid over the following tempos, and only in
proportion to the stake of the validators that set those weights, so the two differ; epochs this validator did not
record (while it was down) are missing from earned. Epochs recorded before earned was kept per epoch count as
they were read: once per tempo window, by the UTC day of the read.

To publish it, put it behind a reverse proxy with TLS; `deploy/nginx/kokoah-subnet.conf` is a template with a
request limit. Publishing lets miners and other validators compare figures with yours.

## 9. Monitoring, alerts and backups

- **Health check.** `uv run kokoah-subnet validator check` prints the latest scored day, the last weights, the last
  epoch, errors in the last 24 hours (routine notes are counted apart and are no problem) and the builder-earnings
  check, and exits 1 on a problem. The
  `kokoah-subnet-check` container (or `kokoah-subnet-check.timer`) runs it hourly and posts to a webhook only when
  something is wrong: put
  `KOKOAH_SUBNET_ALERT_WEBHOOK=<Discord or Slack webhook URL>` in `/etc/kokoah-subnet/alert.env`. A run that finds a
  problem also leaves the unit failed, so `systemctl --failed` and any monitor of systemd units show it; a webhook
  that refuses the report is noted in `/var/log/kokoah-subnet/check.log` (in Docker, `docker logs kokoah-subnet-check`).
- **The earnings check.** The builder fees credited to miners must never exceed what Hyperliquid reports the builder
  has earned; the health check alarms if they do.
- **Dashboards.** `scripts/dashboard.sh start` runs Grafana on `127.0.0.1:48120` with the record as its data source
  (through the read-only role) and the dashboards in `dashboards/`.
- **Backups.** `kokoah-subnet-backup.timer` dumps the record daily (14 kept) to `/var/backups/kokoah-subnet/`;
  `scripts/db.sh backup <file>` does it by hand. On a host that also runs the agent service, put
  `KOKOAH_SUBNET_AGENT_DATA=<its data directory>` in `/etc/kokoah-subnet/backup.env`: its database and the stored
  agent code are then backed up too, and a wrong path fails the backup instead of skipping them.

## 10. Choosing the start day

Set `--start-day` to a day within the last three days: the exchange keeps minute prices for about three and a half
days, and on a day scored later this validator's record counts none of the perpetual builder fills and knows no
day's return (no skill credit that day). The same holds after an outage, so keep the validator running and let it
catch up within three days. From the start day on, the validator scores every day in order and builds its record
day by day; each account's earlier daily values are backfilled from the exchange when the account is first scored
(they show on the account's record; the skill test reads only the days after its link).

## 11. Upgrades

**Automatic (Docker).** Every change to main that reaches the validator is checked, built and signed by the
repository's release workflow (`.github/workflows/release.yml`) and published as `kokoahdotcom/kokoah-subnet:latest`,
also tagged with its commit and its mechanism version (`spec-<version>`). The updater looks at that tag every five
minutes (a request Docker Hub does not count as a pull). For a new image it:

1. checks the signature: made by exactly that workflow on `main`, as GitHub certifies through Sigstore (cosign), and
   for this image. A leaked Docker Hub login can publish an image, but no updater installs it. An image whose build
   number (the workflow run, inside the signature) is below the installed one's is refused too, so an older release
   cannot be put back;
2. downloads it by its digest, tags it `kokoah-subnet:current` (the one before stays as `kokoah-subnet:previous`) and
   installs the release's own `compose.yml`;
3. restarts the validator and waits until the record is at the release's schema version (the validator migrates
   it), then restarts the health check and the interface, then itself.

The updater's log says what it installed or refused: `docker logs kokoah-subnet-updater`. To hold updates, stop it
(`docker stop kokoah-subnet-updater`; it stays stopped until `docker start kokoah-subnet-updater`). To go back one
release while it is stopped: `docker tag kokoah-subnet:previous kokoah-subnet:current`, then
`docker compose --project-directory /opt/kokoah-subnet/docker up -d validator interface check` (or without
`interface` when the profile is off). A release that needs a step on the host as well (such as a new database role)
says so in its announcement.

**By hand (systemd).**

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

**Moving from systemd to Docker.** Stop and disable the systemd services
(`sudo systemctl disable --now kokoah-subnet-validator@main kokoah-subnet-api kokoah-subnet-check.timer`), then
follow the Docker part of section 6 from `/opt/kokoah-subnet/docker`: the settings, the data and the record stay as
they are, and `install.sh` refuses to start while the systemd validator runs.

- **Parameter changes** are versioned in the code, announced at least seven days before they take effect, and
  published in every daily artifact as the mechanism's version. Such a change reaches main on the day it takes
  effect, so every updater installs it that day; with systemd, nothing changes until you update.
- **New agent harness images** become valid only when their digest is added to the approved list in the code
  (`src/kokoah_subnet/agent/images.py`), so they arrive with an update. Each approved image is a reproducible build:
  `deploy/agent/build.sh <commit>` rebuilds it and prints the same digest.

## 12. Security

- The server holds **only the hotkey**; keep the coldkey offline.
- The services run as their own user, in Docker on a read-only system without privileges (`deploy/validator/`)
  or in systemd's sandbox (`deploy/systemd/`), with a memory cap.
- The updater holds Docker's socket, which is root's; it installs only images signed by the release workflow on main
  (section 11).
- The validator **never holds Hyperliquid keys** and never writes to the exchange.
- The record and the interface are local by default; publish the interface only through a reverse proxy.
- Keep the server patched. With systemd, keep the repository on a commit you have reviewed.

## 13. Troubleshooting

| Symptom | What to check |
|---|---|
| Weights are not accepted | The hotkey needs a validator permit (enough stake); the chain also limits how often weights can be set. |
| A day stays pending | The builder file is late: for 8 hours on any day, and up to 12 when a miner's history shows builder fees; then the day is scored from the histories. Check the log and `validator check`. |
| Errors about the exchange's API | Public rate limits or an outage; the day is retried within the hour. A dedicated endpoint (`--hl-info-url`) avoids rate limits. |
| "no TAO perpetual" | Pass `--tao-usd-fallback <price>`. |
| No verified agents | `--agent-service https://api.kokoah.com/miners` must be set and reachable. |
| Different figures from another validator | Compare the `/days/<day>` artifacts, and check that both run the same release. |
| The updater says `no signature of the release workflow` or `refused` | The published tag names an image the release workflow did not sign, or an older one; nothing was installed and the validator keeps running. Tell the subnet team. |
| The exchange's history itself was unavailable for a whole day | The operator announces it; close the day with `uv run kokoah-subnet validator skip-day --day YYYY-MM-DD --burn-hotkey <ss58>`, passing the hotkey the announcement names (the owner hotkey when no burn hotkey is published). Days are scored and closed in order only: the day must be the one after the latest scored day. |
