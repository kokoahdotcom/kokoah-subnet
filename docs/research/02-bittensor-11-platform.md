# Bittensor 11 platform notes (as of 2026-10-19)

Research note for kokoah-subnet. Everything below comes from the RaoFoundation
`subtensor` monorepo docs and the Latent developer docs read on 2026-10-19
(exact URLs in Sources). Where the two disagree, the RaoFoundation version is
preferred: those pages are generated from the chain and the SDK registries.
Anything not backed by a source is marked **unconfirmed**. Versions, live
numbers and what is installed here are in `01-bittensor-setup.md`.

Obsolete and deliberately not described: `Axon` / `Dendrite` / `Synapse`,
`bittensor-cli` v9, Yuma 2 as "the" consensus, the old "100 TAO lock cost",
`bt.logging`, `bt.config` / argparse plumbing, `weight_utils`, `MockSubtensor`.

---

## 1. Where the code and docs live now

| Thing | Location (2026-10-19) |
| --- | --- |
| Chain, SDK, `btcli`, docs | One monorepo: https://github.com/RaoFoundation/subtensor (docs under `docs/`) |
| Archived since July 2026 | `opentensor` / `latent-to` SDK, `btcli`, wallet and subnet-template repos |
| Python package | `bittensor` 11.3.0 on PyPI: library **and** the `btcli` command in one install. Python 3.10–3.14 (we use 3.12); Linux and macOS wheels, Windows only via WSL |
| Must not be installed | `bittensor-cli` (v9) and `bittensor_wallet`. They conflict; v11's `btcli` warns on every run while a stale `bittensor-cli` exists. Fix: `pip uninstall -y bittensor-cli bittensor-wallet` then `pip install --force-reinstall --no-deps bittensor` |
| RaoFoundation docs | https://www.bittensor.com/docs. Machine entry points: `/llms.txt`, `/llms-full.txt`, `/llms.mdx/docs/<page>/content.md`, `/catalog/intents.json`, `/catalog/reads.json`, `/catalog/errors.json`, chain source under `/code` and `/code/raw/<path>` |
| Latent docs | https://docs.learnbittensor.org (`latent-to/developer-docs`). Older; several pages still use v9 CLI syntax (`btcli subnet create`, `btcli sudo set --param`) and the old `ghcr.io/opentensor/...` image name |
| Images | localnet `ghcr.io/raofoundation/subtensor-localnet` (`devnet`, `testnet`, `main`, `latest`, `sha-<commit>`, `v<spec_version>`); node `ghcr.io/raofoundation/subtensor` (`devnet`, `testnet`, `mainnet`, `v475`, `v477`, `v478`) |

Discover the API at runtime instead of memorising it: `btcli tools` (every tx
op with JSON schema), `btcli query --help` (70+ reads), `btcli explain <Name>`;
in Python `bt.intents.list_tools()` and `bt.reads.list_reads()`.

Chain governance: block production is Proof of Authority (Aura + GRANDPA), NPoS
planned "within roughly the next year". The senate was removed; privileged
calls go through sudo held by a Rao Foundation multisig, so root-only
hyperparameters need the foundation.

---

## 2. Networks and costs

| Name | Endpoint |
| --- | --- |
| `finney` (mainnet) | `wss://entrypoint-finney.opentensor.ai:443` (lite `wss://lite.chain.opentensor.ai:443`, archive `wss://archive.chain.opentensor.ai:443`) |
| `test` | `wss://test.finney.opentensor.ai:443` |
| `devnet` | `wss://dev.chain.opentensor.ai:443` |
| `local` | `ws://127.0.0.1:9944` (override with `BT_CHAIN_ENDPOINT`) |

Any `ws://` / `wss://` URL works as `--network` or the `Subtensor(...)`
argument. Public endpoints are limited to ~1 request/s per IP; a lite node
keeps ~300 blocks of state (historical reads need archive).

### Subnet registration cost

- Read it first: `btcli query subnet-registration-cost --json` /
  `sub.subnets.subnet_registration_cost()`. Live 2026-10-19: **finney
  τ482.5**, **test τ1.0**.
- The cost **doubles** after every subnet registration and decays linearly
  over 100,800 blocks (~2 weeks), floored at `NetworkMinLockCost`. The subnets
  guide says the floor is "1,000 TAO by default"; the live τ482.5 is below
  that, so the mainnet floor is set lower (**unconfirmed** why).
- The whole payment becomes the new subnet's TAO reserve (alpha reserve sized
  so the start price equals the median alpha price). **Not refundable.**
