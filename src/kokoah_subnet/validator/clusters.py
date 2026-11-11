"""Address clusters (specification section 3): addresses one owner holds are scored as one and never trade against
each other for credit. Clusters come only from links the owner alone can create: the chain (hotkeys on one coldkey)
and Hyperliquid (a master's sub-accounts); union-find turns them into cluster ids. A transfer between two addresses
(anyone can send one) links only the two clusters that made it, for their trades with each other, and never joins a
third through them."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def clusters(addresses: Iterable[str], edges: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Map every address (and every address named by an edge) to its cluster id: the smallest address in it."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        parent[rb] = ra

    for a in addresses:
        find(a.lower())
    for a, b in edges:
        union(a.lower(), b.lower())
    return {x: find(x) for x in list(parent)}


def coldkey_edges(address_of_hotkey: Mapping[str, str], coldkey_of_hotkey: Mapping[str, str]) -> list[tuple[str, str]]:
    """Bound addresses whose hotkeys share a coldkey belong together."""
    by_coldkey: dict[str, list[str]] = {}
    for hk, addr in address_of_hotkey.items():
        ck = coldkey_of_hotkey.get(hk)
        if ck:
            by_coldkey.setdefault(ck, []).append(addr.lower())
    edges: list[tuple[str, str]] = []
    for addrs in by_coldkey.values():
        edges.extend((addrs[0], other) for other in addrs[1:])
    return edges


OWNER_LINKS = ("coldkey", "subaccount")  # links only the owner can create; anyone can send a transfer


def penalty_reach(reported: str, edges: Iterable[tuple[str, str, str]]) -> set[str]:
    """The addresses a mirror report reaches: the reported address and those tied to it by links only its owner can
    create (hotkeys on one coldkey, a master and its sub-accounts). Transfers do not carry a report, because anyone
    can send a transfer to anyone."""
    owned = [(a, b) for a, b, kind in edges if kind in OWNER_LINKS]
    of = clusters([reported], owned)
    root = of[reported.lower()]
    return {a for a, c in of.items() if c == root}


def transfer_links(cluster_of: Mapping[str, str], edges: Iterable[tuple[str, str, str]]) -> frozenset[tuple[str, str]]:
    """The pairs of clusters (ids in order) that a transfer between an address of each links. A pair, never a chain:
    anyone can send a transfer, so an address that sends to two others links itself to each of them, never the two to
    each other."""
    pairs: set[tuple[str, str]] = set()
    for a, b, kind in edges:
        if kind == "transfer":
            ca, cb = cluster_of.get(a.lower(), a.lower()), cluster_of.get(b.lower(), b.lower())
            if ca != cb:
                pairs.add((min(ca, cb), max(ca, cb)))
    return frozenset(pairs)
