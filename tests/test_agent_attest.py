"""A validator's checks on a sealed machine's token: signature, production build, approved image, fresh nonces."""

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