- Rate limit: the subnets guide says one registration per 7,200 blocks
  (~1 day) network-wide; the `register-subnet` page says "per coldkey"; Latent
  says 14,400 blocks. **Unconfirmed**; expect roughly one per day.
- A `bt.Policy` spend cap blocks `register-subnet` (price unknown until
  execution). At the subnet limit the call is **queued**
  (`NetworkRegistrationQueued`) while the lowest-EMA-price non-immune subnet is
  dissolved; SDK and `btcli` wait for `NetworkAdded` and return the real
  `netuid` (`result.registration_mode` tells which path ran).

### Subnet lifecycle

1. `btcli tx register-subnet -w owner` (`bt.RegisterSubnet()`), coldkey-signed;
   the wallet hotkey becomes the subnet-owner hotkey.
2. Wait for `StartCallDelay` (value not in the sources; `btcli query
   subnet-start-schedule --netuid N` shows the earliest block). Before it:
   no staking (`SubtokenDisabled`), no emission, `set-children` applies
   immediately.
3. `btcli tx start-call --netuid N -w owner` (`bt.StartCall(netuid=N)`, also
   `btcli sudo start`): owner-only, once. Sets `subnet_is_active` (storage
   `SubtokenEnabled`): staking, alpha trading and epochs start.
4. **Root** must flip `subnet_emission_enabled`
   (`btcli tx set-subnet-emission-enabled --netuids N --enabled -w sudo_key`).
   New subnets register with it **off**; owners cannot set it. Until then no
   TAO reaches the pool, although `alpha_out` still pays participants.
5. Verify: `btcli sudo get --netuid N --name subnet_is_active`,
   `btcli query subnet-emission-enabled --netuid N`.

`get_subnets_to_emit_to` also requires a recorded first-emission block and
`registration_allowed == true`; turning burn registration off has an emission
side effect (gotcha 5).

### Subnet deregistration

- Mainnet limit **128** subnets (testnet has 596, so its limit differs —
  **unconfirmed**). No inactivity-based deregistration.
- Trigger: `register-subnet` while full. Victim: the non-immune subnet with
  the lowest EMA alpha price (ties: oldest). All immune → registration fails.
- Immunity: Rao **1,296,000 blocks (~6 months)**; Latent 864,000 (4 months).
  Prefer Rao.
- Dissolution deletes every UID; the TAO reserve is paid pro-rata to alpha
  holders as free coldkey balance (protocol-held alpha dilutes the payout);
  conviction locks are deleted uncompensated. Rao: post-dTAO subnets get **no**
  lock refund. Latent describes a tiered refund (`max(0, lock_cost −
  owner_emission_in_tao)` for subnets registered before 2025-10-01). Prefer
  Rao: assume no refund.

### Neuron slots, immunity, trimming

- `max_allowed_uids` default 256, floor 64. At capacity a registration evicts
  the lowest-**emission** non-immune neuron (ties: older registration, lower
  UID); miners and validators are not distinguished.
- `immunity_period` default 4,096 blocks (~13.7 h). If pruning would leave
  ≤ `MinNonImmuneUids` (10) non-immune UIDs, the lowest-emission immune neuron
  is pruned instead. The owner hotkey is permanently immune
  (`owner_immune_neuron_limit`, default 1, max 10).
- `btcli sudo trim --netuid N --max M` (`trim-subnet`): once per 216,000
  blocks (~30 days), never below 64, immune UIDs ≤ 80% of the new max,
  survivors renumbered from 0, trimmed state deleted permanently.

---

## 3. Architecture of a Bittensor 11 subnet

### Division of labour

| Layer | Responsibility |
| --- | --- |
| Chain | Registry of subnets / UIDs / hotkeys / axon `ip:port`; stake and AMM pools; hyperparameters; weight storage (plain or timelock-encrypted, decrypted by the chain at the drand round); Yuma or Null consensus each epoch; emissions; pruning; collateral locks |
| SDK (`bittensor` 11) | `bt.Subtensor` (blocking, or async when awaited); typed reads (`sub.subnets`, `sub.neurons`, `sub.weights`, `sub.staking`, `sub.epochs`, `sub.hyperparameters`, `sub.collateral`, ...); intents + `plan` / `execute`; `ExtrinsicResult` with `bt.ErrorCode`; `bt.Policy`; `bt.Wallet`; unit-tagged `bt.Balance`; `bt.http_auth` (sign/verify only); `bt.timelock`; raw escape hatch `bt.calls` / `bt.storage` / `client.submit_call` |
| Subnet owner (us) | **Everything between validator and miner**: HTTP server, HTTP client, request/response schema, caller policy, scoring, the weight loop, the miner's service |

