# Runbook

How to run each role of the subnet. Commands assume the repo is set up (`uv sync`) and a wallet exists in
`~/.bittensor/wallets`. Everything below works on the local chain (`scripts/localnet.sh start --slow`) and on
testnet (`--network test`); mainnet needs the owner's go-ahead per transaction.

## Validator

```bash
# one pass (score yesterday if its builder file is out, submit weights), then exit
uv run kokoah-subnet validator run --network test --netuid <N> --builder <builder address> \
    --data-dir ~/kokoah-subnet-data --wallet validator --once

# the daemon: same thing every minute; weights resubmitted every tempo; missing files retried hourly
uv run kokoah-subnet validator run --network test --netuid <N> --builder <builder address> \
    --data-dir ~/kokoah-subnet-data --wallet validator

# read-only HTTP view of the store (published daily artifacts, bindings, leaderboard), port 48100
uv run kokoah-subnet validator serve --data-dir ~/kokoah-subnet-data
```

What a pass does (whitepaper appendix B): fetch the previous UTC day's per-builder fill file (cached under
`<data-dir>/fills/`, hash stored), read every binding commitment and resolve which address counts for each hotkey,
fetch each bound address's public fills (for the total fee), account value and flows, positions, fee rates,
sub-accounts and transfer partners (for clusters), read the day's minimum own stake and mean pool price from the
chain, score the day with the mechanism, store the per-miner artifact under `<data-dir>/validator.sqlite`, and set
weights (miners by pay, the burn to the owner hotkey).

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
- The buyback is one transaction per run: `btcli tx stake-burn --netuid <N> --amount-tao <amount> -w owner`
  (buys alpha with TAO and burns it, emitting `TokensBurned`).
- Keep `owner_cut_auto_lock_enabled` on, so the owner's share is locked.
- Parameter changes: edit `src/kokoah_subnet/mechanism/params.py`, bump `SPEC_VERSION`, update the spec and the
  whitepaper, announce at least seven days ahead, release.

## Offline tools

```bash
uv run kokoah-subnet verify                      # the specification's numerical checks
uv run kokoah-subnet score-day ...               # score one day from files (README)
uv run python scripts/replay.py --days 60        # adversarial replay through the real scoring code
uv run python scripts/measure_burn.py            # burn and emission counters per tempo on a local chain
```
