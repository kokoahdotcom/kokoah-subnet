# kokoah-subnet

A Bittensor subnet built by the Kokoah team, kept separate from the Kokoah trading app. The subnet's incentive
mechanism is being designed (see `docs/research/`); this repo holds the research, the design, and the validator,
miner and scoring code as they appear.

- Status, decisions and open questions: `subnet-agent.md` (the single status file of this project).
- Working rules: `.claude/CLAUDE.md`.
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

Networks: `local` (Docker, throwaway, pre-funded Alice account), `test` (Bittensor testnet, test TAO without value),
`finney` (mainnet, real TAO; nothing is sent there without the owner's go-ahead).

## Layout

```
.claude/CLAUDE.md     working rules
subnet-agent.md       development status
docs/research/        dated research notes (platform, landscape, what Kokoah does today)
scripts/              localnet.sh (Docker chain), check.sh (CI steps)
src/kokoah_subnet/    the package: cli.py, chain.py (read-only chain facts)
tests/                offline tests (no network)
```

Wallets never enter the repo. They live in `~/.bittensor/wallets`; the public `//Alice` dev key is only for the
localnet.
