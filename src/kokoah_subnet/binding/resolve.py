"""Which binding counts for each hotkey (specification section 14), as a pure function over what the chain shows and
what the validator accepted before.

Rules: a valid first binding is accepted; the same address again changes nothing; a migration is accepted only when
the previous address is the one currently accepted, its signature checks out, and the last accepted change is at
least `migration_blocks` old; anything else keeps the earlier binding and is flagged. One address can be bound to
one hotkey: a later claim on an address already accepted elsewhere is refused.
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
) -> Resolution:
    accepted: dict[str, AcceptedBinding] = dict(previous)
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
            if b.previous_address is not None:
                refused[hk] = "migration without an accepted binding"
                continue
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
