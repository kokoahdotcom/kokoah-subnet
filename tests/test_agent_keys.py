"""The account key's encrypted copy: stored only by a genuine sealed machine, restored only to one, released only to
the master address, and a released account never counts as an agent again. Only hotkeys registered on the subnet
submit code or store keys; restoring and releasing never depend on the registration."""

import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from bittensor.keyfiles import Keypair
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import rsa
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from kokoah_subnet.agent.attest import DevelopmentAttestor, StaticKeys
from kokoah_subnet.agent.bundle import package, sha256_hex
from kokoah_subnet.agent.keystore import KeyService, decrypt_key, encrypt_key, release_message
from kokoah_subnet.agent.service import create_app, submit_message
from kokoah_subnet.agent.supervisor import account_key
from kokoah_subnet.binding import AgentRef, Binding
from kokoah_subnet.chain import RegisteredHotkeys
from kokoah_subnet.validator.agents import check_agents

IMAGE = "sha256:" + "ee" * 32
SECRET = bytes(range(32))


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    machine_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    svc = TestClient(
        create_app(tmp_path / "svc", images=[IMAGE], attestation_keys=StaticKeys(machine_key.public_key()))
    )
    return {"svc": svc, "keys": KeyService("http://svc", svc), "machine": DevelopmentAttestor(machine_key, IMAGE)}


def test_the_copy_needs_the_miners_secret_and_names_its_account() -> None:
    acct = Account.create()
    blob = encrypt_key(bytes(acct.key), acct.address, SECRET)
    assert bytes(acct.key) not in blob and decrypt_key(blob, acct.address, SECRET) == bytes(acct.key)
    with pytest.raises(InvalidTag):
        decrypt_key(blob, acct.address, bytes(32))
    with pytest.raises(InvalidTag):
        decrypt_key(blob, Account.create().address, SECRET)


def test_store_restore_and_release(world: dict[str, Any]) -> None:
    keys: KeyService = world["keys"]
    master = Account.create()
    key = account_key(keys, world["machine"], SECRET, master.address, "5Hk", None)
    address = Account.from_key(key).address.lower()
    # a new genuine machine with the same secret gets the same account back (the bonus continues)
    assert account_key(keys, world["machine"], SECRET, master.address, "5Hk", address) == key
    # a machine without a genuine attestation is refused, for restoring and for storing
    stranger = DevelopmentAttestor(rsa.generate_private_key(public_exponent=65537, key_size=2048), IMAGE)
    with pytest.raises(httpx.HTTPStatusError) as refused:
        account_key(keys, stranger, SECRET, master.address, "5Hk", address)
    assert refused.value.response.status_code == 403
    with pytest.raises(httpx.HTTPStatusError):
        account_key(keys, stranger, SECRET, master.address, "5Hk", None)
    # the miner takes the key out: signed by the master, public, final
    t = int(time.time() * 1000)
    bad = Account.create().sign_message(encode_defunct(text=release_message(address, t)))
    with pytest.raises(httpx.HTTPStatusError) as wrong_signer:
        keys.release(address, t, "0x" + bytes(bad.signature).hex())
    assert wrong_signer.value.response.status_code == 403
    good = master.sign_message(encode_defunct(text=release_message(address, t)))
    assert decrypt_key(keys.release(address, t, "0x" + bytes(good.signature).hex()), address, SECRET) == key
    assert [r["address"] for r in keys.released()] == [address]
    with pytest.raises(httpx.HTTPStatusError) as gone:
        account_key(keys, world["machine"], SECRET, master.address, "5Hk", address)
    assert gone.value.response.status_code == 410


def test_a_restore_needs_a_fresh_challenge(world: dict[str, Any]) -> None:
    keys: KeyService = world["keys"]
    key = account_key(keys, world["machine"], SECRET, Account.create().address, "5Hk", None)
    address = Account.from_key(key).address.lower()
    replayed = world["machine"].token(["00" * 32])
    r = world["svc"].post(f"/key-store/{address}/restore", json={"token": replayed})
    assert r.status_code == 409  # no challenge asked
    keys.challenge(address)
    r = world["svc"].post(f"/key-store/{address}/restore", json={"token": replayed})
    assert r.status_code == 403  # the token does not answer the challenge


def test_validators_never_count_a_released_account() -> None:
    acct = Account.create()
    b = Binding(acct.address, "0x" + "00" * 65, agent=AgentRef("ab" * 32, "203.0.113.5", 8080))

    async def run(released: set[str] | None, service: str = "") -> Any:
        async with httpx.AsyncClient() as http:
            return await check_agents({"hk": b}, 2, StaticKeys(None), [IMAGE], http, None, service, released)

    out = asyncio.run(run({acct.address.lower()}))
    assert len(out) == 1 and not out[0].ok and "released" in str(out[0].reason)
    none = asyncio.run(run(None))
    assert not none[0].ok and "released list" in str(none[0].reason)


