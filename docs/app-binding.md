# Binding a trader in the Kokoah application

What the application has to do so that a trader becomes a subnet miner. The subnet side is in the whitepaper
(appendix D) and `src/kokoah_subnet/binding/`; nothing on the app side touches Bittensor keys.

## The one line to sign

The trader's Hyperliquid wallet signs this exact text as an Ethereum personal message (EIP-191, `personal_sign`):

```
kokoah-bind-1 netuid=<netuid> hotkey=<hotkey ss58> address=<0x address, lowercase>
```

- `netuid`: the subnet's id on the network the trader joins (testnet 385 today; mainnet later).
- `hotkey`: the ss58 address of the trader's Bittensor hotkey, typed or pasted by the trader.
- `address`: the trader's Hyperliquid address, lowercase; the wallet that signs must be this address.

The result is a 65-byte signature (0x-hex, 132 characters).

## What the app shows the trader

1. A field for the hotkey ss58 address and a "Sign" button that calls `personal_sign` with the line above.
2. The signature, and the one command that publishes it from the trader's own hotkey (the trader runs it where
   their Bittensor wallet lives; it needs no password for a hotkey):

```
kokoah-subnet miner bind --network <test|finney> --netuid <netuid> --address <0x...> --signature <0x...> \
    --wallet <wallet> --wallet-hotkey <hotkey name>
```

The command checks the signature against the line before publishing, so a wrong hotkey or address fails locally.

3. Optionally, a status line read from any validator's interface: `GET /bindings` lists accepted bindings;
   `GET /leaderboard` shows the trader's rank and pay once a day has been scored.

## Migration

To move to a new Hyperliquid address (once per 60 days; the track record, pay history and skill move with it, the
fee credit and the qualification start again): the new address signs the line (with the new
address in it), and the old address signs the same line. Both signatures go on the command as `--signature` and
`--previous-address`/`--previous-signature`.

## What the app must not do

Never ask for, store or forward a Bittensor coldkey or hotkey, a mnemonic or a password. The app only produces the
Ethereum signature; publishing it is the trader's hotkey transaction.
