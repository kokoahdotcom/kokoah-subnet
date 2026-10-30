"""The account key's encrypted copy: filed only by a genuine sealed machine with its hotkey owner's start permission,
restored only to a genuine machine, released only to the master address, and a released account never counts as an
agent again. Only hotkeys registered on the subnet submit code or file copies; restoring and releasing never depend
on the registration."""

import asyncio
import base64
import hashlib
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
from uploads import upload

from kokoah_subnet.agent import service as service_module
from kokoah_subnet.agent import supervisor as supervisor_module
from kokoah_subnet.agent.attest import KEYSTORE_AUDIENCE, DevelopmentAttestor, StaticKeys, verify_token
from kokoah_subnet.agent.bundle import package, sha256_hex
from kokoah_subnet.agent.harness import Harness, HarnessConfig
from kokoah_subnet.agent.keystore import (
    KeyService,
    checked_address,
    decrypt_key,
    encrypt_key,
    parse_permit,
    release_message,
    restore_digest,
    start_message,
    store_digest,
)
from kokoah_subnet.agent.service import create_app, submit_message
from kokoah_subnet.agent.supervisor import account_key, address_setting, mainnet_setting
from kokoah_subnet.binding import AgentRef, Binding
from kokoah_subnet.chain import RegisteredHotkeys
from kokoah_subnet.validator.agents import check_agents

IMAGE = "sha256:" + "ee" * 32
SECRET = bytes(range(32))
NETUID = 2
ALICE = Keypair.create_from_uri("//Alice")
BOB = Keypair.create_from_uri("//Bob")


def submit(svc: TestClient, hotkey: Any, tmp_path: Path) -> httpx.Response:
    d = tmp_path / f"agent-{hotkey.ss58_address[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "entry.py").write_text("print('agent')\n")
    blob = package(d)
    sig = bytes(hotkey.sign(submit_message(sha256_hex(blob), hotkey.ss58_address).encode())).hex()
    response: httpx.Response = upload(svc, hotkey.ss58_address, blob, sig)
    return response


def permit(submission: str, master: str, hotkey: Any = ALICE, netuid: int = NETUID, t: int | None = None) -> str:
    t = int(time.time() * 1000) if t is None else t
    return f"{t}:{bytes(hotkey.sign(start_message(netuid, hotkey.ss58_address, master, submission, t).encode())).hex()}"


def make_world(tmp_path: Path, **options: Any) -> dict[str, Any]:
    machine_key = options.pop("machine_key", None) or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app = create_app(
        tmp_path / "svc",
        images=[IMAGE],
        attestation_keys=StaticKeys(machine_key.public_key()),
        netuid=NETUID,
        **options,
    )
    svc = TestClient(app)
    first = submit(svc, ALICE, tmp_path)
    return {
        "svc": svc,
        "keys": KeyService("http://svc", svc),
        "machine": DevelopmentAttestor(machine_key, IMAGE),
        "machine_key": machine_key,
        "submission": first.json()["id"] if first.status_code == 200 else "",
    }


def start(world: dict[str, Any], master: Any, restore: str | None = None, **kw: Any) -> bytes:
    """One machine start: a new account (with a fresh start permission) or a restore."""
    hotkey = kw.pop("hotkey", ALICE)
    given = kw.pop("permit", None)
    machine = kw.pop("machine", world["machine"])
    keys = kw.pop("keys", world["keys"])
    p = None if restore else (given or permit(world["submission"], master.address, hotkey))
    return account_key(
        keys, machine, SECRET, master.address, hotkey.ss58_address, restore, NETUID, world["submission"], p
    )


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    return make_world(tmp_path)


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
    key = start(world, master)
    address = Account.from_key(key).address.lower()
    # a new genuine machine with the same secret gets the same account back (the bonus continues)
    assert start(world, master, address) == key
    # a machine without a genuine attestation is refused, for restoring and for storing
    stranger = DevelopmentAttestor(rsa.generate_private_key(public_exponent=65537, key_size=2048), IMAGE)
    with pytest.raises(httpx.HTTPStatusError) as refused:
        start(world, master, address, machine=stranger)
    assert refused.value.response.status_code == 403
    with pytest.raises(httpx.HTTPStatusError):
        start(world, master, machine=stranger)
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
        start(world, master, address)
    assert gone.value.response.status_code == 410


