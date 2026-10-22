"""The sealed-machine agent (specification v6, section 16): the trusted harness that miners run in a cloud
confidential VM, the sandbox their agent code runs in, attestation, and the owner's submission service.

The harness is the only code here that holds a Hyperliquid key and writes to the exchange: it creates the
account's key inside the confidential VM, never exports it, signs the agent's orders with Kokoah's builder code,
and withdraws only to the miner's own address. The rest of this repository only reads public data.
"""
