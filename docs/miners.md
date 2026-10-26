# Miner guide

Mining on the Kokoah subnet means trading. A miner is a Hyperliquid trader whose account is linked to a registered
Bittensor hotkey and who trades perpetuals through [Kokoah](https://kokoah.com). There is no miner software to run:
validators read your trades and your account from public data and pay you from the subnet's emission.

This guide covers everything from registration to getting paid. The rules behind each step are in the
[whitepaper](whitepaper.md) and, with every formula, in the [specification](design/02-mechanism-spec.md).

**Networks.** The subnet runs on the Bittensor test network as **netuid 385** today. The mainnet netuid will be
published at launch. The examples below use the test network (`--network test`); on mainnet use `--network finney`
and the mainnet netuid.

## Contents

1. [What you need](#1-what-you-need)
2. [Install the tools](#2-install-the-tools)
3. [Register a hotkey](#3-register-a-hotkey)
4. [Link your Hyperliquid account](#4-link-your-hyperliquid-account)
5. [Trade](#5-trade)
6. [Qualify and stay eligible](#6-qualify-and-stay-eligible)
7. [How you are paid](#7-how-you-are-paid)
8. [Check your status](#8-check-your-status)
9. [Changing address, hotkey or registration](#9-changing-address-hotkey-or-registration)
10. [Trading with an agent](#10-trading-with-an-agent)
11. [Keeping your keys safe](#11-keeping-your-keys-safe)
12. [Troubleshooting](#12-troubleshooting)

## 1. What you need

- **A Bittensor wallet**: a coldkey (holds your funds; keep it offline) and a hotkey (your miner identity).
- **TAO for registration.** Registering a hotkey on the subnet burns a registration fee set by the chain; `btcli`
  shows the current amount before you confirm. Test TAO has no value and is available from the Bittensor community.
- **A Hyperliquid account controlled by an ordinary wallet** (one private key, such as MetaMask or a hardware
  wallet). Smart-contract wallets cannot sign the link message.
- **An average account value of at least $10,000** on the days you want to be paid (section 6).
- **Your trades routed through Kokoah**, so that every order carries Kokoah's builder code.

## 2. Install the tools

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kokoahdotcom/kokoah-subnet
cd kokoah-subnet
uv sync
uv run btcli --version            # Bittensor's command line, installed with the project
uv run kokoah-subnet --help
```

Run these on the computer where your Bittensor wallet lives (`~/.bittensor/wallets`). Your keys never leave it.

## 3. Register a hotkey

```bash
uv run btcli wallet create -w miner -H default                    # skip if you already have a wallet
uv run btcli tx burned-register --netuid 385 -w miner -H default --network test
```

The hotkey's ss58 address (`uv run btcli wallet show -w miner -H default`) is your miner identity. One hotkey links to one
Hyperliquid account.

## 4. Link your Hyperliquid account

The link is a short line signed by your Hyperliquid address and published from your hotkey as an on-chain
commitment. Validators check both signatures; nothing else is needed.

**Step 1: get the exact line.**

```bash
uv run kokoah-subnet miner message --netuid 385 --hotkey <hotkey ss58> --address <0x your Hyperliquid address>
```

It prints a line of this form:

```
kokoah-bind-1 netuid=385 hotkey=<hotkey ss58> address=<0x address, lowercase>
```

**Step 2: sign it with the wallet that controls the address.** Use an Ethereum personal-message signature
(`personal_sign`, EIP-191) from any wallet that holds the address's key, for example the wallet you use with the
Kokoah app, or from the command line:

```bash
cast wallet sign --interactive "kokoah-bind-1 netuid=385 hotkey=<hotkey ss58> address=<0x...>"   # Foundry
```

The result is a 0x-hex signature of 132 characters.

**Step 3: publish the link from your hotkey.**

```bash
uv run kokoah-subnet miner bind --network test --netuid 385 \
    --address <0x...> --signature <0x signature> --wallet miner --wallet-hotkey default
```

The command checks the signature against the line before publishing, so a wrong hotkey or address fails on your
computer, not on chain. Publishing uses only the hotkey; no coldkey password is needed.

**Step 4: confirm it.**

```bash
uv run kokoah-subnet miner show --network test --netuid 385
```

Your hotkey should appear with your address and a valid signature.

A link counts for every day scored after it is accepted. A day is scored a few hours after it ends, so a link made
today covers all of today's trades, including those made before you linked. Days scored before your link was
accepted never pay.

## 5. Trade

Trade **perpetuals** on Hyperliquid through Kokoah. Kokoah attaches its builder code to every order, and Hyperliquid
charges the builder fee of **0.025% of the notional** on every fill, maker or taker. That fee is what the volume
reward measures.

**What counts:** every perpetual fill of your linked address that carries Kokoah's builder code.

**What never counts:**

- fills without Kokoah's builder code (trades placed on other front ends);
- spot trades (recorded, never scored);
- trades against yourself or against accounts linked to you (sub-accounts, accounts funded from the same source or
  moving money between each other);
- trades against other miners' linked accounts: the subnet does not pay for volume between its own miners;
- the part of a fill's price more than 0.5% away from the market price: such a fill is valued at the market price,
  and any profit it moves into the account is removed from your returns.

Accounts whose positions mirror each other (one long while the other is short, day after day) are detected. When
mirrored volume is more than a quarter of an account's volume, it is removed from the account's credit; above half,
both accounts also lose their skill standing for 30 days and are reported.

## 6. Qualify and stay eligible

You are paid on a day only when you are **eligible** that day. Two conditions:

**Qualifying trades (once).** A new identity first makes **3 qualifying trades** with at least **$1,000** of
eligible notional in total. A trade is one execution: all fills of one coin and side in the same millisecond. The
first two trades earn no credit; the trade that completes both conditions, and every trade after it, does. There
is no waiting period, and qualification lasts for as long as your identity does (section 9).

**Account value (every day).** The day's **average account value must be at least $10,000**. It is measured from
Hyperliquid's own record of your perpetuals account value, recorded about every two hours, averaged over the whole
UTC day, never from a single snapshot. Falling below costs that day only: there is no penalty, and pay resumes on
the next day at or above the floor. On an ineligible day nothing is added to your credit and you take no share of
the pool, so you dilute nobody.

## 7. How you are paid

The day's miner emission is split into two rewards.

**Volume reward: 80%.** Shared among eligible miners in proportion to the builder fees they paid, smoothed over
recent days. A day's fees count 30% on the day, 21% the next day, 15% the day after, and so on, so every dollar of
fees counts once in total and a single busy day keeps paying for about two weeks. Splitting one account into many
earns nothing extra: the reward is proportional to fees.

**Skill reward: 20%.** Shared among miners who prove a real trading record:

- **Entry test.** At least 90 days of daily returns whose risk-adjusted performance passes a statistical test at
  95% confidence, so a lucky streak does not pay.
- **Activity.** In the last 30 days: at least 10 completed round trips, at least 7 days with a Kokoah fill, and an
  average account value of at least $500.
- **Weight.** Skill above a luck floor, multiplied by participation: full participation at $1 million of eligible
  Kokoah volume over 30 days, less in proportion below it.
- **Copies.** Among accounts whose daily returns correlate above 0.9 over at least 30 days, only the one whose
  trades come first keeps its weight; copying another miner earns nothing.
- **Agents.** A verified trading agent earns 10% more skill weight (section 10).

If nobody qualifies for the skill reward on a day, the volume reward takes the whole emission.

**When.** Each UTC day is scored after it ends, usually within a few hours (validators wait up to 12 hours for the
exchange's daily builder file, then score from your own trade history). Validators set weights every tempo (360
blocks, about 72 minutes), and the chain pays your hotkey in the subnet's alpha each tempo. Rewards accrue as stake
on your hotkey; manage them with `btcli stake`.

## 8. Check your status

- **Leaderboard:** `https://api.kokoah.com/subnet/leaderboard` (JSON), with every miner's rank and pay once a day
  is scored. Any validator's interface serves the same data at `/leaderboard`, with a page at `/`.
- **Bindings:** `uv run kokoah-subnet miner show --network test --netuid 385`, or `/bindings` on a validator's
  interface.
- **Your record:** `https://api.kokoah.com/subnet/accounts/<your address>` (or `/accounts/<address>` on any
  validator's interface): every day's return as the validators score it, the growth curve, the 90-day return and
  worst drop, and the days your account passed as a verified agent.
- **Your day in detail:** a validator's `/days/latest` and `/days/<YYYY-MM-DD>` publish every miner's figures:
  eligibility and the reason when not eligible, the day's average account value, qualification progress, credit,
  fees, skill and its parts, the entry test, and both rewards.

## 9. Changing address, hotkey or registration

Your fee credit and your qualification belong to one **identity**: one hotkey, one linked address, one
registration. Your pay history, skill record and any penalty stay with you whatever changes.

| Change | Fee credit and qualification | Skill record, pay history, penalties |
|---|---|---|
| **New Hyperliquid address** (allowed once per 60 days) | start again | carry over |
| **Hotkey swap on chain** (same UID and coldkey) | start again | carry over |
| **Deregistered, same hotkey registers again** | start again | carry over |
| **A new hotkey links an address** | start from zero | start from zero (penalties follow the address) |

**Moving to a new address.** Get the line for the new address (`miner message ... --address <new>`), sign it with
the new address *and* with the old address, then publish both:

```bash
uv run kokoah-subnet miner bind --network test --netuid 385 --address <new> --signature <new address's signature> \
    --previous-address <old> --previous-signature <old address's signature> --wallet miner --wallet-hotkey default
```

**After a hotkey swap**, publish the link again: the signed line names the hotkey.

**Unlinking.** The linked address can end its link at any time with one signature, without the hotkey: on
kokoah.com (Settings, subnet miner), or from the command line:

```bash
uv run kokoah-subnet miner unlink-message --hotkey <hotkey ss58> --address <0x...>   # prints the line and its time
# sign the line with the address (personal_sign, EIP-191) within 10 minutes, then:
uv run kokoah-subnet miner unlink --hotkey <hotkey ss58> --address <0x...> --time-ms <time> --signature <0x...>
```

From the next UTC day the hotkey earns nothing from the address, its unpaid credit is lost (as when leaving), the
address can be linked by another hotkey and the hotkey can link another address. This hotkey and this address can
never be linked again. Penalties stay with the address.

Leaving the subnet forfeits the part of your credit not yet paid out (with the 30% smoothing, most of the last few
days' fees).

## 10. Trading with an agent

Instead of trading by hand, you can run your own trading code in a sealed cloud machine started from Kokoah's
published harness. The machine creates the trading account's key inside itself, so nobody, you included, can
trade that account by hand, and validators verify the machine every day. A verified agent earns 10% more skill
weight. Everything else in this guide applies to its account too. See the [agent guide](agents.md).

## 11. Keeping your keys safe

- **Your coldkey never needs to be online** for mining. Linking and everything else here use the hotkey only.
- **Nobody from Kokoah will ever ask** for a mnemonic, a private key, a coldkey password or a seed phrase, and the
  Kokoah app never asks for Bittensor keys. The link needs only a signature, which reveals no key.
- **Sign only the exact line** printed by `miner message`. It names this subnet, your hotkey and your address and
  can do nothing else.

## 12. Troubleshooting

| Symptom | What to check |
|---|---|
| `miner bind` says the signature does not match | The signed text must be exactly the printed line, signed by the address in it (lowercase), as a personal message (EIP-191), not typed data. |
| My binding does not show in `miner show` | The commitment needs the hotkey to be registered on the subnet and a few blocks to be included. |
| Not eligible: account value | The day's *average* must be at least $10,000. A deposit late in the day raises the average only for the hours after it. |
| Not eligible: account value unknown | The exchange recorded too few points of your account that day (fewer than 4). The validator never guesses; the next day is measured again. |
| My trades do not count | Make sure they are perpetuals placed through Kokoah, from the linked address, not against your own or another miner's account. |
| No skill reward | The skill reward needs 90 days of record, the activity conditions and the entry test; until then you earn the volume reward. |
