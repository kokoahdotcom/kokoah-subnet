# Running a trading agent

An agent is your trading code running in a **sealed machine**: a Google Cloud Confidential Space virtual machine
started from Kokoah's published harness image. The machine creates your agent's Hyperliquid account inside itself and
never lets its key out, so nobody (you included) can trade that account by hand; every validator checks the machine
every day, and a verified agent earns 10% more skill weight ([specification](design/02-mechanism-spec.md), sections
7a and 16).

**Networks.** The subnet runs on the Bittensor test network as **netuid 385** today, and the examples below use it
(`--netuid 385`, `--network test`). On mainnet use `--network finney` and the mainnet netuid, published at launch.
The trading that counts is on Hyperliquid's main exchange, with real USDC, on either Bittensor network: validators
read Hyperliquid mainnet. So the machine below runs on Hyperliquid mainnet (`KOKOAH_AGENT_NETWORK=mainnet`) and the
account needs a real deposit (section 5); Hyperliquid's test exchange is only for trying an agent, and nothing traded
there counts.

## 1. Write the agent

A directory with an `entry.py` at its top. Everything it imports must be inside the directory: the agent has no
network, so nothing is installed when it starts. `deploy/agent/example/entry.py` is a complete example that uses only
the Python standard library.

The agent talks to the harness over a Unix socket whose path is in `KOKOAH_AGENT_SOCKET`:

| Request | What it does |
|---|---|
| `POST /info {"request": {...}}` | read-only exchange data: `meta`, `metaAndAssetCtxs`, `allMids`, `l2Book`, `candleSnapshot`, `fundingHistory`, `predictedFundings`; account queries, answered for the agent's own account: `clearinghouseState`, `openOrders`, `frontendOpenOrders`, `userFills`, `userFillsByTime`, `userFunding`, `orderStatus`, `historicalOrders`, `userFees`, `userRateLimit`, `portfolio`. Nothing else is answered |
| `POST /call {"method": "order", "params": {"orders": [...], "grouping": "na"}}` | limit or trigger orders: `{"coin", "is_buy", "sz", "limit_px", "order_type": {"limit": {"tif": "Gtc"/"Ioc"/"Alo"}} or {"trigger": {"triggerPx", "isMarket", "tpsl": "tp"/"sl"}}, "reduce_only"}` |
| `cancel {"coin", "oid"}`, `cancel_by_cloid {"coin", "cloid"}` | cancels |
| `update_leverage {"coin", "leverage", "is_cross"}`, `update_isolated_margin {"coin", "amount_usd", "is_buy"}` | margin |
| `schedule_cancel {"time_ms"}` | a dead man's switch |
| `GET /address` | the account address |

Every order gets Kokoah's builder code (0.025%) and an order id with Kokoah's agent marker; the response returns the
order ids. An order that executes now (a limit order, or a trigger order already past its trigger price) is refused
when its price is more than 5% from the mid. A trigger order that has not fired yet (a stop-loss or take-profit)
rests wherever you place it, and its limit price must be within 5% of its trigger price. Not offered: modify, TWAP,
trailing stops, withdrawals, transfers, approvals. Main perpetuals only.

## 2. Package and submit

The commands below run from the repository checkout, but they write your agent's keys and secrets to a folder of
their own, outside it, so that no later commit from the checkout can pick them up:

```bash
mkdir -p -m 700 ~/kokoah-agent
uv run kokoah-subnet agent package ./my-agent --out ~/kokoah-agent/my-agent.bundle
# encrypted: writes the bundle key to ~/kokoah-agent/my-agent.bundle.key (keep it secret); prints the code
# fingerprint and the submission id
uv run kokoah-subnet agent submit --service https://api.kokoah.com/miners --bundle ~/kokoah-agent/my-agent.bundle \
  --wallet <wallet> --wallet-hotkey <hotkey>
```

The same files always give the same fingerprint. Version-control and cache folders (`.git`, `__pycache__`) are left
out, and the tool refuses to package a file that looks like a key or a secret: by its name, whatever its case
(`.env`, `*.key`, `*.ppk`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, SSH `id_*` keys and `.ssh` folders, `secret.hex`,
`key.txt`, a Bittensor wallet's `coldkey`, `coldkeypub.txt` or `hotkeys` folder, a `wallets` folder, an Ethereum
`UTC--` keystore file), or by its content, whatever its name (a PEM or PGP private key, an Ethereum keystore or a
Bittensor key file). Certificate files are fine. Keep keys outside the agent directory. The code is encrypted so that
only your sealed machine can read it; `--public` publishes it unencrypted instead, and then anyone can download it
from the service. One submission per hotkey per 12 hours, and only from a hotkey that is registered on the subnet
(the service also stores key copies only for registered hotkeys; getting an account back or taking its key out works
even after the hotkey lost its registration).

