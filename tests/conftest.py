"""Shared test settings: the documentation address ranges (RFC 5737, RFC 3849) stand in for public machines."""

import ipaddress
import os
from collections.abc import Generator

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


# CI's sandbox job sets KOKOAH_SANDBOX_TESTS=required: there every test must run, so a skip fails, whatever skipped
# it; any other value is refused rather than quietly read as "not required"
SANDBOX_TESTS = os.environ.get("KOKOAH_SANDBOX_TESTS", "")


def pytest_configure(config: pytest.Config) -> None:
    if SANDBOX_TESTS not in ("", "required"):
        raise pytest.UsageError(f"KOKOAH_SANDBOX_TESTS is 'required' or unset, not {SANDBOX_TESTS!r}")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if SANDBOX_TESTS == "required" and report.skipped:
        report.outcome = "failed"
        report.longrepr = f"skipped where every test must run: {report.longrepr}"
    return report
