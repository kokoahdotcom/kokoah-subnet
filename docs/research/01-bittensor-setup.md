# Bittensor in October 2026: where things stand and what we set up

Date: 2026-10-19. Everything below was checked on that day against the live chains, PyPI, GitHub and the container
registry; numbers move daily.

## 1. The code moved, and the old stack is gone

- The Opentensor Foundation's repos (`opentensor/bittensor`, `btcli`, `btwallet`, `bittensor-subnet-template`,
  `async-substrate-interface`, `bittensor-drand`) redirect to `RaoFoundation/*` and were **archived in July 2026**.
  The one active repo is the monorepo **https://github.com/RaoFoundation/subtensor** (chain, runtime, Python SDK in
  `sdk/python`, docs in `docs/`, Docker images; last push 2026-10-19).
- The archived subnet template's notice: it "targets the legacy Bittensor SDK (v10) and its Axon/Dendrite/Synapse
  networking stack, which was removed in Bittensor 11. It will not work with current releases." Builders are pointed to
  the migration guide and the signed-requests guide.
- `latent-to/*` (Latent Holdings) hosts the other doc site, `docs.learnbittensor.org` (`latent-to/developer-docs`,
  active, last push 2026-10-16), a `subnet-template` (May 2026, still v10 code: imports `Axon`, `Dendrite`,
  `Synapse`), the `targon` reward-stack library and a few subnets (`tenex-subnet`, `taohash`).
- Two doc sites, then: **bittensor.com/docs** (generated from the chain's metadata in the monorepo; authoritative for
  SDK 11 and `btcli`) and **docs.learnbittensor.org** (Latent; concepts, tutorials, parts still v10-era).

## 2. Packages and versions

| Package | Version | Date | Python | Note |
|---|---|---|---|---|
| `bittensor` | 11.3.0 | 2026-10-07 | >=3.10,<3.15 | SDK **and** `btcli`; supersedes `bittensor-cli` and `bittensor-wallet` |
| `bittensor-cli` | 9.23.2 | 2026-07-12 | >=3.10 | legacy v9 CLI; installing it hijacks the `btcli` command (the SDK warns and tells you to uninstall it) |
| `bittensor-wallet` | 4.1.1 | 2026-07-12 | >=3.10 | legacy; the wallet now ships in `bittensor` |
| `turbobt` | 1.3.2 | 2026-09-18 | >=3.11 | third-party "next generation" SDK (bactensor); not used |
| `async-substrate-interface` | 2.2.1 | 2026-06-29 | | archived upstream |

We pin `bittensor==11.3.0` in `pyproject.toml` (uv, Python 3.12). The SDK's `btcli` has 102 `query` reads and 83 `tx`
intents, all generated from chain metadata; `btcli tools` and `bt.intents.list_tools()` expose the catalog as JSON.

## 3. Networks and live numbers (2026-10-19)

| Network | `--network` | Endpoint | Subnet registration cost | Notes |
|---|---|---|---|---|
| mainnet | `finney` | public pool | **τ482.500** | doubles on each registration, decays linearly over 100,800 blocks (~2 weeks); the docs' default floor is 1,000 TAO but the live value is below it, so the floor is set lower on chain; one subnet registration per 7,200 blocks (~1 day) network-wide; cap 128 subnets, price-ranked pruning of the lowest EMA alpha price once full, 6-month immunity for new subnets |
| testnet | `test` | public pool (`wss://test.finney.opentensor.ai:443` in older docs) | **τ1.000** | 596 subnets; the faucet is off, test TAO is requested in the Bittensor Discord |
| local | `local` | `ws://127.0.0.1:9944` | from Alice's 1,000,000 TAO | Docker image, 0.25 s blocks by default (48× faster than mainnet for anything measured in blocks), subnets 0 and 1 at genesis, a PoW faucet of 1,000 TAO per call |

TAO traded around $270 on 2026-10-19 by press reports, so the mainnet registration is in the order of $130k. The
lock is **not refundable**: it becomes the new subnet's TAO reserve. Registration is also chain-rate-limited and the
subnet then needs the owner's `start-call` to go live, and root's `subnet_emission_enabled` switch (off at
registration, not owner-settable) before any TAO emission reaches it.

News context (secondary sources, not verified on chain): the registration cost rose from about 460 TAO in September
2026 to 884 TAO paid by the newest subnet on 2026-10-09 and 1,333 TAO quoted on 2026-10-13, then decayed to the τ482.5
read above. Runtime 475 (2026-10-07) added a "Null consensus" option (up to 2,500 UIDs, the largest-stake validator
alone sets weights) next to Yuma consensus, and fee-free proof-of-work neuron registration that owners can enable.
Emissions to subnets have followed the EMA of the alpha price since June 2026 (after the "Taoflow" period), with a
threshold gate (runtime 440) that collapses emission for subnets ranked below roughly the 32nd by price; 57 of 128
subnets had emissions halted on 2026-06-22. The next halving is projected for December 2026.