v11 ships **no** networking stack: `bt.Axon`, `bt.Dendrite`, `bt.Synapse`,
`bt.StreamingSynapse`, `bt.SubnetsAPI`, `bt.Tensor` do not exist. Mapping from
the migration page: signing → `bt.http_auth.sign` / `verify`; `Axon` → your own
server (FastAPI recipe); `Dendrite` → your own client (httpx recipe);
`Synapse` → your own models (signing covers raw bytes); `blacklist_fn` →
metagraph lookup on the verified hotkey; `priority_fn` → your rate limiting
keyed by caller hotkey; `StreamingSynapse` → SSE / websockets. Chain-side
calls remain: `bt.ServeAxon(netuid, ip, port)` and `bt.SetWeights`.

### Signed requests (btauth/1)

```python
headers = bt.http_auth.sign(wallet, method="POST", path="/generate",
                            body=body_bytes, receiver_ss58=miner_hotkey)   # client
caller = bt.http_auth.verify(request_headers, body_bytes, method="POST",
                             path="/generate", self_hotkey_ss58=my_hotkey)  # server
caller.hotkey_ss58
```

`verify` raises an `AuthError` subclass: `MalformedAuth`, `WrongReceiver`,
`StaleRequest`, `ReplayedRequest`, `BadSignature`.

Wire format (normative): headers `X-Bittensor-Version: 1`, `X-Bittensor-Hotkey`,
`X-Bittensor-Receiver` (optional), `X-Bittensor-Nonce` (unix time in
**nanoseconds**), `X-Bittensor-Crypto: ed25519` (omitted for sr25519),
`X-Bittensor-Signature` (`0x` + lowercase hex). The signature covers eight
`\n`-joined lines: `btauth/1`, scheme, uppercase method, request target in
**percent-encoded** form (path + `?query`), sha256 hex of the raw body, nonce,
sender ss58, receiver ss58 or `-`. sr25519 uses the `b"substrate"` context;
ed25519 is RFC 8032. Golden vectors (Alice → Bob, nonce `1752076800000000000`)
and `bt.http_auth.build_payload(...)` exist for test fixtures.

Verification order: shape → receiver (unbound requests rejected unless
`require_receiver=False`) → freshness (`max_age` 10 s, `allowed_skew` 2 s) →
signature under the declared scheme only → replay check on `(hotkey, nonce)`,
consulted only after the signature verifies. Default `InMemoryNonceStore`;
multi-process servers supply a shared `NonceStore` (Redis `SET NX EX`). Every
attempt signs a fresh nonce; transport-level retries are rejected as replays.

FastAPI rules: hash the **raw** received bytes before parsing; rebuild the
target from `request.scope["raw_path"]` + `query_string` (ASGI `path` is
decoded); 401 for `AuthError`, 403 for verified-but-unwelcome. Caller policy:

```python
mg = await client.subnets.metagraph(netuid)
n = mg.by_hotkey(caller.hotkey_ss58)
ok = n is not None and n.validator_permit and n.tao_stake >= bt.tao(1000)
```

Discovery: `n.axon` is `"ip:port"` or `None`, published with `btcli tx
serve-axon --netuid N --ip A --port P -w coldkey -H hotkey` (hotkey-signed;
`serving_rate_limit` default 50 blocks; also `serve-axon-tls`, `reset-axon`).
A legacy v10 `Dendrite` header can be verified with `bittensor.sp_core.verify`
as a bridge only. Timelock composes with signing:
`timelock.encrypt(challenge, reveal_in="30s")` for synchronized challenges or
copy-proof answers; `X-Bittensor-Drand-*` headers are reserved.

### Validators and weights

- Permits: each epoch the top **K** by stake weight, K = `max_validators`
  (`MaxAllowedValidators`, default 128, root-only). Stake weight =
  `alpha_stake + tao_stake × TaoWeight` (0.18 on mainnet; runtime default
  ~0.053). `StakeThreshold` zeroes smaller stake and is the minimum to set
  weights (runtime default 0; mainnet "around 1,000 TAO-equivalent"). The
  owner's UID is exempt from both gates. Losing a permit **deletes bonds**.
- Activity cutoff: `activity_cutoff_factor` (13,889 per-mille of tempo ≈
  5,000 blocks at tempo 360); a validator silent longer is masked out.
