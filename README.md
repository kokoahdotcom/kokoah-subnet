# kokoah-subnet

A Bittensor subnet built by the Kokoah team, kept separate from the Kokoah trading app. Miners are traders who
trade Hyperliquid perpetuals through the Kokoah builder code; validators score them from public data. This repo
holds the whitepaper, the mechanism specification, the research behind it, and the validator and scoring code.

- `docs/whitepaper.md`: the public description of the subnet and its incentive mechanism.
- `docs/runbook.md`: how to run a validator, how a miner binds an address, what the operator does.
- `docs/database.md`: the record every validator keeps (PostgreSQL via `scripts/db.sh`, SQLite for tests).
- `docs/app-binding.md`: what the application does so a trader becomes a miner; `docs/mainnet-checklist.md`: the
  owner's go-live steps.
- The validator's read-only interface (`kokoah-subnet validator serve`, port 48100) serves the daily artifacts,
  the bindings, a summary and the leaderboard page at `/`.
- `docs/design/02-mechanism-spec.md`: the complete formal specification (every formula and parameter);
  `01-incentive-mechanism.md` says the same in plain words.
- `scripts/verify_mechanism.py`: numerical verification of the specification's claims (`uv run python
  scripts/verify_mechanism.py`); `scripts/sim_skill.py` is the simulation behind
  the calibration.

- The maintainer's status notes and working rules are kept locally, outside this repository.
- Stack: Python 3.12, [uv](https://docs.astral.sh/uv/), Bittensor SDK 11 (`bittensor==11.3.0`, which ships `btcli`),
  FastAPI and httpx for the hotkey-signed HTTP between validators and miners (`btauth/1`).

## Setup

```bash
uv sync                                 # creates .venv from uv.lock (Python 3.12)
uv run btcli --version                  # 11.3.0
scripts/localnet.sh start               # local Bittensor chain in Docker, ws://127.0.0.1:9944, fast blocks
uv run kokoah-subnet info --network local
scripts/localnet.sh stop
```

`scripts/check.sh` runs the same steps as CI: ruff (lint and format), mypy and the offline tests.

## Scoring a day offline

```bash
uv run kokoah-subnet verify                                  # the specification's numerical checks
uv run kokoah-subnet score-day --fills 20261020.csv.lz4 --miners miners.json --state state.json \
    --price-usd 5.33 --day-index 1 --owner-hotkey <owner hotkey ss58> --out day.json
```

`--fills` is Hyperliquid's per-builder daily file (or a plain CSV with the same header); `miners.json` is a list of
`{hotkey, address, returns, mean_equity_usd, avg_equity_usd, staked_min_alpha, collateral_min_alpha}` (a miner
without `avg_equity_usd`, the day's average account value, is not eligible that day); the state file carries the
smoothed values from day to day. The result lists every miner's credit, factors, multiplier, share, cap and pay, the
burn, and the weight fractions (miners by pay, the burn to the owner hotkey). The same code path runs in the
validator against live data.

Networks: `local` (Docker, throwaway, pre-funded Alice account), `test` (Bittensor testnet, test TAO without value),
`finney` (mainnet, real TAO; nothing is sent there without the owner's go-ahead).

## Layout

```
docs/                 whitepaper.md, design/ (specification, plain-words summary), research/ (dated notes)
sim/                  index.html, an interactive model of pay, caps, burn, revenue and buyback
scripts/              localnet.sh (Docker chain), check.sh (CI steps), verify_mechanism.py, sim_*.py, git-hooks/
src/kokoah_subnet/    cli.py, chain.py (read-only chain facts), mechanism/ (the scoring, pure functions),
                      validator/ (Hyperliquid client, builder file, chain reads and weights, day runner)
tests/                offline tests (no network)
```

Wallets never enter the repo. They live in `~/.bittensor/wallets`; the public `//Alice` dev key is only for the
localnet.
