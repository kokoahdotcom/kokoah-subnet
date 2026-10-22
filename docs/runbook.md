# Runbook

The operations reference: the record, services, dashboards and the operator's duties. To run a validator or to
mine, start with the [validator guide](validators.md) and the [miner guide](miners.md). Commands assume the repo is
set up (`uv sync`) and a wallet exists in `~/.bittensor/wallets`. Everything below works on the local chain
(`scripts/localnet.sh start --slow`) and on testnet (`--network test`).

## The record (database)

`scripts/db.sh start` runs the project's PostgreSQL (Docker, loopback port 48110) and writes the connection URL to
`~/.local/share/kokoah/subnet-db.env`. Export it (`set -a; . ~/.local/share/kokoah/subnet-db.env; set +a`) before
running the validator, the API or the burn command, or pass `--db-url`. Without it they use a SQLite file in the
data directory. What is recorded is in `docs/database.md`.

## Services (systemd)

On a server with systemd, run everything as units that restart on their own (the unit files are in
`deploy/systemd/`; copy them to `/etc/systemd/system/`, put the arguments in
`~/.local/share/kokoah/subnet-validator-<name>.env` as `KOKOAH_SUBNET_ARGS=...`, then
`systemctl enable --now kokoah-subnet-validator@<name> kokoah-subnet-api kokoah-subnet-burn kokoah-subnet-check.timer
kokoah-subnet-operator.timer`). The hourly check posts to `KOKOAH_SUBNET_ALERT_WEBHOOK` (a Discord or Slack webhook,
in `~/.local/share/kokoah/subnet-alert.env`) only when something is wrong; the daily operator step commits
yesterday's file hash from the owner hotkey at 00:20 UTC and retries hourly until noon if the file is late.

The exchange's daily builder file has covered only about the first half of each UTC day, and it writes none for a
day whose covered part had no fills. The validator therefore scores each bound address from its own fill history
(merged with the file), waits for the file until 12:00 UTC the next day, and then scores from the histories alone
(an error is recorded, so the check shows it). The check also alarms when the builder fees credited to traders
exceed what the exchange says the builder has earned. Only if the exchange's fill history itself is unavailable
for a day, close it by announcement on every validator:
`uv run kokoah-subnet validator skip-day --day YYYY-MM-DD --burn-hotkey <ss58>` (scored as a day with no fills).

`uv run kokoah-subnet validator check` prints the health report (latest day, last weights, last epoch, errors in
24 h, last burn) and exits 1 on a problem.

Backups: `kokoah-subnet-backup.timer` dumps the record daily at 03:00 UTC to `~/.local/share/kokoah/backups/`
(14 kept); `scripts/db.sh backup <file>` does it by hand. Public exposure of the interface, if wanted, goes behind a
reverse proxy; `deploy/nginx/kokoah-subnet.conf` is a template.

## Dashboards

`scripts/dashboard.sh start` runs Grafana (Docker, 127.0.0.1:48120, admin password generated into
`~/.local/share/kokoah/subnet-grafana.env`) provisioned with the record as its datasource and the dashboards in
`dashboards/` (overview: volume, fees, paid and burned, miners, conversion-rate inputs, emission per UID per epoch,
latest day per miner, the miners table, fills by exclusion reason, submissions and burns). Add dashboards as JSON
files in `dashboards/` and restart.

## Validator

See the [validator guide](validators.md): requirements, configuration, running as a service, the daily cycle, the
interface, monitoring, and how a validator that joins late converges with the others.

## Miner

See the [miner guide](miners.md): registration, linking a Hyperliquid account, what counts, eligibility, pay,
status, and changing address or hotkey. Agents: the [agent guide](agents.md).

## Operator

- Builder fees are collected by the exchange; the per-builder daily file is public and is what validators score.
- Each day, commit the file's hash from the owner hotkey so validators can check theirs against it (the day's
  artifact shows `builder_file_hash_matches_operator`):
  `uv run kokoah-subnet owner commit-file --network test --netuid <N> --day 2026-10-21 --file 20261021.csv.lz4 -w owner`
- The buyback is one transaction per run: `btcli tx stake-burn --netuid <N> --amount-tao <amount> -w owner`
  (buys alpha with TAO and burns it, emitting `TokensBurned`).
- Keep `owner_cut_auto_lock_enabled` on, so the owner's share is locked.
- The burn hotkey (v6: only a day on which no miner has any weight goes to it): a wallet of its own
  (`btcli wallet create -w burn -H default`), its hotkey registered on the subnet
  (`btcli tx burned-register --netuid <N> -w burn`), passed to every validator as `--burn-hotkey <ss58>`.
  Burn what it received, as often as you like (one transaction per run):
  `uv run kokoah-subnet owner burn --network test --netuid <N> --wallet burn [--dry-run]`
  (`--password-stdin` reads the coldkey password from stdin; `scripts/burn_loop.sh` runs it every few hours).
- The agent service (miners' submissions, and each agent account's key encrypted with its miner's secret):
  `uv run kokoah-subnet agent service --netuid <N> --network finney --data-dir ~/kokoah-subnet-agents --host
  127.0.0.1 --port 48130 --wrap-key-file <key file>` (systemd: `kokoah-subnet-agent-service`), published at
  `https://api.kokoah.com/miners` (`deploy/nginx/kokoah-subnet-agents.conf`). It accepts submissions
  (hotkey-signed) and key copies only from hotkeys registered on subnet N; restores and releases do not depend on
  the registration. The key file (`agent service-key --out <file>`, once) adds the service's own encryption over
  every stored copy: keep it out of the backups and keep one copy offline, because without it no copy can be
  restored or released. Back its data up like money: the
  encrypted keys are how agents resume after a machine stops, and losing them strands miners' funds. Lock it down:
  a leaked copy plus a miner's own secret would let that miner trade by hand unnoticed. Validators read its
  released list every day (`validator run --agent-service <URL>`); without it no account counts as an agent.
- A harness release: commit, `deploy/agent/build.sh <commit>` (a reproducible build from a clean export of the
  commit; it prints the digest), `docker push docker.io/kokoahdotcom/kokoah-subnet-agent:<tag>`, run it once on a
  real sealed machine, add the digest with its commit to `src/kokoah_subnet/agent/images.py`, release the
  validator and restart the agent service (both read the approved list at start); validators accept the new image
  once they update. Anyone can rebuild the commit and compare digests.
- Parameter changes: edit `src/kokoah_subnet/mechanism/params.py`, bump `SPEC_VERSION`, update the spec and the
  whitepaper, announce at least seven days ahead, release.

## Offline tools

```bash
uv run kokoah-subnet verify                      # the specification's numerical checks
uv run kokoah-subnet score-day ...               # score one day from files (`--help` lists the inputs)
uv run python scripts/replay.py --days 60        # adversarial replay through the real scoring code
uv run python scripts/fetch_real_fills.py --out DIR   # public fills of real addresses, then replay.py --real DIR
```
