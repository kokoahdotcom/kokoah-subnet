"""The btauth/1 signed-request format, checked against the SDK's normative test vectors.

Validators and miners on Bittensor 11 talk over plain HTTP with hotkey-signed headers; this is the identity layer
the subnet's transport will build on. The fixtures are the golden vectors from the signed-requests guide.
"""

import hashlib

import bittensor as bt
import pytest
from bittensor.keyfiles import Keypair

BODY = b'{"prompt": "hello"}'
NONCE_NS = 1752076800000000000
BOB_SR25519 = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
ALICE_SR25519 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
ALICE_ED25519 = "5FA9nQDVg267DEd8m1ZypXLBnvN7SFxYwV7ndqSYGiN9TTpu"
ALICE_ED25519_SIGNATURE = (
    "0x1a6b631988bf1f7ea093ab9d5acf61f05c316d0b0e428b24b487b75a5f6ee0e1"
    "a56a26918fdf871b6cf09437e6588023a4ce423cf622621b2a7feee17612f704"
)


def test_body_hash_matches_the_vector() -> None:
    assert hashlib.sha256(BODY).hexdigest() == "341c57448e531310fbbe83f44cea2a5e838bd9e8a6b82b269f01d0dbbc23c3cc"


def test_payload_is_the_eight_documented_lines() -> None:
    payload = bt.http_auth.build_payload(
        scheme="sr25519",
        method="POST",
        path="/generate?stream=false",
        body=BODY,
        nonce_ns=NONCE_NS,
        sender_ss58=ALICE_SR25519,
        receiver_ss58=BOB_SR25519,
    )
    assert payload == (
        b"btauth/1\nsr25519\nPOST\n/generate?stream=false\n"
        b"341c57448e531310fbbe83f44cea2a5e838bd9e8a6b82b269f01d0dbbc23c3cc\n"
        b"1752076800000000000\n" + ALICE_SR25519.encode() + b"\n" + BOB_SR25519.encode()
    )


def test_pinned_ed25519_signature_verifies() -> None:
    headers = {
        "X-Bittensor-Version": "1",
        "X-Bittensor-Hotkey": ALICE_ED25519,
        "X-Bittensor-Receiver": BOB_SR25519,
        "X-Bittensor-Nonce": str(NONCE_NS),
        "X-Bittensor-Crypto": "ed25519",
        "X-Bittensor-Signature": ALICE_ED25519_SIGNATURE,
    }
    caller = bt.http_auth.verify(
        headers,
        BODY,
        method="POST",
        path="/generate?stream=false",
        self_hotkey_ss58=BOB_SR25519,
        now_ns=NONCE_NS + 1_000_000_000,
    )
    assert caller.hotkey_ss58 == ALICE_ED25519


def _signed_by_alice_for_bob() -> tuple[Keypair, Keypair, dict[str, str]]:
    alice = Keypair.create_from_uri("//Alice")
    bob = Keypair.create_from_uri("//Bob")
    headers = bt.http_auth.sign(alice, method="POST", path="/score", body=BODY, receiver_ss58=bob.ss58_address)
    return alice, bob, headers


def test_sign_then_verify_round_trip_and_replay_refusal() -> None:
    alice, bob, headers = _signed_by_alice_for_bob()
    store = bt.http_auth.InMemoryNonceStore()
    caller = bt.http_auth.verify(
        headers, BODY, method="POST", path="/score", self_hotkey_ss58=bob.ss58_address, nonce_store=store
    )
    assert caller.hotkey_ss58 == alice.ss58_address
    with pytest.raises(bt.http_auth.ReplayedRequest):
        bt.http_auth.verify(
            headers, BODY, method="POST", path="/score", self_hotkey_ss58=bob.ss58_address, nonce_store=store
        )


def test_wrong_receiver_is_refused() -> None:
    alice, _bob, headers = _signed_by_alice_for_bob()
    with pytest.raises(bt.http_auth.WrongReceiver):
        bt.http_auth.verify(headers, BODY, method="POST", path="/score", self_hotkey_ss58=alice.ss58_address)


def test_tampered_body_is_refused() -> None:
    _alice, bob, headers = _signed_by_alice_for_bob()
    with pytest.raises(bt.http_auth.BadSignature):
        bt.http_auth.verify(headers, BODY + b" ", method="POST", path="/score", self_hotkey_ss58=bob.ss58_address)