- One entry point: `bt.set_weights(netuid, {uid: w}, wallet="cold",
  hotkey="hot")` (blocking, raises) or `bt.SetWeights(netuid=N, weights={...}
  | uids=[...], weights=[...], mechid=0, version_key=0, raw_u16=False)` via
  the client; CLI `btcli weights set --netuid N --uids 0,1,2 --weights
  0.5,0.3,0.2 -w cold -H hot` or `btcli tx set-weights`. Values are relative;
  the SDK clips, normalizes, quantizes to u16, drops zeros
  (`bittensor.intents.normalize`), preflights registration and rate limit,
  then picks the path: commit-reveal **off** → `set_mechanism_weights`;
  **on** (default for new subnets) → timelock-encrypted
  `commit_timelocked_mechanism_weights`, auto-decrypted by the chain after
  `commit_reveal_period` tempos (default 1); **no manual reveal**. Plain
  `set_weights` is rejected with `CommitRevealEnabled` while on.
- `bt.CommitWeights` / `btcli weights commit` force the timelocked path;
  `bt.RevealWeights` / `btcli weights reveal` are for legacy salted commits.
- Chain rules: `weights_rate_limit` (`WeightsSetRateLimit`, 100 blocks,
  root-only; applied at commit time), `weights_version` key (owner bumps it
  to force new validator code; mismatch → `IncorrectWeightVersionKey`; v11
  defaults `version_key` to 0), `min_allowed_weights`, `max_weights_limit`,
  Yuma ciphertext ≤ 5,000 bytes. Reads: `weights-rate-limit`,
  `blocks-since-last-update`, `epoch-status`, `blocks-until-next-epoch`,
  `reveal-period`, `commit-reveal-enabled`; `client.weights.weights(netuid=N)`
  (`dict[uid, dict[uid, float]]`), `client.weights.bonds(netuid=N)`.
- Owner rule: keep `immunity_period > commit_reveal_period × tempo`, or new
  miners are pruned before their first revealed score. On-chain scores lag by
  the reveal period; never use them as liveness.
- Bootstrapping: the owner gets no stake exemption for weights; the usual
  tactic is a root validator parenting the owner's validator hotkey as a
  childkey (`set-children`).

### Tempo, epochs, Yuma 3

- `tempo` default 360 blocks (~72 min), owner range 360–50,400, one change per
  360 blocks, change resets the cycle. ≤ 2 subnet epochs per block
  (`MaxEpochsPerBlock`). Emission settles at the epoch to whoever holds the UID
  then. The owner can trigger an early epoch (`trigger_epoch` per Latent).
- Yuma: rows normalized; self-weights dropped (except owner); consensus =
  stake-weighted median at `kappa` (≈0.5); weights clipped to consensus; rank
  → incentive; bonds = EMA of weights × stake with `alpha = 1 −
  bonds_moving_avg / 1e6` (900,000 → 0.1); dividends = bonds × incentive.
  Per-miner `trust` / `rank` are deprecated (empty in the metagraph).
- `yuma3_enabled` (owner flag, chain default **false**): fixed-point bonds
  with per-pair scaling and a different dividend formula. `liquid_alpha_enabled`
  (EMA rate between `alpha_low` 0.7 and `alpha_high` 0.9,
  `alpha_sigmoid_steepness` 1000) only works with Yuma 3 on. Latent: no forced
  YC3 migration deadline, validators/miners need no code change.
- Fallbacks: no valid weights → stake-proportional emission; zero miner
  incentive → that tempo's miner alpha goes to validators.

### Miner registration

- Burned: `btcli tx burned-register --netuid N -w coldkey` /
  `bt.BurnedRegister(netuid=N, hotkey_ss58=None)`; price `btcli query burn
  --netuid N --json` / `client.subnets.burn(netuid)`. Price halves every
  `burn_half_life` blocks (360), × `burn_increase_mult` (1.26) per
  registration, clamped to `min_burn` (τ0.0005) … `max_burn` (τ100); burned
  TAO is swapped into the pool and the alpha recycled. Fails with
  `SubNetRegistrationDisabled` when `registration_allowed` is off. One UID per
  hotkey per subnet; no registration windows.
