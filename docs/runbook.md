# Runbook

How to run each role of the subnet. Commands assume the repo is set up (`uv sync`) and a wallet exists in
`~/.bittensor/wallets`. Everything below works on the local chain (`scripts/localnet.sh start --slow`) and on
testnet (`--network test`); mainnet needs the owner's go-ahead per transaction.

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

```bash
# one pass (score yesterday if its builder file is out, submit weights), then exit
uv run kokoah-subnet validator run --network test --netuid <N> --builder <builder address> \
    --burn-hotkey <burn hotkey ss58> --data-dir ~/kokoah-subnet-data --wallet validator --once

# the daemon: same thing every minute; weights resubmitted every tempo; missing files retried hourly
uv run kokoah-subnet validator run --network test --netuid <N> --builder <builder address> \
    --burn-hotkey <burn hotkey ss58> --data-dir ~/kokoah-subnet-data --wallet validator

# read-only HTTP view of the store (published daily artifacts, bindings, leaderboard), port 48100
uv run kokoah-subnet validator serve --data-dir ~/kokoah-subnet-data
```

What a pass does (whitepaper appendix B): fetch the previous UTC day's per-builder fill file (cached under
`<data-dir>/fills/`, hash stored), read every binding commitment and resolve which address counts for each hotkey,
fetch each bound address's public fills (for the total fee), account value and flows, positions, fee rates,
sub-accounts and transfer partners (for clusters), read the day's minimum own stake and mean pool price from the
chain, score the day with the mechanism, store the per-miner artifact under `<data-dir>/validator.sqlite`, and set
weights (miners by pay, the remainder to the burn hotkey).

TAO/USD comes from the mark price of the `TAO` perp on Hyperliquid. If it is not listed, pass
`--tao-usd-fallback <price>` or the validator refuses to score.

The validator never holds Hyperliquid keys and never trades.

## Miner

A miner registers a hotkey (`btcli tx burned-register --netuid <N> -w <wallet>`), then binds the Hyperliquid
address it trades from:

```bash
# 1. the exact text to sign with the Hyperliquid address's wallet (EIP-191 personal message)
uv run kokoah-subnet miner message --netuid <N> --hotkey <hotkey ss58> --address <0x...>

# 2. publish the binding as the hotkey's on-chain commitment (checks the signature first)
uv run kokoah-subnet miner bind --network test --netuid <N> --address <0x...> --signature <0x...> \
    --wallet <wallet> --wallet-hotkey default

# migration to a new address (once per 60 days): both addresses sign the message for the new address
uv run kokoah-subnet miner bind ... --address <new> --signature <new's sig> \
    --previous-address <old> --previous-signature <old's sig>

# what is published and whether it verifies
uv run kokoah-subnet miner show --network test --netuid <N>
```

Then trade perpetuals through the Kokoah app. Nothing else runs on the miner's side.

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
  127.0.0.1 --port 48130`, behind a reverse proxy with TLS at the public URL miners, machines and validators use. It
  accepts submissions (hotkey-signed) and key copies only from hotkeys registered on subnet N; restores and
  releases do not depend on the registration. Back its data up like money: the
  encrypted keys are how agents resume after a machine stops, and losing them strands miners' funds. Lock it down:
  a leaked copy plus a miner's own secret would let that miner trade by hand unnoticed. Validators read its
  released list every day (`validator run --agent-service <URL>`); without it no account counts as an agent.
- A harness release: commit, `deploy/agent/build.sh <commit>` (a reproducible build from a clean export of the
  commit; it prints the digest), `docker push docker.io/kokoahdotcom/kokoah-subnet-agent:<tag>`, run it once on a
  real sealed machine, add the digest with its commit to `src/kokoah_subnet/agent/images.py`, release the
  validator; validators accept the new image once they update. Anyone can rebuild the commit and compare digests.
- Parameter changes: edit `src/kokoah_subnet/mechanism/params.py`, bump `SPEC_VERSION`, update the spec and the
  whitepaper, announce at least seven days ahead, release.

## Testnet

`scripts/testnet.sh wallets`, then (after test TAO from the Bittensor Discord) `scripts/testnet.sh register`,
`KOKOAH_SUBNET_NETUID=<n> scripts/testnet.sh start`, and `KOKOAH_SUBNET_NETUID=<n> KOKOAH_BUILDER_ADDRESS=<0x…>
scripts/testnet.sh run`.

## Offline tools

```bash
uv run kokoah-subnet verify                      # the specification's numerical checks
uv run kokoah-subnet score-day ...               # score one day from files (README)
uv run python scripts/replay.py --days 60        # adversarial replay through the real scoring code
uv run python scripts/fetch_real_fills.py --out DIR   # public fills of real addresses, then replay.py --real DIR
uv run python scripts/measure_burn.py            # burn and emission counters per tempo on a local chain
```
