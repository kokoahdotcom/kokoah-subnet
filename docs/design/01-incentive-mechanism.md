# Incentive mechanism in plain words (v5.7, 2026-10-20)

The formulas are in `02-mechanism-spec.md`; this page says what they do. The public, polished version is the
whitepaper (`docs/whitepaper.md`).

## What the subnet is

Miners are traders. A miner links one Hyperliquid address to a subnet hotkey and trades perpetuals through Kokoah,
so every scored trade carries Kokoah's builder code and pays Kokoah a small fee (0.025% of the trade). Validators
read the public Hyperliquid data of the linked addresses and share the day's miner emission among them. Kokoah
uses the fees to buy back and burn the subnet's token. Miners run nothing: register, link, trade.

## How a miner's daily pay is decided

1. **Count the fees the miner paid through Kokoah today.** Every fill counts the same, maker or taker, because the
   fee is the same on every order. Mix today with the past: 30% today, 70% the running figure. Every dollar of fees
   is counted exactly once over time.
2. **Multiply by a small quality factor between 0.85 and 1.** Skill adds up to 0.05 and holding (not selling the
   tokens you were paid) up to 0.10. Two miners with the same volume are always within 15% of each other.
3. **Share the day's pool in proportion.** Twice the volume, about twice the pay.
4. **Cap each miner's pay.** Nobody is paid more than twice the Kokoah fee they paid, and nobody is paid more than
   three quarters of everything the trade cost them (Hyperliquid's fee plus Kokoah's). The cap is converted to
   tokens at the day's average price, so a price dip at the scoring moment changes nothing.
5. **Burn what is left.** When the pool is bigger than the sum of the caps (a young subnet), the rest is burned in
   the chain-visible way, and the chain reduces the subnet's TAO inflow by the burned share. The subnet does not
   draw TAO for emission it did not award.

## Why cheating does not pay

Every fake-volume strategy (wash trading, a hedge on another venue or in a second account, instant round trips,
dust accounts, lottery farms) pays Hyperliquid and Kokoah more than the subnet can ever pay back, because the pay
is capped at three quarters of what the trade cost. The tightest case is a top-tier market maker who earns rebates
on both legs: still a loss of half a basis point per round trip, and it would need a rebate almost twice
Hyperliquid's best to break even. Splitting into many accounts or merging accounts changes nothing, because shares
and caps are linear in fees. Linked addresses that mirror each other lose their credit and, above half, their
quality factor. Verified by `scripts/verify_mechanism.py`.

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

## Still open

The burn cost measured on the localnet and testnet; the holding effect measured on testnet; legal review.
