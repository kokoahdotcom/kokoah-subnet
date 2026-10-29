"""The binding message and its Ethereum signature.

A miner proves control of a Hyperliquid address by signing, with that address's key (EIP-191 personal message),
a short text that names the subnet, the hotkey and the address. The hotkey then publishes the address and the
signature as its on-chain commitment; the hotkey's own signature is the extrinsic itself. Validators read every
commitment, recover the signer, and accept the binding only when the recovered address is the named one.

A migration (spec section 14) is a new commitment that also carries the old address's signature over the same
message, so the records can follow the hotkey to the new address.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_defunct

BINDING_VERSION = "kokoah-bind-1"


def binding_message(netuid: int, hotkey: str, address: str) -> str:
    """The exact text the Hyperliquid address signs. One line, no ambiguity."""
    return f"{BINDING_VERSION} netuid={netuid} hotkey={hotkey} address={address.lower()}"


def sign_binding(private_key: str | bytes, netuid: int, hotkey: str, address: str) -> str:
    """Sign the binding message with the address's key (for tests and for a miner's own tooling; the app signs
    the same text with the user's wallet). Returns the 65-byte signature as 0x-hex."""
    msg = encode_defunct(text=binding_message(netuid, hotkey, address))
    signed = Account.sign_message(msg, private_key=private_key)
    return "0x" + bytes(signed.signature).hex()


def recover_signer(netuid: int, hotkey: str, address: str, signature: str) -> str:
    """The lowercase address that signed the binding message with `signature`; raises on a malformed signature."""
    msg = encode_defunct(text=binding_message(netuid, hotkey, address))
    return str(Account.recover_message(msg, signature=bytes.fromhex(signature.removeprefix("0x")))).lower()


@dataclass(frozen=True)
class AgentRef:
    """An agent account's sealed machine (spec v6, section 16): the code fingerprint the miner commits to, and
    where validators reach the machine to challenge it."""

    code_sha256: str  # 64 hex characters
    host: str  # IP address or host name, at most 51 bytes
    port: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "code_sha256", self.code_sha256.lower())
        if len(self.code_sha256) != 64:
            raise ValueError("the code fingerprint must be 64 hex characters")
        if not 0 < self.port < 65536 or not self.host or len(self.host.encode()) > 51:
            raise ValueError("bad agent endpoint")

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


@dataclass(frozen=True)
class Binding:
    """What a hotkey publishes: the address it trades from, and proof it controls it."""

    address: str  # lowercase 0x address
    signature: str  # 0x-hex EIP-191 signature of `binding_message` by `address`
    previous_address: str | None = None  # on a migration: the address the records come from
    previous_signature: str | None = None  # its signature over the same message (same netuid, hotkey, new address)
    agent: AgentRef | None = None  # set when the address is a sealed-machine agent account

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", self.address.lower())
        if (self.previous_address is None) != (self.previous_signature is None):
            raise ValueError("a migration needs both the previous address and its signature")
        if self.previous_address is not None:
            object.__setattr__(self, "previous_address", self.previous_address.lower())


FIELD_CHARS = 116  # base64 of 87 bytes: 2-byte tag + 20-byte address + 65-byte signature
_TAG_CURRENT = b"K1"
_TAG_PREVIOUS = b"P1"
_TAG_AGENT = b"A1"


def _pack(tag: bytes, address: str, signature: str) -> str:
    raw = tag + bytes.fromhex(address.removeprefix("0x")) + bytes.fromhex(signature.removeprefix("0x"))
    if len(raw) != 87:
        raise ValueError("address must be 20 bytes and the signature 65 bytes")
    return base64.b64encode(raw).decode("ascii")


def _unpack(chunk: str) -> tuple[bytes, str, str]:
    raw = base64.b64decode(chunk, validate=True)
    if len(raw) != 87:
        raise ValueError("bad binding field")
    return raw[:2], "0x" + raw[2:22].hex(), "0x" + raw[22:].hex()


def _pack_agent(a: AgentRef) -> str:
    host = a.host.encode()
    raw = _TAG_AGENT + bytes.fromhex(a.code_sha256) + a.port.to_bytes(2, "big") + host + b"\0" * (51 - len(host))
    return base64.b64encode(raw).decode("ascii")


def _unpack_agent(chunk: str) -> AgentRef:
    raw = base64.b64decode(chunk, validate=True)
    if len(raw) != 87 or raw[:2] != _TAG_AGENT:
        raise ValueError("bad agent field")
    host = raw[36:].rstrip(b"\0").decode()
    return AgentRef(raw[2:34].hex(), host, int.from_bytes(raw[34:36], "big"))


def encode_fields(b: Binding) -> list[str]:
    """The commitment's raw fields: one 116-character text per address (each fits the chain's 128-byte Raw field).
    Field 1 is the current address, field 2 (migration only) the previous one."""
    fields = [_pack(_TAG_CURRENT, b.address, b.signature)]
    if b.previous_address and b.previous_signature:
        fields.append(_pack(_TAG_PREVIOUS, b.previous_address, b.previous_signature))
    if b.agent is not None:
        fields.append(_pack_agent(b.agent))
    return fields


def encode_binding(b: Binding) -> str:
    """The fields joined, which is exactly how the chain reads a multi-field commitment back."""
    return "".join(encode_fields(b))


def decode_binding(text: str) -> Binding | None:
    """Parse a commitment's data (the concatenated raw fields); None when it is not a binding of this version."""
    if not text or len(text) % FIELD_CHARS != 0:
        return None
    pieces = [text[i : i + FIELD_CHARS] for i in range(0, len(text), FIELD_CHARS)]
    agent: AgentRef | None = None
    try:
        if base64.b64decode(pieces[-1], validate=True)[:2] == _TAG_AGENT:
            agent = _unpack_agent(pieces.pop())
        chunks = [_unpack(p) for p in pieces]
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not chunks or chunks[0][0] != _TAG_CURRENT:
        return None
    address, signature = chunks[0][1], chunks[0][2]
    previous: tuple[str, str] | None = None
    for tag, a, s in chunks[1:]:
        if tag == _TAG_PREVIOUS and previous is None:
            previous = (a, s)
        else:
            return None
    if previous is None:
        return Binding(address, signature, agent=agent)
    return Binding(address, signature, previous[0], previous[1], agent=agent)


def verify_binding(netuid: int, hotkey: str, b: Binding) -> bool:
    """True when the address signed the message for this hotkey and netuid, and, on a migration, the previous
    address signed it too."""
    try:
        if recover_signer(netuid, hotkey, b.address, b.signature) != b.address:
            return False
        if b.previous_address is not None:
            if b.previous_signature is None:
                return False
            if recover_signer(netuid, hotkey, b.address, b.previous_signature) != b.previous_address:
                return False
        return True
    except Exception:  # noqa: BLE001 - anyone can publish a commitment: a signature that cannot be read is not valid
        return False
