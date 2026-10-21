"""Which binding counts for each hotkey (specification section 14), as a pure function over what the chain shows and
what the validator accepted before.

Rules: a valid first binding is accepted (a migration seen without history counts as a first binding: both
signatures are on the commitment); the same address again changes nothing; a migration of an accepted binding is
accepted only when the previous address is the one currently accepted, its signature checks out, and the last
accepted change is at least `migration_days` old; anything else keeps the earlier binding and is flagged. One
address can be bound to one hotkey: a later claim on an address already accepted elsewhere is refused. A validator
that starts fresh therefore agrees with the others except on the 60-day limit during its own first 60 days.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .chain import PublishedBinding

BLOCKS_PER_DAY = 7200


@dataclass(frozen=True)
class AcceptedBinding:
    hotkey: str
    address: str
    accepted_block: int  # when this address was accepted (a migration resets it)
    first_block: int  # when the hotkey's first binding was accepted


@dataclass(frozen=True)
class Resolution:
    accepted: dict[str, AcceptedBinding]  # by hotkey
    refused: dict[str, str]  # hotkey -> reason, for the published report


def resolve_bindings(
    previous: Mapping[str, AcceptedBinding],
    published: Iterable[PublishedBinding],
    block: int,
    migration_days: int = 60,
    registered: Iterable[str] | None = None,
) -> Resolution:
    """`registered`, when given, is the set of hotkeys on the subnet today: a hotkey that left releases its address
    (its records stay in the store and resume if the same hotkey comes back)."""
    accepted: dict[str, AcceptedBinding] = dict(previous)
    if registered is not None:
        live = set(registered)
        accepted = {hk: b for hk, b in accepted.items() if hk in live}
    refused: dict[str, str] = {}
    taken = {a.address: hk for hk, a in accepted.items()}
    min_gap = migration_days * BLOCKS_PER_DAY
    for pb in sorted(published, key=lambda p: (p.block, p.hotkey)):
        hk, b = pb.hotkey, pb.binding
        if not pb.valid:
            refused[hk] = "signature does not match"
            continue
        current = accepted.get(hk)
        if current is None:
            # No history for this hotkey (a first binding, or a validator that started after a migration): the
            # commitment's own signatures are the whole proof, so a valid one is accepted as it stands.
            if b.address in taken and taken[b.address] != hk:
                refused[hk] = "address already bound to another hotkey"
                continue
            accepted[hk] = AcceptedBinding(hk, b.address, pb.block, pb.block)
            taken[b.address] = hk
            continue
        if b.address == current.address:
            continue  # unchanged
        if b.previous_address != current.address:
            refused[hk] = "migration must be signed by the accepted address"
            continue
        if block - current.accepted_block < min_gap:
            refused[hk] = "migration allowed once per 60 days"
            continue
        if b.address in taken and taken[b.address] != hk:
            refused[hk] = "address already bound to another hotkey"
            continue
        del taken[current.address]
        accepted[hk] = AcceptedBinding(hk, b.address, pb.block, current.first_block)
        taken[b.address] = hk
    return Resolution(accepted, refused)
