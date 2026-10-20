"""Binding: message, signature, commitment encoding, and the resolution rules."""

from dataclasses import dataclass

from eth_account import Account

from kokoah_subnet.binding import (
    AcceptedBinding,
    Binding,
    PublishedBinding,
    binding_message,
    decode_binding,
    encode_binding,
    encode_fields,
    recover_signer,
    resolve_bindings,
    sign_binding,
    verify_binding,
)
from kokoah_subnet.validator.clusters import clusters, coldkey_edges

HK = "5GmqHTsV9627ugQFVDz3WdJgWYpXKtNXuHZ38ZmtEZP1Di6Q"
HK2 = "5DxukvpL6km37GqUu2bUKo4amLqNebCAmp9aDtiUW5yZCC1s"


def test_message_is_one_exact_line() -> None:
    assert binding_message(2, HK, "0xABC") == f"kokoah-bind-1 netuid=2 hotkey={HK} address=0xabc"


def test_sign_recover_and_verify() -> None:
    acct = Account.create()
    sig = sign_binding(acct.key, 2, HK, acct.address)
    assert recover_signer(2, HK, acct.address, sig) == acct.address.lower()
    b = Binding(acct.address, sig)
    assert verify_binding(2, HK, b)
    assert not verify_binding(3, HK, b)  # another subnet
    assert not verify_binding(2, HK2, b)  # another hotkey
    assert not verify_binding(2, HK, Binding(acct.address, "0x" + "11" * 65))


def test_commitment_fields_fit_the_chain_and_round_trip() -> None:
    acct, old = Account.create(), Account.create()
    sig = sign_binding(acct.key, 2, HK, acct.address)
    fields = encode_fields(Binding(acct.address, sig))
    assert len(fields) == 1 and len(fields[0]) == 116 and fields[0].isascii()
    assert decode_binding(encode_binding(Binding(acct.address, sig))) == Binding(acct.address, sig)
    migration = Binding(acct.address, sig, old.address, sign_binding(old.key, 2, HK, acct.address))
    fields = encode_fields(migration)
    assert len(fields) == 2 and all(len(f) == 116 for f in fields)
    decoded = decode_binding("".join(fields))
    assert decoded == migration and verify_binding(2, HK, decoded)
    assert decode_binding("not a binding") is None
    assert decode_binding("A" * 116) is None


def _pub(hotkey: str, block: int, b: Binding, valid: bool = True) -> PublishedBinding:
    return PublishedBinding(hotkey, block, b, valid)


def test_resolution_rules() -> None:
    a, b2, c = Account.create(), Account.create(), Account.create()
    sig_a = sign_binding(a.key, 2, HK, a.address)
    first = resolve_bindings({}, [_pub(HK, 100, Binding(a.address, sig_a))], block=200)
    assert first.accepted[HK] == AcceptedBinding(HK, a.address.lower(), 100, 100) and not first.refused
    # another hotkey claiming the same address is refused
    dup = resolve_bindings(
        first.accepted, [_pub(HK2, 150, Binding(a.address, sign_binding(a.key, 2, HK2, a.address)))], 300
    )
    assert HK2 in dup.refused and HK2 not in dup.accepted
    # a migration without the accepted address's signature is refused
    bad = resolve_bindings(
        first.accepted, [_pub(HK, 500, Binding(b2.address, sign_binding(b2.key, 2, HK, b2.address)))], 600
    )
    assert bad.accepted[HK].address == a.address.lower() and "accepted address" in bad.refused[HK]
    # a proper migration too early is refused, later it is accepted and keeps the first block
    mig = Binding(
        b2.address, sign_binding(b2.key, 2, HK, b2.address), a.address, sign_binding(a.key, 2, HK, b2.address)
    )
    early = resolve_bindings(first.accepted, [_pub(HK, 500, mig)], block=1000)
    assert "60 days" in early.refused[HK]
    late = resolve_bindings(first.accepted, [_pub(HK, 500_000, mig)], block=500_000)
    assert late.accepted[HK] == AcceptedBinding(HK, b2.address.lower(), 500_000, 100)
    # after the migration the old address is free for another hotkey; an invalid signature is refused
    reuse = resolve_bindings(late.accepted, [_pub(HK2, 600_000, Binding(a.address, "0x00"), valid=False)], 600_000)
    assert reuse.refused[HK2] == "signature does not match"
    ok = resolve_bindings(
        late.accepted, [_pub(HK2, 600_000, Binding(c.address, sign_binding(c.key, 2, HK2, c.address)))], 600_000
    )
    assert ok.accepted[HK2].address == c.address.lower()


def test_clusters_union_find_and_coldkey_edges() -> None:
    edges = coldkey_edges({"h1": "0xA", "h2": "0xB", "h3": "0xC"}, {"h1": "ck1", "h2": "ck1", "h3": "ck2"})
    assert edges == [("0xa", "0xb")]
    cl = clusters(["0xA", "0xB", "0xC", "0xD"], edges + [("0xD", "0xE")])
    assert cl["0xa"] == cl["0xb"] and cl["0xc"] != cl["0xa"]
    assert cl["0xd"] == cl["0xe"] == "0xd"


@dataclass
class _Commitment:
    data: str
    block: int
    encrypted: bool = False


class _FakeSubnets:
    def __init__(self, items: dict[str, _Commitment]) -> None:
        self.items = items

    async def commitments(self, netuid: int) -> dict[str, _Commitment]:
        return self.items


class _FakeClient:
    def __init__(self, items: dict[str, _Commitment]) -> None:
        self.subnets = _FakeSubnets(items)


async def test_read_bindings_from_commitments() -> None:
    from kokoah_subnet.binding import read_bindings

    acct = Account.create()
    good = encode_binding(Binding(acct.address, sign_binding(acct.key, 2, HK, acct.address)))
    client = _FakeClient({HK: _Commitment(good, 10), HK2: _Commitment("hello", 11), "hk3": _Commitment(good, 12)})
    out = await read_bindings(client, 2)
    assert [(p.hotkey, p.valid) for p in out] == [(HK, True), ("hk3", False)]
