"""Unlinking (specification section 14): the owner of a linked Hyperliquid address ends the link with one signature
from that address, without the hotkey.

The address signs (EIP-191 personal message)

    kokoah-unlink-1 netuid=<netuid> hotkey=<ss58> address=<0x lowercase> time=<unix ms>

and sends it to the subnet's service, which publishes every unlink it accepted (`GET /unlinks`). Validators check
each signature themselves, so the service cannot make one up. An unlink takes effect from the first UTC day after
its time: from that day the hotkey no longer earns from the address, the address is free to be linked by another
hotkey (and the hotkey to link another address), and this hotkey and this address are never linked again (the old
link signature cannot simply be republished).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct

UNLINK_VERSION = "kokoah-unlink-1"


def unlink_message(netuid: int, hotkey: str, address: str, time_ms: int) -> str:
    """The exact text the address signs."""
    return f"{UNLINK_VERSION} netuid={netuid} hotkey={hotkey} address={address.lower()} time={time_ms}"


def sign_unlink(private_key: str | bytes, netuid: int, hotkey: str, address: str, time_ms: int) -> str:
    """Sign the unlink with the address's key (tests and a miner's own tooling; the app signs with the wallet)."""
    signed = Account.sign_message(encode_defunct(text=unlink_message(netuid, hotkey, address, time_ms)), private_key)
    return "0x" + bytes(signed.signature).hex()


def effective_day(time_ms: int) -> str:
    """The first UTC day after the signature's time: the day the unlink takes effect."""
    return (datetime.fromtimestamp(time_ms / 1000, UTC).date() + timedelta(days=1)).isoformat()


@dataclass(frozen=True)
class Unlink:
    hotkey: str
    address: str
    time_ms: int
    signature: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", self.address.lower())

    @property
    def effective_day(self) -> str:
        return effective_day(self.time_ms)

    def signed_by_address(self, netuid: int) -> bool:
        """Whether the address itself signed this unlink, for this subnet and hotkey."""
        try:
            signer = Account.recover_message(
                encode_defunct(text=unlink_message(netuid, self.hotkey, self.address, self.time_ms)),
                signature=bytes.fromhex(self.signature.removeprefix("0x")),
            )
        except Exception:  # noqa: BLE001 - any signature that cannot be read was not signed by the address
            return False
        return str(signer).lower() == self.address