- Proof of work: owner sets `network_pow_registration_allowed` (default off);
  `btcli subnets register --netuid N --pow --yes` (OpenCL GPUs, CPU fallback;
  `--pow-backend`, `--pow-device`, `--pow-workers`, `--pow-timeout`). SDK
  `sub.mine_pow_registration(netuid=, hotkey_ss58=, coldkey_ss58=, workers=)`
  then `sub.execute(proof, wallet)`; intent `bt.PowRegister(netuid,
  work_block, nonce, work_hex)`. Zero-tip PoW pays no burn, collateral or fee.
  PoW raises only `difficulty`, burn only the burn price; both share
  `burn_increase_mult` / `burn_half_life`; `min_difficulty` is root-controlled.
  Proofs valid for five blocks. At least one route must stay enabled.
- Collateral (spec ≥ 435): `collateral_lock_share` `p` (default 0, max 0.95),
  `collateral_drain_ratio` `k` (default 1.0, range (0, 10]). Registration
  charges `(1 − p) × T` burned and `max(0, p × T − standing collateral)` locked
  as alpha on the hotkey; each tempo releases `min(locked − min_locked, k ×
  emission)`. No withdrawal except earned emission; survives deregistration;
  follows `swap-hotkey` (`keep_stake=false`). `bt.AddCollateral(netuid,
  amount_alpha)`, `bt.SetMinCollateral(netuid, min_alpha)`, `btcli collateral
  add|set-min|show|list`, `client.collateral.miner_collateral(...)`,
  `subnet-collateral`, metagraph fields `collateral_locked`, `collateral_min`,
  `collateral_earned`. Enforcement is off-chain: validators stop scoring and
  the lock strands. Score by `hotkey_root`, not UID.
- Never mine on the owner hotkey: that emission is burned or recycled
  (`recycle_or_burn`) and `MinerBurned` cuts the subnet's TAO share.

### Null consensus

Owner-selected alternative via `epoch_consensus` (`Yuma` = 0, `Null` = 1):
`btcli hparams set --netuid N --name epoch_consensus --value Null` or
`SetHyperparameter(netuid=N, name="epoch_consensus", value="Null")`. Root
cannot use it.

- One validator permit: the UID with the highest raw stake weight (ties: first
  UID). Only it may set/commit weights; owner and self-weight exceptions do
  not apply. All positively staked UIDs get dividends pro-rata.
- Half the epoch budget to validators by stake, half to miners by the winner's
  integer ratios; an empty row splits miner rewards equally over every UID. No
  consensus, no bonds.
- Capacity `floor(2500 / mechanism_count)` (the `max_allowed_uids` page says
  16,000 — inconsistent; guide and `epoch_consensus` page say 2,500). Yuma:
  `floor(256 / mechanism_count)`.
- Exact weights: `SetWeights(netuid=N, weights={2: 65534, 3: 1},
  raw_u16=True)`; `btcli misc weights set --netuid N --raw-u16
  --weights-file weights.json`. Ciphertext ≤ `floor(32 KiB /
  mechanism_count)`; queue 64 KiB / 64 commits per epoch.
- Back to Yuma: prune to ≤ 256 first (`btcli hparams trim-null --netuid N
  --switch-to-yuma`, batches of 64, resumable) with pending commits drained.

When it applies: thousands of UIDs, one trusted highest-stake validator, or
linear-cost epochs. A normal multi-validator subnet keeps Yuma.

### Multiple mechanisms

Formerly "subsubnets". `set-mechanism-count` (owner, once per 7,200 blocks);
chain cap **2**; `max_allowed_uids × count ≤ 256` under Yuma. Each mechanism
runs its own Yuma with its own weights and bonds; miners keep one UID and sum
emission; validators set weights per mechanism (`mechid=`) and earn less if
they skip one. Split is a u16 vector summing to 65,535 (`[13107, 52428]` =
20/80), reset to even on count change; reads `mechanism-count`,
`mechanism-emission-split`; Latent names `sudo_set_mechanism_emission_split`.

---

## 4. Economics

**TAO issuance.** 1 TAO/block base, halving on issuance thresholds (10.5M,
15.75M, ...), cap 21M. First halving December 2025: **0.5 TAO/block
(~3,600/day)**. Recycled TAO (registration burns, fees) is re-emittable and
pushes halvings out; burned TAO stays counted.