def test_only_registered_hotkeys_submit_code_or_store_keys(tmp_path: Path) -> None:
    machine_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alice, bob = Keypair.create_from_uri("//Alice"), Keypair.create_from_uri("//Bob")
    registered = {alice.ss58_address}
    chain_down = False

    def is_registered(hotkey: str) -> bool:
        if chain_down:
            raise ConnectionError("no chain")
        return hotkey in registered

    svc = TestClient(
        create_app(
            tmp_path / "svc",
            images=[IMAGE],
            attestation_keys=StaticKeys(machine_key.public_key()),
            is_registered=is_registered,
        )
    )
    keys, machine = KeyService("http://svc", svc), DevelopmentAttestor(machine_key, IMAGE)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "entry.py").write_text("print('agent')\n")
    blob = package(tmp_path / "agent")

    def submit(hotkey: Any) -> httpx.Response:
        sig = bytes(hotkey.sign(submit_message(sha256_hex(blob), hotkey.ss58_address).encode())).hex()
        body = {"hotkey": hotkey.ss58_address, "bundle": base64.b64encode(blob).decode(), "signature": sig}
        response: httpx.Response = svc.post("/agents", json=body)
        return response

    refused = submit(bob)  # signed by its hotkey, but not registered
    assert refused.status_code == 403 and "not registered" in refused.json()["detail"]
    assert submit(alice).status_code == 200
    master = Account.create()
    with pytest.raises(httpx.HTTPStatusError) as unregistered:
        account_key(keys, machine, SECRET, master.address, bob.ss58_address, None)
    assert unregistered.value.response.status_code == 403
    key = account_key(keys, machine, SECRET, master.address, alice.ss58_address, None)
    address = Account.from_key(key).address.lower()
    registered.clear()  # alice loses her registration: her account still comes back, and out
    assert account_key(keys, machine, SECRET, master.address, alice.ss58_address, address) == key
    t = int(time.time() * 1000)
    signed = master.sign_message(encode_defunct(text=release_message(address, t)))
    assert decrypt_key(keys.release(address, t, "0x" + bytes(signed.signature).hex()), address, SECRET) == key
    chain_down = True
    assert submit(alice).status_code == 503


def test_the_registered_list_is_read_again_when_old_or_on_an_unknown_hotkey() -> None:
    reads: list[set[str]] = [{"a"}, {"a", "b"}, {"b"}]
    calls = []
    now = [0.0]

    async def fetch() -> set[str]:
        calls.append(now[0])
        if not reads:
            raise ConnectionError("no chain")
        return reads.pop(0)

    hk = RegisteredHotkeys(fetch, max_age_s=600, refresh_s=30, clock=lambda: now[0])
    assert hk.check("a") and not hk.check("b") and len(calls) == 1  # unknown, but just read
    now[0] = 40
    assert hk.check("b") and len(calls) == 2  # unknown and older than 30 s: read again
    now[0] = 700
    assert not hk.check("a") and len(calls) == 3  # older than 600 s: read again, "a" left
    now[0] = 800
    assert not hk.check("c") and hk.check("b") and len(calls) == 4  # the chain is down: the last list stands
    now[0] = 700 + 6 * 600 + 1
    with pytest.raises(ConnectionError):
        hk.check("b")  # too old to stand on


def test_the_service_adds_its_own_layer_and_a_stolen_database_alone_opens_nothing(tmp_path: Path) -> None:
    import sqlite3

    machine_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrap = bytes(range(100, 132))

    def service(key: bytes | None) -> KeyService:
        app = create_app(
            tmp_path / "svc", images=[IMAGE], attestation_keys=StaticKeys(machine_key.public_key()), wrap_key=key
        )
        return KeyService("http://svc", TestClient(app))

    machine, master = DevelopmentAttestor(machine_key, IMAGE), Account.create()
    key = account_key(service(wrap), machine, SECRET, master.address, "5Hk", None)
    address = Account.from_key(key).address.lower()
    with sqlite3.connect(tmp_path / "svc" / "agents.sqlite") as db:
        (stored,) = db.execute("select blob from agent_keys where address = ?", (address,)).fetchone()
    raw = base64.b64decode(stored)
    assert raw.startswith(b"KSW1")
    with pytest.raises(ValueError):
        decrypt_key(raw, address, SECRET)  # the database copy is not the machine's copy: the service's key is needed
    assert account_key(service(wrap), machine, SECRET, master.address, "5Hk", address) == key
    # a service started with another key file opens nothing and records nothing
    with pytest.raises(httpx.HTTPStatusError) as wrong:
        account_key(service(bytes(32)), machine, SECRET, master.address, "5Hk", address)
    assert wrong.value.response.status_code == 500
    t = int(time.time() * 1000)
    signed = master.sign_message(encode_defunct(text=release_message(address, t)))
    with pytest.raises(httpx.HTTPStatusError):
        service(bytes(32)).release(address, t, "0x" + bytes(signed.signature).hex())
    assert service(wrap).released() == []
    blob = service(wrap).release(address, t, "0x" + bytes(signed.signature).hex())
    assert decrypt_key(blob, address, SECRET) == key
