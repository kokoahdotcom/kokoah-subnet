# kokoah-subnet

A Bittensor subnet built by the Kokoah team, kept separate from the Kokoah trading app. Miners are traders who
trade Hyperliquid perpetuals through the Kokoah builder code; validators score them from public data. This repo
holds the whitepaper, the mechanism specification, the research behind it, and the validator and scoring code.

- `docs/whitepaper.md`: the public description of the subnet and its incentive mechanism.
- `docs/design/02-mechanism-spec.md`: the complete formal specification (every formula and parameter);
  `01-incentive-mechanism.md` says the same in plain words.
- `scripts/verify_mechanism.py`: numerical verification of the specification's claims (`uv run python
  scripts/verify_mechanism.py`); `scripts/sim_skill.py` and `scripts/sim_incentives.py` are the simulations behind
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

Networks: `local` (Docker, throwaway, pre-funded Alice account), `test` (Bittensor testnet, test TAO without value),
`finney` (mainnet, real TAO; nothing is sent there without the owner's go-ahead).

## Layout

```
docs/                 whitepaper.md, design/ (specification, plain-words summary), research/ (dated notes)
sim/                  index.html, an interactive model of pay, caps, burn, revenue and buyback
scripts/              localnet.sh (Docker chain), check.sh (CI steps), verify_mechanism.py, sim_*.py, git-hooks/
src/kokoah_subnet/    the package: cli.py, chain.py (read-only chain facts)
tests/                offline tests (no network)
```

Wallets never enter the repo. They live in `~/.bittensor/wallets`; the public `//Alice` dev key is only for the
localnet.
