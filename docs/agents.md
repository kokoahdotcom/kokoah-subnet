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
uv run kokoah-subnet agent submit --service <agent service URL> --bundle my-agent.bundle --wallet <wallet> --wallet-hotkey <hotkey>
```

The same files always give the same fingerprint. Without `--encrypt` the code is public: anyone can download it
from the service and check the fingerprint. One submission per hotkey per 12 hours.

## 3. Start the sealed machine

On your own Google Cloud project (you pay for the machine; Kokoah runs nothing for you). The harness image digest
must be one of the approved images (`src/kokoah_subnet/agent/images.py`).

```bash
gcloud compute instances create kokoah-agent \
  --zone=us-central1-a --machine-type=c3-standard-4 \
  --confidential-compute-type=TDX --maintenance-policy=TERMINATE --shielded-secure-boot \
  --image-project=confidential-space-images --image-family=confidential-space \
  --scopes=cloud-platform \
  --metadata="^~^tee-image-reference=<registry>/kokoah-agent@sha256:<approved digest>\
~tee-env-KOKOAH_AGENT_SERVICE=<agent service URL>\
~tee-env-KOKOAH_AGENT_SUBMISSION=<submission id>\
~tee-env-KOKOAH_AGENT_BUNDLE_KEY=<bundle key, for an encrypted bundle>\
~tee-env-KOKOAH_AGENT_NETUID=<netuid>\
~tee-env-KOKOAH_AGENT_HOTKEY=<your hotkey ss58>\
~tee-env-KOKOAH_AGENT_MASTER=<your own Hyperliquid address: the only withdrawal destination>\
~tee-env-KOKOAH_AGENT_SEAL_SECRET=<64+ hex characters only you keep>"
gcloud compute firewall-rules create kokoah-agent-8080 --allow=tcp:8080   # validators reach the machine here
```

Use the production `confidential-space` image family, never `confidential-space-debug`: a debug machine is refused
by every validator. Keep the seal secret and the bundle key: after a restart, start the machine again with the same
values plus `KOKOAH_AGENT_RESTORE=<account address>` and it resumes the same account.

## 4. Bind and fund

```bash
curl http://<machine>:8080/status          # the account address, the code fingerprint, the hotkey
uv run kokoah-subnet agent bind --netuid <netuid> --endpoint http://<machine>:8080 --network finney --wallet <wallet> --wallet-hotkey <hotkey>
```

`agent bind` reads the binding line the machine signed with the account's key and publishes it from your hotkey
with the agent field (the code fingerprint and the machine's address). Then deposit USDC to the account address on
Hyperliquid (at least $10,000 for the account to be eligible); the harness approves Kokoah's builder fee as soon as
the account is funded, and your agent starts trading.

## 5. Withdraw or stop

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
code fingerprint) in the token; the fingerprint you committed; and no other trading key approved on the account.
The result of every check is in each validator's record (`agent_checks`).