**A subnet's share per block.** (1) `demand_share_i = price_ema_i / Σ
price_ema` (spot capped at 1.0; emission-disabled subnets get 0); (2) scaled by
`1 − MinerBurned` (last tempo's miner incentive withheld because it targeted
owner hotkeys) and renormalized; (3) **emission gate** `gate = 1 / (1 + (theta
/ share)^h)`, `theta` = the 32nd-highest positive adjusted share (recomputed
every 360 blocks), `h = 3`, renormalized. The EMA is age-dependent: `ema_alpha
= base_alpha × blocks_since_start / (blocks_since_start + 201,600)`, so a new
subnet's moving price starts near zero. Latent omits the gate and says the
flow-based model (Nov 2025–Jun 2026) is deprecated; prefer Rao.

**Alpha.** Up to 1 alpha/block as `alpha_out` for participants, plus
`alpha_in = tao_in / price` injected into the pool, capped by `root_proportion
× alpha_emission`; uninjectable TAO buys alpha (protocol-owned). Each alpha has
its own 21M cap and halving curve. Pools are Balancer-style (weights 0.5/0.5,
bounded [0.01, 0.99]); injections shift weights, not price.

**Per-tempo split: emission weights.** Five slots `[burn, recycle, owner,
validator, miner]` as u64 parts of `u64::MAX`; new subnets start at `[0, 0,
0.18, 0.41, 0.41]`. Rules: burn and recycle must be 0; `0 ≤ owner ≤ 0.33`
(≥ 0.18 on lease-created subnets); `validator ≤ 0.50` and strictly above
root's baseline `r`; `miner ≥ 0.33`; sum = 1. Owner change **once per week**
(50,400 blocks, keyed by subnet; first change free; root exempt):

```bash
btcli subnets emission show --netuid N
btcli subnets emission set --netuid N --owner 0.10 --validator 0.40 --miner 0.50
btcli subnets emission set-value --netuid N --slot owner --value 0.10
btcli subnets emission increase --netuid N --slot miner --by 5%
```

Intent `set-subnet-emission-weights`; read `subnet-emission-weights`.
`owner_emission`, `validator_emission`, `miner_emission` are read-only views;
`owner_cut_enabled` is derived (`owner > 0`).

**Root off the top.** Each block `r = V0 × x / (x + A)` with `V0 = 0.41`,
`x = root_tao × TaoWeight`, `A` = alpha issuance; root TAO stakers get
`min(V, r) × alpha_out` from the validator slot, alpha validators the rest.
Docs example: 5.4M root TAO, 6.5M alpha → `r ≈ 5.4%`.

**Owner cut.** Paid as stake on the owner hotkey each epoch; auto-locked into
conviction only if `owner_cut_auto_lock_enabled` (default **false**). Latent
says always auto-locked; prefer Rao. **Validator dividends** flow through
bonds minus take; the TAO-staker portion is `τw / (α + τw)`, alpha-staker
`α / (α + τw)`.

**Beta tokens.** β is a share count in one root validator's **basket** (a fund
of subnet tokens plus TAO): not a `Balance`, not TAO, not alpha; each
validator's β is a different fund starting at 1 TAO/β. `btcli root allocate`
buys in; `btcli root claim` (`claim-root-with-hotkey`) sells the slice into
root stake. For a subnet builder the only relevance is that root stakers are
paid from our validator slot via `r`.

**Money.** `bt.Balance` is unit-tagged by netuid (0 = TAO); mixing raises
`UnitMismatchError`; `.tao` on alpha raises. `bt.tao()`, `bt.alpha(x, netuid)`,
`bt.rao()`; 1 TAO = 10⁹ rao. Existential deposit 500 rao; min stake 0.002
TAO; swap fee 33/65535 ≈ 0.05%; stake intents apply 5% slippage protection
(`slippage_protection=False` to opt out).

**What makes a subnet lose emissions.** Not started, or root never enabled
`subnet_emission_enabled`; `registration_allowed` false; low EMA price (tiny
share, then the gate) and lowest EMA price among non-immune subnets → pruned
when 128 slots are full; mining on owner hotkeys (`MinerBurned`); no valid
weights → stake-proportional fallback; zero miner incentive → miner alpha to
validators; validators past the activity cutoff are masked, lost permits
delete bonds.

---

## 5. Local development

```bash
docker run --rm --name local_chain \
  -p 9944:9944 -p 9945:9945 \
  ghcr.io/raofoundation/subtensor-localnet:devnet
