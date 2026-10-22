# Running a trading agent

An agent is your trading code running in a **sealed machine**: a Google Cloud Confidential Space virtual machine
started from Kokoah's published harness image. The machine creates your agent's Hyperliquid account inside itself and
never lets its key out, so nobody (you included) can trade that account by hand; every validator checks the machine
every day, and a verified agent earns 10% more skill weight (specification v6, sections 7a and 16).

## 1. Write the agent

A directory with an `entry.py` at its top. Everything it imports must be inside the directory: the agent has no
network, so nothing is installed when it starts. `deploy/agent/example/entry.py` is a complete example that uses only
the Python standard library.

The agent talks to the harness over a Unix socket whose path is in `KOKOAH_AGENT_SOCKET`:

| Request | What it does |
|---|---|
| `POST /info {"request": {...}}` | read-only exchange data: `meta`, `metaAndAssetCtxs`, `allMids`, `l2Book`, `candleSnapshot`, `fundingHistory`, `predictedFundings`; account queries (`clearinghouseState`, `openOrders`, `userFills`, `userFillsByTime`, `orderStatus`, `historicalOrders`, `userFees`, `portfolio`, ...) answer for the agent's own account |
| `POST /call {"method": "order", "params": {"orders": [...], "grouping": "na"}}` | limit or trigger orders: `{"coin", "is_buy", "sz", "limit_px", "order_type": {"limit": {"tif": "Gtc"/"Ioc"/"Alo"}} or {"trigger": {"triggerPx", "isMarket", "tpsl"}}, "reduce_only"}` |
| `cancel {"coin", "oid"}`, `cancel_by_cloid {"coin", "cloid"}` | cancels |
| `update_leverage {"coin", "leverage", "is_cross"}`, `update_isolated_margin {"coin", "amount_usd", "is_buy"}` | margin |
| `schedule_cancel {"time_ms"}` | a dead man's switch |
| `GET /address` | the account address |

Every order gets Kokoah's builder code (0.025%) and an order id with Kokoah's agent marker; the response returns the
order ids. Prices more than 5% from the mid are refused. Not offered: modify, TWAP, trailing stops, withdrawals,
transfers, approvals. Main perpetuals only.

## 2. Package and submit

```bash
uv run kokoah-subnet agent package ./my-agent --out my-agent.bundle --encrypt
# prints the code fingerprint (commit this), the submission id, and for --encrypt the bundle key (keep it secret)
uv run kokoah-subnet agent submit --service https://api.kokoah.com/miners --bundle my-agent.bundle --wallet <wallet> --wallet-hotkey <hotkey>
```

The same files always give the same fingerprint. Without `--encrypt` the code is public: anyone can download it
from the service and check the fingerprint. One submission per hotkey per 12 hours, and only from a hotkey that is
registered on the subnet (the service also stores key copies only for registered hotkeys; getting an account back
or taking its key out works even after the hotkey lost its registration).