## 4. How a subnet is built now (summary; details in `02-bittensor-11-platform.md`)

- **Chain + SDK:** the chain holds registrations, stake, weights, hyperparameters and each neuron's served endpoint
  (`serve-axon`). The SDK (`bt.Subtensor` / `bt.Client`) reads state (typed namespaces such as
  `client.subnets.metagraph(netuid)`) and submits **intents** (`bt.RegisterSubnet`, `bt.BurnedRegister`,
  `bt.SetWeights`, `bt.CommitWeights`, …) through `client.plan` / `client.execute`, optionally bounded by a `bt.Policy`.
- **Transport is ours:** validators query miners over plain HTTP. Identity comes from `bt.http_auth.sign` /
  `bt.http_auth.verify` (the normative `btauth/1` format: eight signed lines covering scheme, method, path, body hash,
  nonce, sender and receiver; replay store; 10 s freshness). The docs give FastAPI and httpx recipes, and a drand
  timelock for copy-proof responses. Our offline tests pin the SDK's golden vectors.
- **Validators** set weights each tempo (`set-weights`, or commit-reveal when `commit_reveal_weights_enabled`, which
  is the default); they need stake above the validator threshold, and a new subnet typically borrows stake from a
  root validator via childkeys.
- **Miners** register by burn (`burned-register`, live price between `min_burn` and `max_burn`) or by owner-enabled
  PoW (`pow-register`); 256 UIDs under Yuma, up to 2,500 under Null consensus; immunity 4,096 blocks by default.
- **Economics:** each tempo's alpha emission splits 18% owner / ~41% miners / ~41% validators by default (owners
  can move it within limits since "Etao"); the subnet's TAO income follows demand for its alpha token.
- **Owner hygiene from the guide:** multisig owner coldkey plus an Owner-type proxy for operations; run a validator on
  the owner hotkey, never mine on it; decide the registration policy before activation.

## 5. What we set up here

- `/kokoah/kokoah-subnet`: uv project (Python 3.12.13, uv 0.11.19), `.venv` with `bittensor` 11.3.0 (`btcli` 11.3.0
  verified: `btcli -n finney --json query subnet-registration-cost` → `"τ482.500080842"`).
- `scripts/localnet.sh`: the Docker localnet (`ghcr.io/raofoundation/subtensor-localnet:devnet`, pulled 2026-10-19;
  the tags are `devnet`, `testnet`, `main`/`latest`, `sha-<commit>`; the older `opentensor` image names no longer
  exist). Container `kokoah-subnet-localnet`, ports 9944/9945 bound to 127.0.0.1.
- Offline tests: the `btauth/1` golden vectors (payload bytes, the pinned ed25519 signature, sign→verify round trip,
  replay and wrong-receiver refusals), unit-tagged money, CLI parsing.
- Machine tooling available: Docker 29.3.0, Rust 1.89 (for a source build of the node if ever needed), Node 24.

## 6. Open questions after this pass

- The live floor of the registration cost (below the documented 1,000 TAO default) and its path over the coming
  weeks: read it again before any mainnet decision.
- How root decides to enable `subnet_emission_enabled` for a new subnet (process, criteria, typical delay).
- Whether to use Yuma 3 (`yuma3_enabled`) or Null consensus for the first version, and whether two mechanisms are
  useful (the chain caps subnets at two).
- Validator bootstrapping: which root validator would parent our validator hotkey on testnet and mainnet.

## Sources

- https://github.com/RaoFoundation/subtensor (README; `docs/guides/subnets.mdx`, `docs/guides/signed-requests.mdx`,
  `docs/guides/local-development.mdx`; `sdk/python/README.md`)
- https://github.com/RaoFoundation/bittensor-subnet-template (archived notice)
- https://github.com/latent-to/developer-docs, https://github.com/latent-to/subnet-template
- https://pypi.org/project/bittensor/, https://pypi.org/project/bittensor-cli/, https://pypi.org/project/turbobt/
- Live reads with `btcli` 11.3.0 on 2026-10-19 (`subnet-registration-cost` on `finney` and `test`; `subnets list` on `test`)
- Press and dashboards on the registration cost, runtime 475 and emissions (secondary): https://cryptoticker.io/en/bittensor-null-consensus-subnet-slots/,
  https://chainbuying.netlify.app/, https://www.bittensor.com/releases/v440-upgrade, https://docs.learnbittensor.org/learn/emissions,
  https://ourcryptotalk.com/news/bittensor-subnet-emissions-halted-57-subnets
