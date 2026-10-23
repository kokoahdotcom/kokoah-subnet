"""Shared test settings: the documentation address ranges (RFC 5737, RFC 3849) stand in for public machines."""

import ipaddress

import pytest

from kokoah_subnet.validator import agents

DOCUMENTATION = [
    ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
]


@pytest.fixture(autouse=True)
def documentation_addresses_count_as_public(monkeypatch: pytest.MonkeyPatch) -> None:
    real = agents.address_allowed

    def allowed(ip: str) -> bool:
        address = ipaddress.ip_address(ip)
        return any(address in net for net in DOCUMENTATION) or real(ip)

    monkeypatch.setattr(agents, "address_allowed", allowed)