def test_a_restore_needs_a_fresh_challenge(world: dict[str, Any]) -> None:
    keys: KeyService = world["keys"]
    key = start(world, Account.create())
    address = Account.from_key(key).address.lower()
    replayed = world["machine"].token(["00" * 32], KEYSTORE_AUDIENCE)
    r = world["svc"].post(f"/key-store/{address}/restore", json={"token": replayed})
    assert r.status_code == 403  # it answers no current challenge
    keys.challenge(address)
    r = world["svc"].post(f"/key-store/{address}/restore", json={"token": replayed})
    assert r.status_code == 403  # the token does not answer the challenge


def test_a_key_copy_needs_its_hotkey_owners_start_permission(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    master = Account.create()
    sub = world["submission"]

    def refused(**kw: Any) -> int:
        with pytest.raises(httpx.HTTPStatusError) as e:
            start(world, master, **kw)
        return e.value.response.status_code

    with pytest.raises(SystemExit, match="KOKOAH_AGENT_PERMIT"):  # the harness itself asks for one
        account_key(world["keys"], world["machine"], SECRET, master.address, ALICE.ss58_address, None, NETUID, sub)
    forged = f"{int(time.time() * 1000)}:{permit(sub, master.address, BOB).split(':')[1]}"  # Bob signs for Alice
    assert refused(permit=forged) == 403
    assert refused(permit=permit(sub, master.address, netuid=NETUID + 1)) == 403  # for another subnet
    assert refused(permit=permit(sub, Account.create().address)) == 403  # for another withdrawal address
    assert refused(permit=permit(sub, master.address, t=int((time.time() - 8 * 86400) * 1000))) == 403  # expired
    assert refused(permit=permit("ab" * 32, master.address)) == 403  # signed, but for code that was never submitted
    assert refused(permit="not-a-permission") == 400
    good = permit(sub, master.address)
    start(world, master, permit=good)
    assert refused(permit=good) == 409  # one machine start per permission
    monkeypatch.setattr(service_module, "MAX_COPIES_PER_HOTKEY", 2)
    start(world, master)
    assert refused() == 429  # a hotkey's kept copies are capped


def test_a_used_start_permission_stays_used_in_every_spelling(world: dict[str, Any]) -> None:
    """The signature check reads the hex in either case and with spaces: a used permission spelled another way files
    no second copy."""
    master = Account.create()
    good = permit(world["submission"], master.address)
    t, signature = good.split(":")
    start(world, master, permit=good)
    spaced = " ".join(signature[i : i + 2] for i in range(0, len(signature), 2))
    for respelled in (f"{t}:{signature.upper()}", f"{t}:0x{signature}", f"{t}:{spaced}"):
        with pytest.raises(httpx.HTTPStatusError) as e:
            start(world, master, permit=respelled)
        assert e.value.response.status_code == 409
    assert parse_permit(f"{t}:0x{signature.upper()}") == (int(t), signature)
    with pytest.raises(ValueError):
        parse_permit(f"{t}:not-hex")


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
    registered = {ALICE.ss58_address}
    chain_down = False

    def is_registered(hotkey: str) -> bool:
        if chain_down:
            raise ConnectionError("no chain")
        return hotkey in registered

    w = make_world(tmp_path, is_registered=is_registered)
    refused = submit(w["svc"], BOB, tmp_path)  # signed by its hotkey, but not registered
    assert refused.status_code == 403 and "not registered" in refused.json()["detail"]
    master = Account.create()
    with pytest.raises(httpx.HTTPStatusError) as unregistered:
        start(w, master, hotkey=BOB)  # Bob's own permission, but Bob is not registered
    assert unregistered.value.response.status_code == 403
    key = start(w, master)
    address = Account.from_key(key).address.lower()
    registered.clear()  # alice loses her registration: her account still comes back, and out
    assert start(w, master, address) == key
    t = int(time.time() * 1000)
    signed = master.sign_message(encode_defunct(text=release_message(address, t)))
    assert decrypt_key(w["keys"].release(address, t, "0x" + bytes(signed.signature).hex()), address, SECRET) == key
    chain_down = True
    assert submit(w["svc"], ALICE, tmp_path / "again").status_code == 503


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

    wrap = bytes(range(100, 132))
    w = make_world(tmp_path, wrap_key=wrap)

    def service(key: bytes | None) -> KeyService:
        app = create_app(
            tmp_path / "svc",
            images=[IMAGE],
            attestation_keys=StaticKeys(w["machine_key"].public_key()),
            wrap_key=key,
            netuid=NETUID,
        )
        return KeyService("http://svc", TestClient(app))

    master = Account.create()
    key = start(w, master)
    address = Account.from_key(key).address.lower()
    with sqlite3.connect(tmp_path / "svc" / "agents.sqlite") as db:
        (stored,) = db.execute("select blob from agent_keys where address = ?", (address,)).fetchone()
    raw = base64.b64decode(stored)
    assert raw.startswith(b"KSW1")
    with pytest.raises(ValueError):
        decrypt_key(raw, address, SECRET)  # the database copy is not the machine's copy: the service's key is needed
    assert start(w, master, address, keys=service(wrap)) == key
    # a service started with another key file opens nothing and records nothing
    with pytest.raises(httpx.HTTPStatusError) as wrong:
        start(w, master, address, keys=service(bytes(32)))
    assert wrong.value.response.status_code == 500
    t = int(time.time() * 1000)
    signed = master.sign_message(encode_defunct(text=release_message(address, t)))
    with pytest.raises(httpx.HTTPStatusError):
        service(bytes(32)).release(address, t, "0x" + bytes(signed.signature).hex())
    assert service(wrap).released() == []
    blob = service(wrap).release(address, t, "0x" + bytes(signed.signature).hex())
    assert decrypt_key(blob, address, SECRET) == key


def test_made_up_requests_leave_nothing_behind_and_touch_no_real_copy(world: dict[str, Any]) -> None:
    svc: TestClient = world["svc"]
    master = Account.create()
    key = start(world, master)
    address = Account.from_key(key).address.lower()
    stranger = "0x" + "12" * 20
    assert svc.get(f"/key-store/{stranger}/challenge").status_code == 404  # no copy kept: no challenge
    p = permit(world["submission"], master.address)
    fake = {
        "blob": base64.b64encode(b"KAK1" + bytes(60)).decode(),
        "master": master.address,
        "hotkey": ALICE.ss58_address,
        "netuid": NETUID,
        "submission": world["submission"],
        "permit": p,
    }
    assert svc.put(f"/key-store/{stranger}", json={**fake, "token": "made-up"}).status_code == 403
    assert svc.put(f"/key-store/{stranger}", json={**fake, "blob": "A" * 4096, "token": "x"}).status_code == 413
    # even a genuine machine with a valid permission cannot replace a kept copy
    other = base64.b64decode(fake["blob"])
    token = world["machine"].token(
        [store_digest(address, master.address, other, ALICE.ss58_address, fake["submission"], p)], KEYSTORE_AUDIENCE
    )
    assert svc.put(f"/key-store/{address}", json={**fake, "token": token}).status_code == 409
    assert start(world, master, address) == key


def test_strangers_cannot_stop_a_machine_from_restoring(world: dict[str, Any]) -> None:
    """Between the machine's challenge and its restore, a stranger asks for challenges and sends junk restores: the
    machine's restore still works. A machine's token works once."""
    keys: KeyService = world["keys"]
    svc: TestClient = world["svc"]
    master = Account.create()
    key = start(world, master)
    address = Account.from_key(key).address.lower()
    challenge = keys.challenge(address)
    token = world["machine"].token([restore_digest(address, challenge)], KEYSTORE_AUDIENCE)
    for _ in range(5):
        svc.get(f"/key-store/{address}/challenge")
        svc.post(f"/key-store/{address}/restore", json={"token": "junk"})
    blob = svc.post(f"/key-store/{address}/restore", json={"token": token})
    assert blob.status_code == 200
    assert decrypt_key(base64.b64decode(blob.json()["blob"]), address, SECRET) == key
    assert svc.post(f"/key-store/{address}/restore", json={"token": token}).status_code == 403  # used once


def test_a_restore_token_works_once_in_every_spelling(world: dict[str, Any]) -> None:
    """A token's signature reads the same from other spellings (the unused low bits of its last character, padding,
    characters the decoder skips), and each passes the token check; none of them restores the key a second time."""
    svc: TestClient = world["svc"]
    key = start(world, Account.create())
    address = Account.from_key(key).address.lower()
    digest = restore_digest(address, world["keys"].challenge(address))
    token = world["machine"].token([digest], KEYSTORE_AUDIENCE)
    head, signature = token.rsplit(".", 1)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(signature[-1])
    respelled = [
        f"{head}.{signature[:-1]}{alphabet[last ^ 1]}",  # the last character's low bits carry nothing
        f"{head}.{signature[:-1]}{alphabet[last ^ 4]}",
        f"{head}.{signature}==",
        f"{head}.{signature[:10]}\n\n{signature[10:]}==",  # skipped by the decoder
    ]
    machine_keys = StaticKeys(world["machine_key"].public_key())
    for t in respelled:
        assert verify_token(t, machine_keys, [IMAGE], [digest], audience=KEYSTORE_AUDIENCE)
    assert svc.post(f"/key-store/{address}/restore", json={"token": token}).status_code == 200
    for t in respelled:
        again = svc.post(f"/key-store/{address}/restore", json={"token": t})
        assert again.status_code == 403 and "used already" in again.json()["detail"]
    assert service_module.used_token(token) == "token " + hashlib.sha256(token.encode()).hexdigest()  # as before


def test_a_machines_public_attestation_never_stores_or_restores_a_key(world: dict[str, Any]) -> None:
    """A genuine machine answers anyone's challenge at `/attestation`. Asked for a store or restore digest, its answer
    (two nonces) is still refused by the key store: it never files a key made by hand as a machine's own, and never
    hands a copy to someone relaying that answer."""
    svc: TestClient = world["svc"]
    master = Account.create()
    key = start(world, master)
    address = Account.from_key(key).address.lower()
    config = HarnessConfig(NETUID, ALICE.ss58_address, master.address, "11" * 32, "0x" + "43" * 20)
    machine = Harness(config, Account.create().key, world["machine"])  # the same genuine machine key and image
    by_hand = Account.create()
    blob = encrypt_key(bytes(by_hand.key), by_hand.address, SECRET)
    p = permit(world["submission"], master.address)
    digest = store_digest(by_hand.address, master.address, blob, ALICE.ss58_address, world["submission"], p)
    body = {
        "blob": base64.b64encode(blob).decode(),
        "master": master.address,
        "hotkey": ALICE.ss58_address,
        "netuid": NETUID,
        "submission": world["submission"],
        "permit": p,
        "token": machine.attestation(digest)["token"],
    }
    refused = svc.put(f"/key-store/{by_hand.address}", json=body)
    assert refused.status_code == 403
    assert any(why in refused.json()["detail"].lower() for why in ("audience", "other nonces"))
    challenge = world["keys"].challenge(address)
    relayed = machine.attestation(restore_digest(address, challenge))["token"]
    assert svc.post(f"/key-store/{address}/restore", json={"token": relayed}).status_code == 403
    assert start(world, master, address) == key  # the machine's own restore still works


def test_a_restore_in_progress_survives_a_service_restart(tmp_path: Path) -> None:
    """With the service's own key, a challenge given before a restart is still answered after it, and a token that
    restored a key stays used."""
    wrap = bytes(range(50, 82))
    w = make_world(tmp_path, wrap_key=wrap)
    master = Account.create()
    key = start(w, master)
    address = Account.from_key(key).address.lower()
    challenge = w["keys"].challenge(address)
    token = w["machine"].token([restore_digest(address, challenge)], KEYSTORE_AUDIENCE)

    def restarted() -> TestClient:
        keys = StaticKeys(w["machine_key"].public_key())
        app = create_app(tmp_path / "svc", images=[IMAGE], attestation_keys=keys, wrap_key=wrap, netuid=NETUID)
        return TestClient(app)

    after = restarted()
    assert after.get(f"/key-store/{address}/challenge").json()["challenge"] == challenge
    blob = after.post(f"/key-store/{address}/restore", json={"token": token})
    assert blob.status_code == 200 and decrypt_key(base64.b64decode(blob.json()["blob"]), address, SECRET) == key
    again = restarted().post(f"/key-store/{address}/restore", json={"token": token})
    assert again.status_code == 403 and "used already" in again.json()["detail"]


def test_the_machine_takes_only_real_addresses_and_a_known_network(world: dict[str, Any]) -> None:
    from eth_utils.address import to_checksum_address

    plain = Account.create().address.lower()
    assert checked_address(plain) == plain and checked_address(to_checksum_address(plain)) == plain
    assert checked_address("0x" + plain[2:].upper()) == plain
    assert checked_address("0X" + plain[2:]) == plain  # an upper-case prefix is still the same address
    typo = to_checksum_address(plain).swapcase().replace("X", "x")  # every letter's case flipped
    for wrong in (typo, plain[:-1], plain[2:], "0x" + "g" * 40, ""):
        with pytest.raises(ValueError):
            checked_address(wrong)
    with pytest.raises(SystemExit, match="KOKOAH_AGENT_MASTER"):
        address_setting("KOKOAH_AGENT_MASTER", plain[:-1])
    assert mainnet_setting("mainnet") and not mainnet_setting("testnet")
    for wrong in ("test", "Testnet", "finney", ""):
        with pytest.raises(SystemExit, match="mainnet or testnet"):
            mainnet_setting(wrong)
    p = permit(world["submission"], "nope")  # signed by the hotkey, but for something that is not an address
    body: dict[str, Any] = {"blob": base64.b64encode(b"KAK1" + bytes(60)).decode(), "master": "nope", "token": "x"}
    body |= {"hotkey": ALICE.ss58_address, "netuid": NETUID, "submission": world["submission"], "permit": p}
    assert world["svc"].put(f"/key-store/{plain}", json=body).status_code == 400


class Flaky:
    """The service as a machine reaches it, with the faults listed per method: "down" (no connection), "lost" (the
    service acts but its answer never arrives) or a status code answered by a gateway in between."""

    def __init__(self, client: TestClient, faults: dict[str, list[Any]]) -> None:
        self.client, self.faults = client, faults
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append(method)
        waiting = self.faults.get(method)
        fault = waiting.pop(0) if waiting else None
        if fault == "down":
            raise httpx.ConnectError("no route")
        if fault == "lost":
            self.client.request(method, url, **kw)
            raise httpx.ReadError("connection reset")
        if isinstance(fault, int):
            return httpx.Response(fault, request=httpx.Request(method, url))
        answer: httpx.Response = self.client.request(method, url, **kw)
        return answer

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def put(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("PUT", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)


def test_a_starting_machine_gets_past_a_passing_fault(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import cast

    from kokoah_subnet.agent.supervisor import fetch_code

    monkeypatch.setattr(supervisor_module, "START_PAUSE_S", 0.0)

    def keys(**faults: list[Any]) -> tuple[KeyService, Flaky]:
        flaky = Flaky(world["svc"], faults)
        return KeyService("http://svc", cast(httpx.Client, flaky)), flaky

    master = Account.create()
    down_once, _ = keys(PUT=["down"])
    key = start(world, master, keys=down_once)
    address = Account.from_key(key).address.lower()
    lost, flaky = keys(PUT=["lost"])  # filed, but the answer was lost: the next try finds the copy kept
    other = Account.from_key(start(world, master, keys=lost)).address.lower()
    assert flaky.calls == ["PUT", "PUT", "GET"] and world["keys"].kept(other)
    busy, flaky = keys(GET=[503], POST=[502])
    assert start(world, master, address, keys=busy) == key
    refused, flaky = keys(PUT=[403])
    with pytest.raises(httpx.HTTPStatusError):
        start(world, master, keys=refused)
    assert flaky.calls == ["PUT"]  # a refusal is final
    always_down, flaky = keys(GET=["down"] * 9)
    with pytest.raises(httpx.ConnectError):
        start(world, master, address, keys=always_down)
    assert flaky.calls == ["GET"] * supervisor_module.START_TRIES
    bundle = Flaky(world["svc"], {"GET": [504]})
    plain, _ = fetch_code("http://svc", world["submission"], None, cast(httpx.Client, bundle))
    assert plain[:2] == b"PK" and bundle.calls == ["GET", "GET"]


def test_a_harness_1_1_0_machine_keeps_its_key_copy_and_its_public_answer_still_files_nothing(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Harness 1.1.0 asks for its key-store tokens with the default audience: from that image only, its own store and
    restore are accepted (exactly the one digest), while its public answer to the same digest is refused (two nonces).
    Another image's default-audience token is refused."""
    from kokoah_subnet.agent import images

    svc: TestClient = world["svc"]
    master, account = Account.create(), Account.create()
    blob = encrypt_key(bytes(account.key), account.address, SECRET)
    p = permit(world["submission"], master.address)
    digest = store_digest(account.address, master.address, blob, ALICE.ss58_address, world["submission"], p)
    body = {
        "blob": base64.b64encode(blob).decode(),
        "master": master.address,
        "hotkey": ALICE.ss58_address,
        "netuid": NETUID,
        "submission": world["submission"],
        "permit": p,
    }
    own = world["machine"].token([digest])
    refused = svc.put(f"/key-store/{account.address}", json={**body, "token": own})
    assert refused.status_code == 403 and "only from harness 1.1.0" in refused.json()["detail"]  # not 1.1.0
    unapproved = DevelopmentAttestor(world["machine_key"], "sha256:" + "0d" * 32).token([digest])
    refused = svc.put(f"/key-store/{account.address}", json={**body, "token": unapproved})
    assert refused.status_code == 403 and "not an approved harness" in refused.json()["detail"]
    monkeypatch.setattr(images, "DEFAULT_AUDIENCE_KEYSTORE_IMAGES", (IMAGE,))  # the test image stands in for 1.1.0
    public = world["machine"].token([digest, "ab" * 32])  # how /attestation answers: the challenge and the binding
    assert svc.put(f"/key-store/{account.address}", json={**body, "token": public}).status_code == 403
    stored = svc.put(f"/key-store/{account.address}", json={**body, "token": own})
    assert stored.status_code == 200, stored.text
    address = account.address.lower()
    challenge = world["keys"].challenge(address)
    old_style = world["machine"].token([restore_digest(address, challenge)])
    restored = svc.post(f"/key-store/{address}/restore", json={"token": old_style})
    assert restored.status_code == 200
    assert decrypt_key(base64.b64decode(restored.json()["blob"]), address, SECRET) == bytes(account.key)