Kokoah's agent service is `https://api.kokoah.com/miners`: `/agents` (submissions and their code), `/images` (the
approved harness images), `/key-store/<account>` (the machines' encrypted key copies) and `/key-store/released`
(every account whose key was taken out).

## 3. Make your secret

The machine keeps a copy of the account key with Kokoah, encrypted with a secret only you hold. Whoever had both
the copy and your secret could take the account's money, so make the secret with the tool:

```bash
uv run kokoah-subnet agent secret --master <your own Hyperliquid address> --hotkey <your hotkey ss58> \
  --out ~/kokoah-agent/secret.hex
```

It asks for a password and refuses a weak one: at least 16 characters and hard to guess (five or more random words
work well; your hotkey, your address and words like "kokoah" count against it). The secret is made from the
password with scrypt (slow and memory-hard on purpose, so guessing is expensive) and salted with your address; the
same password always makes it again, and the password itself never leaves your computer. `--generate` makes a
random secret instead: the strongest, but it exists only in the file, so keep a copy offline. The machine refuses a
secret that is clearly not random. Kokoah adds its own encryption over every stored copy, so its database alone
opens nothing either.

Then sign the start permission with your hotkey, on your own computer (your wallet never goes to the machine):

```bash
uv run kokoah-subnet agent authorize --netuid 385 --master <your own Hyperliquid address> \
  --submission <submission id> --wallet <wallet> --wallet-hotkey <hotkey> --out ~/kokoah-agent/permit.txt
```

It names the subnet, your hotkey, your master address and the code, and is good for one machine start within 7
days. The service files a machine's key copy only with it: signed by that hotkey, for this subnet, for that address
and code, used once, from a registered hotkey (at most 50 kept copies per hotkey). So nobody else can file copies
under your hotkey, and nobody can use up anything of yours. A restore does not need a new permission.

## 4. Start the sealed machine

On your own Google Cloud project (you pay for the machine; Kokoah runs nothing for you). The harness image is
published as `docker.io/kokoahdotcom/kokoah-subnet-agent`; start from the newest approved image
(`src/kokoah_subnet/agent/images.py`). The machine runs as a service account with the Confidential Computing Workload
User role (the default compute account lacks it, and without it the machine cannot get its proof).

The image is reproducible: `deploy/agent/build.sh <commit>` (the commit listed next to the digest in `images.py`)
rebuilds it from the repository and prints its digest, which must equal the approved one. Check it before you trust
a harness with your money.

```bash
gcloud compute instances create kokoah-agent \
  --zone=us-central1-a --machine-type=c3-standard-4 \
  --confidential-compute-type=TDX --maintenance-policy=TERMINATE --shielded-secure-boot \
  --image-project=confidential-space-images --image-family=confidential-space --tags=kokoah-agent \
  --service-account=<service account with the Confidential Computing Workload User role> --scopes=cloud-platform \
  --metadata="^~^tee-image-reference=docker.io/kokoahdotcom/kokoah-subnet-agent@sha256:<approved digest>\
~tee-env-KOKOAH_AGENT_SERVICE=https://api.kokoah.com/miners\
~tee-env-KOKOAH_AGENT_SUBMISSION=<submission id>\
~tee-env-KOKOAH_AGENT_NETUID=385\
~tee-env-KOKOAH_AGENT_HOTKEY=<your hotkey ss58>\
~tee-env-KOKOAH_AGENT_MASTER=<your own Hyperliquid address: the only withdrawal destination>\
~tee-env-KOKOAH_AGENT_NETWORK=mainnet" \
  --metadata-from-file=tee-env-KOKOAH_AGENT_SECRET=$HOME/kokoah-agent/secret.hex,\
tee-env-KOKOAH_AGENT_PERMIT=$HOME/kokoah-agent/permit.txt,\
tee-env-KOKOAH_AGENT_BUNDLE_KEY=$HOME/kokoah-agent/my-agent.bundle.key   # leave the bundle key out for a --public bundle
# validators reach the machine on port 8080: open it to this machine only (its tag), not to every machine you run
gcloud compute firewall-rules create kokoah-agent-8080 --allow=tcp:8080 --target-tags=kokoah-agent
```

Use the production `confidential-space` image family, never `confidential-space-debug`: a debug machine is refused
by every validator.

`KOKOAH_AGENT_NETWORK` chooses the exchange: `mainnet` (the default), where the subnet's trading counts, or
`testnet`, to try an agent on Hyperliquid's test exchange, where nothing it trades counts. Any other value stops the
machine. Every start setting is its own `tee-env-<NAME>` entry, named exactly as above (the image accepts no other
names); the `^~^` at the start makes `~` the separator between them, so leave no spaces around it.

**Check that it started.** A production machine shows no logs, so check it from outside before you bind it:

```bash
curl http://<machine>:8080/status
# {"address": the agent account, "netuid": 385, "hotkey": yours, "master": your address, "code_sha256": the
#  fingerprint `agent package` printed, "harness_version", "builder_approved": false until the account is funded,
#  "network": "mainnet", "started_at"}
curl "http://<machine>:8080/attestation?challenge=$(openssl rand -hex 32)"   # a token: the machine proves itself
```

If `/status` does not answer a few minutes after the start, the machine stopped. Check every setting (an address
with a typo, an unknown network, a missing or weak secret, a start permission that was used or is more than 7 days
old, a bundle key that does not open the submission), then start a new machine.

**If the machine stops.** The machine stores a copy of the account key encrypted with your secret (Kokoah cannot
read it). Start a new machine from the newest approved image (its digest in `tee-image-reference`), with the same
settings plus `tee-env-KOKOAH_AGENT_RESTORE=<account address>`: it proves to the service that it is a genuine sealed
machine, gets the copy back, and the same account carries on as an agent. Keep your password (or the generated file)
safe: without it the copy cannot be opened, and nobody can recover it for you.

**Taking the key out yourself** (for example to trade the account by hand) ends the agent bonus for that account for
good, and the release is public:

```bash
uv run kokoah-subnet agent message release --account <account>   # sign the printed message with your MASTER address
uv run kokoah-subnet agent release --service https://api.kokoah.com/miners --account <account> --time-ms <time_ms> \
  --signature <0x signature> --master <your master address> --out ~/kokoah-agent/key.txt   # asks for your password
# or --secret-file ~/kokoah-agent/secret.hex instead of --master, for a generated secret
```

## 5. Bind and fund

```bash
uv run kokoah-subnet agent bind --netuid 385 --network test --endpoint http://<machine>:8080 \
  --wallet <wallet> --wallet-hotkey <hotkey>
```

`agent bind` reads the binding line the machine signed with the account's key and publishes it from your hotkey with
the agent field (the code fingerprint and the machine's address). Then deposit USDC to the account address
(`address` in `/status`) on Hyperliquid (at least $10,000 for the account to be eligible). USDC that lands in the
account's spot balance (as it does with Hyperliquid's "send") is moved to its perpetuals balance within a minute;
the harness approves Kokoah's builder fee as soon as the account is funded, and your agent starts trading.

## 6. Withdraw or stop

```bash
uv run kokoah-subnet agent message withdraw --account <account> --amount 100
# sign the printed message with your own (MASTER) address, then send the printed body before the printed deadline:
curl -X POST http://<machine>:8080/withdraw -H 'content-type: application/json' \
  -d '{"time_ms": <time_ms>, "signature": "<0x signature>", "amount_usd": "100"}'
```

Withdrawals go only to the master address. `agent message stop` and `POST /stop` cancel all open orders and stop the
agent (positions stay open: close them first with the agent, or let it do so before you stop it). A signed message
is good for 5 minutes from its time and works once; for another withdrawal or stop, print a new message.

## What the validators check every day

Every validator checks every agent binding once a day, in this order. The first check that fails gives the reason
recorded in the validator's record (`agent_checks`), and the account earns no agent bonus that day.

| Check | Reasons when it fails |
|---|---|
| 1. The account's key was never released to you (the service's `/key-store/released` list, read every day) | `the key was released to the miner`; `released list unavailable` (then no account counts as an agent that day) |
| 2. The machine answers a fresh challenge: `GET /attestation?challenge=<32 random bytes as hex>` at the published host and port, only on public addresses, within 30 seconds | `machine unreachable`, `machine too slow to answer`, `endpoint refused`, `bad answer`, `check timed out` |
| 3. The answer names the bound account, the committed code fingerprint, your hotkey and this subnet | `the machine speaks for another account`, `the machine runs other code than the committed fingerprint`, `the machine is bound to another hotkey or subnet` |
| 4. The token is Google's, from a production Confidential Space machine on confidential hardware with secure boot, of an approved harness image, and carries both the challenge and the binding digest (subnet, hotkey, account, code fingerprint) | `attestation: ...` (which part failed) |
| 5. No other trading key is approved on the account, so nothing but the machine can sign for it | `the account has other trading keys approved`, `exchange: ...` (the exchange could not be read) |
