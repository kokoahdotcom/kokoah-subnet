# Incentive mechanism in plain words (v6.2)

The formulas are in the [specification](02-mechanism-spec.md); this page says what they do. The full picture is in
the [whitepaper](../whitepaper.md).

## What the subnet is

The goal: trading agents with verified records. Builders compete to write agents that trade well, the subnet
verifies every record from public data, and the agents that hold up for months are to become strategies Kokoah
brings to traders; builder fees and strategy revenue go to the buyback.

Miners are traders and agents. A miner links one Hyperliquid address to a subnet hotkey and trades perpetuals through Kokoah,
by hand or with an agent, so every scored trade carries Kokoah's builder code and pays Kokoah a small fee (0.025% of
the trade). Validators read the public Hyperliquid data of the linked addresses and share the day's miner emission
among them. Kokoah uses the fees to buy back and burn the subnet's token.

## Who can earn on a day

Two simple conditions, checked every day:

- **Real capital.** The account must hold at least $10,000 on average over the day, measured from the exchange's
  own record of the account through the whole day.
- **A real start.** A new miner first makes three trades worth $1,000 together. The first two only count toward
  this; the trade that completes it, and every trade after, earn. You can qualify the day you register.

On a day that falls short, everything is still recorded, but that day's trades earn nothing and nothing is owed
later. The next day at or above $10,000 you earn again.

## How the day's pay is shared

The whole miner emission is paid out every day, in two parts:

1. **80% for activity.** Shared by the Kokoah fees each miner paid on perpetuals (smoothed: 30% today, 70% the
   running figure). Twice the fees, twice the share. Splitting an account in two changes nothing.
2. **20% for proven skill.** Shared only among miners whose daily returns pass a strict test over the last 90 days
   (a 95% confidence that the edge is real). Inside it, the share grows with the skill score above a luck floor and
   with the miner's Kokoah volume over 30 days, up to $1 million. Verified agents get 10% more. Among accounts that
   trade almost exactly alike, only the one whose trades come first is paid, so copies and split accounts gain nothing. On a day nobody passes, this
   part joins the activity part.

Nothing is capped and nothing is burned (only a day with nobody to pay would go to the burn hotkey).

## Fair play

Trades with yourself or your linked accounts never count, and neither do trades between two of the subnet's own
miners. When two miners' accounts keep opposite positions for weeks and the mirrored volume is more than a quarter
of an account's volume, that mirrored part earns no credit; above half, both accounts also leave the skill reward for
30 days. The skill reward needs a real edge, proven over 90 days.

## Agents

A miner can run an agent: their trading code inside a sealed cloud machine running Kokoah's harness. The machine
creates the trading account's key inside itself and never lets it out, so nobody, the miner included, can trade
that account by hand. The agent has no internet; it can only ask the harness to place orders, which always carry
Kokoah's fee. Every validator checks, every day, the cloud provider's signed proof that the machine runs the approved
harness with the code the miner committed to. A verified agent gets 10% more weight in the skill reward. The machine
keeps a copy of the key with Kokoah, locked twice: with the miner's own secret (from a password the tool only accepts
when it is strong) and with Kokoah's own key; only the miner's hotkey can file it, with a signed permission.

## Where the trades come from

Validators read each linked trader's own trade history from Hyperliquid, which shows the Kokoah fee on every trade,
and check it against Hyperliquid's daily list of Kokoah trades. A trade counts only when the trader's own signed
order shows Kokoah's builder code. Every
day the total credited to traders is compared with what Hyperliquid says Kokoah has earned; more would raise an
alarm.

## The skill score, briefly

It asks two things of the daily returns, with deposits removed and each day capped at ±10%: is the edge
statistically real (probabilistic Sharpe ratio over 60 days), and is the recent month's profit much larger than its
losses (gain-to-pain over 30 days)? Only a record that holds up over 90 days passes the test.

## Leaving and coming back

Come back with the same hotkey and your records resume, but as a new registration you qualify again with three
trades. Moving to a new address, or swapping the hotkey, keeps your track record, your pay history and any penalty,
but your fee credit and your qualification start again. The 90-day test always reads the account's own record,
whatever hotkey it is linked to.
