# Incentive mechanism in plain words (v6.0, 2026-10-22)

The formulas are in `02-mechanism-spec.md`; this page says what they do. The public, polished version is the
whitepaper (`docs/whitepaper.md`).

## What the subnet is

Miners are traders. A miner links one Hyperliquid address to a subnet hotkey and trades perpetuals through Kokoah,
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
   with the miner's Kokoah volume over 30 days, up to $1 million. Verified agents get 10% more. Accounts that trade
   almost exactly alike share one share, so copies and split accounts gain nothing. On a day nobody passes, this
   part joins the activity part.

Nothing is capped and nothing is burned (only a day with nobody to pay would go to the burn hotkey).

## Fake volume

Trading only to earn the activity reward is allowed by design: it is a competition. The reward per fee dollar falls
as more fees are paid, so people who trade only for the reward join until it is worth about what their trading
costs them, and the Kokoah fee they pay is Kokoah's revenue. What fake volume cannot do is win the skill reward: it
has no edge, and accounts with no edge almost never pass the 90-day test. Trades between two of our own miners never
count, and linked addresses that keep opposite positions for weeks lose their credit and leave the skill reward.

## Agents

A miner can run an agent: their trading code inside a sealed cloud machine running Kokoah's harness. The machine
creates the trading account's key inside itself and never lets it out, so nobody, the miner included, can trade
that account by hand. The agent has no internet; it can only ask the harness to place orders, which always carry
Kokoah's fee. Every validator checks, every day, the cloud provider's signed proof that the machine runs the approved
harness with the code the miner committed to. A verified agent gets 10% more weight in the skill reward.

## Where the trades come from

Validators read each linked trader's own trade history from Hyperliquid, which shows the Kokoah fee on every trade,
and check it against Hyperliquid's daily list of Kokoah trades, which has so far covered only about half of each
day. A trade the daily list misses counts only when the trader's own signed order shows Kokoah's builder code. Every
day the total credited to traders is compared with what Hyperliquid says Kokoah has earned; more would raise an
alarm.

## The skill score, briefly

It asks two things of the daily returns, with deposits removed and each day capped at ±10%: is the edge
statistically real (probabilistic Sharpe ratio over 60 days), and is the recent month's profit much larger than its
losses (gain-to-pain over 30 days)? Luck rarely gets past the 90-day test; a strong trader passes about half the
time after 90 days and more often with a longer record.

## Leaving and coming back

Come back with the same hotkey and your records resume, but as a new registration you qualify again with three
trades. Moving to a new address, or swapping the hotkey, keeps your track record, your pay history and any penalty,
but your fee credit and your qualification start again. A new identity has to pass the 90-day test again from its
own record.

## Still open

An agent on a real sealed cloud machine with the released image; a month of real traders and agents on testnet;
legal review.
