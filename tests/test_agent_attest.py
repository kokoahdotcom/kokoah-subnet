"""A validator's checks on a sealed machine's token: signature, production build, approved image, fresh nonces."""

from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kokoah_subnet.agent.attest import (
    AttestationError,
    DevelopmentAttestor,
    StaticKeys,
    binding_digest,
    verify_token,
)

IMAGE = "sha256:" + "ab" * 32


@pytest.fixture(scope="module")
def keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_a_genuine_token_passes(keypair: rsa.RSAPrivateKey) -> None:
    bind = binding_digest(2, "hk", "0xAbC", "11" * 32)
    challenge = "cd" * 32
    token = DevelopmentAttestor(keypair, IMAGE).token([challenge, bind])
    v = verify_token(token, StaticKeys(keypair.public_key()), [IMAGE], [challenge, bind])
    assert v.image_digest == IMAGE and v.hardware == "GCP_INTEL_TDX"


def test_each_check_refuses(keypair: rsa.RSAPrivateKey) -> None:
    good = DevelopmentAttestor(keypair, IMAGE)
    keys = StaticKeys(keypair.public_key())
    bind = binding_digest(2, "hk", "0xabc", "11" * 32)
    token = good.token(["cd" * 32, bind])
    with pytest.raises(AttestationError, match="approved harness"):
        verify_token(token, keys, ["sha256:" + "00" * 32], ["cd" * 32, bind])
    with pytest.raises(AttestationError, match="nonces"):
        verify_token(token, keys, [IMAGE], ["ef" * 32, bind])  # a stale answer to another challenge
    other_bind = binding_digest(2, "hk", "0xdef", "11" * 32)  # a different account
    with pytest.raises(AttestationError, match="nonces"):
        verify_token(token, keys, [IMAGE], ["cd" * 32, other_bind])
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AttestationError, match="signature"):
        verify_token(token, StaticKeys(stranger.public_key()), [IMAGE], ["cd" * 32, bind])


def test_debug_machines_are_refused(keypair: rsa.RSAPrivateKey) -> None:
    claims = jwt.decode(DevelopmentAttestor(keypair, IMAGE).token(["cd" * 32]), options={"verify_signature": False})
    for change, message in (({"dbgstat": "enabled"}, "debug"), ({"hwmodel": "GCP_SHIELDED_VM"}, "hardware")):
        token = jwt.encode({**claims, **change}, keypair, algorithm="RS256")
        with pytest.raises(AttestationError, match=message):
            verify_token(token, StaticKeys(keypair.public_key()), [IMAGE], ["cd" * 32])


def test_the_binding_digest_names_everything() -> None:
    base = binding_digest(2, "hk", "0xabc", "11" * 32)
    assert base == binding_digest(2, "hk", "0xABC", "11" * 32)  # address case does not matter
    for other in (
        binding_digest(3, "hk", "0xabc", "11" * 32),
        binding_digest(2, "hk2", "0xabc", "11" * 32),
        binding_digest(2, "hk", "0xabd", "11" * 32),
        binding_digest(2, "hk", "0xabc", "12" * 32),
    ):
        assert other != base


def test_tokens_naming_made_up_keys_cost_at_most_one_key_read_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import jwt as pyjwt

    from kokoah_subnet.agent import attest

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    real = pyjwt.PyJWK.from_dict({**pyjwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True),
                                  "kid": "google-1", "alg": "RS256"})  # fmt: skip
    reads: list[float] = []
    now = [1000.0]
    monkeypatch.setattr("kokoah_subnet.agent.attest.time.monotonic", lambda: now[0])

    def fetch() -> list[Any]:
        reads.append(now[0])
        return [real]

    keys = attest.PublishedKeys(fetch, refresh_s=300)

    def token(kid: str) -> str:
        return pyjwt.encode({"x": 1}, rsa_key, algorithm="RS256", headers={"kid": kid})

    assert keys.get_signing_key_from_jwt(token("google-1")) is real and len(reads) == 1
    for i in range(50):  # made-up keys: refused, and the set is not read again within the window
        with pytest.raises(pyjwt.PyJWKClientError):
            keys.get_signing_key_from_jwt(token(f"made-up-{i}"))
    assert len(reads) == 1
    now[0] += 301  # a new key of Google's is found once the window has passed
    with pytest.raises(pyjwt.PyJWKClientError):
        keys.get_signing_key_from_jwt(token("google-2"))
    assert len(reads) == 2 and keys.get_signing_key_from_jwt(token("google-1")) is real
