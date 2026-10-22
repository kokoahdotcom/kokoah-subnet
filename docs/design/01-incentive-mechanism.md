# Incentive mechanism in plain words (v5.9, 2026-10-22)

The formulas are in `02-mechanism-spec.md`; this page says what they do. The public, polished version is the
whitepaper (`docs/whitepaper.md`).

## What the subnet is

Miners are traders. A miner links one Hyperliquid address to a subnet hotkey and trades perpetuals through Kokoah,
so every scored trade carries Kokoah's builder code and pays Kokoah a small fee (0.025% of the trade). Validators
read the public Hyperliquid data of the linked addresses and share the day's miner emission among them. Kokoah
uses the fees to buy back and burn the subnet's token. Miners run nothing: register, link, trade.

## Who can earn on a day

Two simple conditions, checked every day:

- **Real capital.** The account must hold at least $10,000 on average over the day. The average comes from the
  exchange's own record of the account through the day, not from one balance at one moment, so a deposit just
  before midnight does not count as a full day.
- **A real start.** A new miner first makes three trades worth $1,000 together. The first two only count toward
  this; the trade that completes it, and every trade after, earn. There is no waiting period: you can qualify the
  day you register.

On a day that falls short, everything is still recorded, but that day's trades earn nothing, you are paid nothing,
and nothing is owed to you later. Your earlier credit fades as it would on a quiet day. The next day at or above
$10,000 you earn again; nobody is ever excluded for good.

## How a miner's daily pay is decided

1. **Count the fees the miner paid through Kokoah today, on perpetuals.** Every perp fill counts the same, maker or
   taker, because the fee is the same on every order; spot fills are kept on the record but not counted for now. Mix today with the past: 30% today, 70% the running figure. Every dollar of fees
   is counted exactly once over time.
2. **Multiply by a small quality factor between 0.85 and 1.** Skill adds up to 0.05 and holding (not selling the
   tokens you were paid) up to 0.10. Two miners with the same volume are always within 15% of each other.
3. **Share the day's pool in proportion.** Twice the volume, about twice the pay.
4. **Cap each miner's pay.** Nobody is paid more than twice the Kokoah fee they paid, and nobody is paid more than
   three quarters of everything the trade cost them (Hyperliquid's fee plus Kokoah's). The cap is converted to
   tokens at the day's average price, so a price dip at the scoring moment changes nothing.
5. **Burn what is left.** When the pool is bigger than the sum of the caps (a young subnet), the rest goes to a
   separate burn hotkey (not the owner's) and a small operator process destroys it on chain, one public
   transaction each time.

## Why cheating does not pay

Every fake-volume strategy (wash trading, a hedge on another venue or in a second account, instant round trips,
dust accounts, lottery farms) pays Hyperliquid and Kokoah more than the subnet can ever pay back, because the pay
is capped at three quarters of what the trade cost. The tightest case is a top-tier market maker who earns rebates
on both legs: still a loss of half a basis point per round trip, and it would need a rebate almost twice
Hyperliquid's best to break even. Splitting into many accounts or merging accounts changes nothing, because shares
and caps are linear in fees. Trades between two of our own miners never count (the exchange's file names the counterparty of every fill),
and linked addresses that keep opposite positions for weeks lose their credit and, above half, their quality
factor. Verified by `scripts/verify_mechanism.py` and by a 60-day replay through the real code (`scripts/replay.py`).

## Why the fees cover the selling

Up to any day, the tokens paid out are worth at most twice the fees collected. If miners keep half of what they
are paid (the holding factor rewards that), the fees cover everything ever paid; at a cap of 1× they would cover
it unconditionally. The buyback runs whenever the owner chooses, as one on-chain transaction each time.

## The skill factor, briefly

It asks two things of the last two months of daily returns, with deposits removed and each day capped at ±10%:
is the edge statistically real (probabilistic Sharpe ratio over 60 days), and is the recent month's profit much
larger than its losses (recency-weighted gain-to-pain over 30 days)? Luck scores about 0.05 to 0.10, a steady
trader 0.30 to 0.35, a strong one 0.55. No drawdown anywhere; one bad day costs about half the score for a month.
It moves pay by at most 5%.

## Faking a track record

Two accounts trading each other at off-market prices to move money into the scored one does not work: every fill
is compared with the exchange's own minute price, the money moved through an off-market price is treated like a
deposit, and the credit is valued at the market price.

## Leaving and coming back

Come back with the same hotkey and your records resume, but as a new registration you qualify again with three
trades. Moving to a new address, or swapping the hotkey, keeps your track record, your pay history and any
penalty, but your fee credit and your qualification start again. Come back as a new identity and you start from
zero, lose the unpaid part of your old credit, and a mirror report still follows your address. Collateral never counts as
holding, so a seller gains nothing by re-registering. Your Hyperliquid account history counts for skill from the
first day you are bound; trades of days scored before your binding never pay.

## Still open

A month of burn figures on testnet (directed vs destroyed); the holding effect measured on testnet; legal review.