Kokoah's agent service is `https://api.kokoah.com/miners`: `/agents` (submissions and their code), `/images` (the
approved harness images), `/key-store/<account>` (the machines' encrypted key copies) and `/key-store/released`
(every account whose key was taken out).

## 3. Make your secret

The machine keeps a copy of the account key with Kokoah, encrypted with a secret only you hold. Whoever had both
the copy and your secret could take the account's money, so make the secret with the tool:

```bash
uv run kokoah-subnet agent secret --master <your own Hyperliquid address> --hotkey <your hotkey ss58> --out secret.hex
```

It asks for a password and refuses a weak one: at least 16 characters and hard to guess (five or more random words
work well; your hotkey, your address and words like "kokoah" count against it). The secret is made from the
password with scrypt (slow and memory-hard on purpose, so guessing is expensive) and salted with your address; the
same password always makes it again, and the password itself never leaves your computer. `--generate` makes a
random secret instead: the strongest, but it exists only in the file, so keep a copy offline. The machine refuses a
secret that is clearly not random. Kokoah adds its own encryption over every stored copy, so its database alone
opens nothing either.

## 4. Start the sealed machine

On your own Google Cloud project (you pay for the machine; Kokoah runs nothing for you). The harness image is
published as `docker.io/kokoahdotcom/kokoah-subnet-agent`; its digest must be one of the approved images
(`src/kokoah_subnet/agent/images.py`). The machine runs as a service account with the Confidential Computing Workload
User role (the default compute account lacks it, and without it the machine cannot get its proof).

The image is reproducible: `deploy/agent/build.sh <commit>` (the commit listed next to the digest in `images.py`)
rebuilds it from the repository and prints its digest, which must equal the approved one. Check it before you trust
a harness with your money.

```bash
gcloud compute instances create kokoah-agent \
  --zone=us-central1-a --machine-type=c3-standard-4 \
  --confidential-compute-type=TDX --maintenance-policy=TERMINATE --shielded-secure-boot \
  --image-project=confidential-space-images --image-family=confidential-space \
  --service-account=<service account with the Confidential Computing Workload User role> --scopes=cloud-platform \
  --metadata="^~^tee-image-reference=docker.io/kokoahdotcom/kokoah-subnet-agent@sha256:<approved digest>\
~tee-env-KOKOAH_AGENT_SERVICE=https://api.kokoah.com/miners\
~tee-env-KOKOAH_AGENT_SUBMISSION=<submission id>\
~tee-env-KOKOAH_AGENT_BUNDLE_KEY=<bundle key, for an encrypted bundle>\
~tee-env-KOKOAH_AGENT_NETUID=<netuid>\
~tee-env-KOKOAH_AGENT_HOTKEY=<your hotkey ss58>\
~tee-env-KOKOAH_AGENT_MASTER=<your own Hyperliquid address: the only withdrawal destination>" \
  --metadata-from-file=tee-env-KOKOAH_AGENT_SECRET=secret.hex
gcloud compute firewall-rules create kokoah-agent-8080 --allow=tcp:8080   # validators reach the machine here
```

Use the production `confidential-space` image family, never `confidential-space-debug`: a debug machine is refused
by every validator.

**If the machine stops.** The machine stores a copy of the account key encrypted with your secret (Kokoah cannot
read it). Start a new machine with the same settings plus `tee-env-KOKOAH_AGENT_RESTORE=<account address>`: it
proves to the service that it is a genuine sealed machine, gets the copy back, and the same account carries on as an
agent. Keep your password (or the generated file) safe: without it the copy cannot be opened, and nobody can
recover it for you.

**Taking the key out yourself** (for example to trade the account by hand) ends the agent bonus for that account for
good, and the release is public:

```bash
uv run kokoah-subnet agent message release --account <account>   # sign the printed message with your MASTER address
uv run kokoah-subnet agent release --service https://api.kokoah.com/miners --account <account> --time-ms <time_ms> \
  --signature <0x signature> --master <your master address> --out key.txt   # asks for your password
# or --secret-file secret.hex instead of --master, for a generated secret
```

## 5. Bind and fund

```bash
curl http://<machine>:8080/status          # the account address, the code fingerprint, the hotkey
uv run kokoah-subnet agent bind --netuid <netuid> --endpoint http://<machine>:8080 --network finney --wallet <wallet> --wallet-hotkey <hotkey>
```

`agent bind` reads the binding line the machine signed with the account's key and publishes it from your hotkey
with the agent field (the code fingerprint and the machine's address). Then deposit USDC to the account address on
Hyperliquid (at least $10,000 for the account to be eligible). USDC that lands in the account's spot balance (as it
does with Hyperliquid's "send") is moved to its perpetuals balance within a minute; the harness approves Kokoah's
builder fee as soon as the account is funded, and your agent starts trading.

## 6. Withdraw or stop

```bash
uv run kokoah-subnet agent message withdraw --account <account> --amount 100
# sign the printed message with your own (MASTER) address, then:
curl -X POST http://<machine>:8080/withdraw -H 'content-type: application/json' \
  -d '{"time_ms": <time_ms>, "signature": "<0x signature>", "amount_usd": "100"}'
```

Withdrawals go only to the master address. `agent message stop` and `POST /stop` cancel all open orders and stop the
agent (positions stay open: close them first with the agent, or let it do so before you stop it).

## What the validators check every day

A fresh challenge to `GET /attestation`; Google's signature on the token; a production Confidential Space machine
on confidential hardware; an approved harness image; the challenge and the binding digest (subnet, hotkey, account,
code fingerprint) in the token; the fingerprint you committed; no other trading key approved on the account; and
the account's key never released to you.
The result of every check is in each validator's record (`agent_checks`).