```

- Default is fast blocks (0.25 s). Append `False` for 12-second blocks.
- Keep state: `docker volume create subtensor-localnet-data`, then run with
  `-v subtensor-localnet-data:/tmp ... :devnet True --no-purge`.
- Tags `devnet` / `testnet` = exact commit on that network, `main` / `latest`
  = current main; re-pull moving tags. Fresh localnet has subnet 0 (root) and
  subnet 1; the first `register-subnet` creates netuid 2.
- Latent still shows `ghcr.io/opentensor/subtensor-localnet:devnet-ready`;
  superseded.
- Source build: `git clone https://github.com/RaoFoundation/subtensor.git`,
  `./scripts/init.sh`, `./scripts/localnet.sh` (`False`, `--no-purge`,
  `--build-only`); listens on `ws://127.0.0.1:9945` (set `BT_CHAIN_ENDPOINT`).
  It builds with the `pow-faucet` feature: the `faucet` extrinsic mints 1,000
  TAO per call (constant in `pallets/subtensor/src/subnets/registration.rs`;
  disabled on mainnet). Whether the Docker image includes the faucet is
  **unconfirmed**; the docs fund from Alice.

Connect: `btcli query is-fast-blocks --network local`;
`async with bt.Subtensor("local") as client: await client.block()`.

Alice (`5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY`) holds 1,000,000
TAO at genesis. Her key is the Substrate dev URI `//Alice`; its public seed is
printed in the local-development guide (not copied here: this repo keeps seeds
out of docs). Wallet file: `btcli wallet regen-coldkey -w alice --no-password
--seed <the //Alice dev seed>`. Never reuse dev keys off a local chain. In
Python no wallet file is needed:

```python
from bittensor.keyfiles import Keypair
alice = Keypair.create_from_uri("//Alice")
result = await client.execute(bt.Transfer(dest_ss58="5F...", amount_tao=1000), alice)
```

The development loop (exact commands from the guide):

```bash
btcli wallet create -w owner -H default --no-password
btcli wallet create -w validator -H default --no-password
btcli wallet create -w miner -H default --no-password

btcli tx transfer --dest owner --amount-tao 2000 -w alice --network local -y
btcli tx transfer --dest validator --amount-tao 100 -w alice --network local -y
btcli tx transfer --dest miner --amount-tao 100 -w alice --network local -y

btcli tx register-subnet -w owner --network local          # creates netuid 2
btcli tx start-call --netuid 2 -w owner --network local
btcli tx burned-register --netuid 2 -w validator --network local
btcli tx burned-register --netuid 2 -w miner --network local
btcli query metagraph --netuid 2 --network local --json
```

Then `btcli tx serve-axon --netuid 2 --ip ... --port ... -w miner -H default
--network local`, run the miner server and validator loop, tune with
`btcli query subnet-hyperparameters --netuid 2 --network local --json` and
`btcli tx set-hyperparameter --netuid 2 --name <name> --value <v> -w owner`.
Localnet sudo is Alice, so root-only flags can be flipped, e.g.
`btcli call AdminUtils.sudo_set_subnet_emission_enabled --args '{"netuid": 2, "enabled": true}' --sudo`.

Configuration: flag > env > `~/.bittensor/btcli.json` (`btcli config set
network local`, `btcli config set wallet owner`) > default. Env: `BT_NETWORK`,
`BT_WALLET`, `BT_WALLET_HOTKEY`, `BT_WALLET_PATH`, `BT_WALLET_PASSWORD` /
`BT_WALLET_PASSWORD_FILE`, `BT_CHAIN_ENDPOINT`, `BTCLI_CONFIG`. `--json`,
`--yes` (no TTY and no `--yes` → exit 1, never hangs), `--dry-run` on any `tx`;
`client.plan(intent, wallet)` before `execute`. Logging is standard `logging`
under `bittensor.*`. **Fast-blocks caveat:** rate limits, immunity, activation
delay and tempo elapse 48× faster than mainnet; use `False` to test timing.

---

## 6. Gotchas and open questions

1. Registration cost: live τ482.5 vs documented `NetworkMinLockCost` default
   1,000 TAO. Which floor applies on finney is **unconfirmed**; read
   `subnet-registration-cost` before any budget.
2. Subnet-registration rate limit is stated three ways (7,200 network-wide /
   per coldkey / 14,400). **Unconfirmed.**
3. Testnet has 596 subnets against a documented cap of 128; testnet limit and
   immunity rules are **unconfirmed**.
4. TAO emission needs both the owner's `start-call` and root's
   `subnet_emission_enabled`; the second needs the Rao sudo key and the docs
   do not say how to request it. **Open.**
5. `registration_allowed = false` drops the subnet from the emission set. Keep
   burn on; whether PoW-only satisfies the check is **unconfirmed**.
6. `commit_reveal_weights_enabled` defaults to **true** for new subnets:
   validators must use `set_weights` (auto-timelock) and `immunity_period` must
   exceed `commit_reveal_period × tempo`. Drand pulses must arrive in order; a
   missing round stalls every timelock reveal chain-wide.
