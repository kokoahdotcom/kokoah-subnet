"""The account key's encrypted copy: stored only by a genuine sealed machine, restored only to one, released only to
the master address, and a released account never counts as an agent again."""

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import rsa
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from kokoah_subnet.agent.attest import DevelopmentAttestor, StaticKeys
from kokoah_subnet.agent.keystore import KeyService, decrypt_key, encrypt_key, release_message
from kokoah_subnet.agent.service import create_app
from kokoah_subnet.agent.supervisor import account_key
from kokoah_subnet.binding import AgentRef, Binding
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
    r = world["svc"].post(f"/v1/keys/{address}/restore", json={"token": replayed})
    assert r.status_code == 409  # no challenge asked
    keys.challenge(address)
    r = world["svc"].post(f"/v1/keys/{address}/restore", json={"token": replayed})
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