7. Owner hyperparameter changes: ~once per parameter per two tempos
   (`OwnerHyperparamRateLimit` = 2), rejected in the last ~10 blocks of a tempo
   (`AdminFreezeWindow`); `tempo` once per 360 blocks.
8. `btcli hparams ...` (Null pages) vs `btcli sudo set/get` (everywhere else):
   whether `hparams` aliases `sudo` is **unconfirmed**. The generated
   `set-hyperparameter` page is the authoritative name list (it says
   `owner_immune_neuron_limit`; the subnets guide says
   `immune_owner_uids_limit`).
9. Rao docs disagree on Null capacity (2,500 vs 16,000); assume 2,500.
10. `yuma3_enabled` and `liquid_alpha_enabled` default to **false**; decide
    early which bond path we design for.
11. `StakeThreshold` (~1,000 TAO-equivalent on mainnet) gates the owner's
    validator too; the only documented workaround is a childkey from a root
    validator. Testnet value **unconfirmed**.
12. `version_key` defaults to 0; if we enforce `weights_version` the validator
    must pass it explicitly.
13. The metagraph is per-neuron records, not matrices; `rank` / `trust` are
    empty; weights and bonds come from `client.weights.*` as floats. `Balance`
    is unit-strict: never sum alpha across subnets or call `.tao` on alpha.
14. Latent docs use v9 CLI syntax (`btcli subnet create`, `--param`,
    `btcli subnet start`), the old image name, 4-month subnet immunity, a
    tiered lock refund, a fixed 18/41/41 split and an auto-locked owner cut.
    All superseded by the Rao docs.
15. No subnet template ships with Bittensor 11; the FastAPI/httpx recipe in the
    signed-requests guide is the only transport reference. Server, client,
    schema and nonce store are ours to design.
16. Testnet TAO: the Rao docs document no testnet faucet; Latent says ask in
    Discord. **Open.**

---

## Sources

RaoFoundation subtensor docs, read 2026-10-19 as raw files under
`https://raw.githubusercontent.com/RaoFoundation/subtensor/main/docs/`:
guides/subnets.mdx, guides/signed-requests.mdx, guides/local-development.mdx,
migration.mdx, guides/validating.mdx, guides/mining/index.mdx,
guides/mining/burn.mdx, guides/mining/collateral.mdx,
guides/null-consensus.mdx, concepts/emissions.mdx,
concepts/emission-weights.mdx, concepts/beta-tokens.mdx, concepts/money.mdx,
concepts/network.mdx, concepts/chain-consensus.mdx, agents.mdx,
quickstart.mdx, sdk.mdx, hyperparameters/index.mdx,
hyperparameters/immunity-period.mdx, hyperparameters/tempo.mdx,
hyperparameters/max-allowed-uids.mdx, hyperparameters/owner-emission.mdx,
hyperparameters/miner-emission.mdx, hyperparameters/validator-emission.mdx,
hyperparameters/registration-allowed.mdx,
hyperparameters/network-pow-registration-allowed.mdx,
hyperparameters/subnet-is-active.mdx,
hyperparameters/subnet-emission-enabled.mdx,
hyperparameters/commit-reveal-weights-enabled.mdx,
hyperparameters/yuma3-enabled.mdx, hyperparameters/epoch-consensus.mdx,
tx/register-subnet.mdx, tx/start-call.mdx, tx/set-weights.mdx,
tx/commit-weights.mdx, tx/burned-register.mdx, tx/pow-register.mdx,
tx/set-hyperparameter.mdx, tx/set-subnet-identity.mdx, tx/serve-axon.mdx,
query/subnet-registration-cost.mdx, query/metagraph.mdx, query/neurons.mdx,
query/subnet-hyperparameters.mdx, internals/null-consensus-testnet.mdx.

Latent developer docs, read 2026-10-19 as raw files under
`https://raw.githubusercontent.com/latent-to/developer-docs/main/docs/`:
learn/emissions.md, learn/yuma3-migration-guide.md,
subnets/subnet-deregistration.md, subnets/uid-trimming.md,
subnets/understanding-multiple-mech-subnets.md, subnets/create-a-subnet.md,
local-build/deploy.md.

All 52 fetches returned HTTP 200. Live numbers (τ482.5 finney, τ1.0 test, 596
testnet subnets, image tags) are from the 2026-10-19 queries recorded in
`01-bittensor-setup.md` and were not re-run here.
