"""The account key's two wrapping layers, and the Cloud KMS flow (against fake Google endpoints)."""

import base64
import json
from typing import Any

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from eth_account import Account

from kokoah_subnet.agent.keystore import DevelopmentSealer, GcpKmsSealer, unwrap, wrap

SECRET = bytes(range(32))


def test_both_layers_are_needed() -> None:
    acct = Account.create()
    addr = acct.address.lower()
    blob = wrap(bytes(acct.key), addr, SECRET, DevelopmentSealer())
    assert bytes(acct.key) not in blob
    assert unwrap(blob, addr, SECRET, DevelopmentSealer()) == bytes(acct.key)
    with pytest.raises(InvalidTag):
        unwrap(blob, addr, bytes(32), DevelopmentSealer())  # the miner's secret is needed
    with pytest.raises(InvalidTag):
        unwrap(blob, Account.create().address.lower(), SECRET, DevelopmentSealer())  # bound to the address
    with pytest.raises(ValueError, match="32 bytes"):
        wrap(bytes(acct.key), addr, b"short", DevelopmentSealer())


class FakeAttestor:
    def __init__(self) -> None:
        self.audiences: list[str | None] = []

    def token(self, nonces: list[str], audience: str | None = None) -> str:
        self.audiences.append(audience)
        return "attestation-token"


def test_kms_layer_uses_an_attestation_exchanged_access_token() -> None:
    seen: list[tuple[str, dict[str, Any], str]] = []

    def google(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((str(request.url), body, request.headers.get("authorization", "")))
        if request.url.host == "sts.googleapis.com":
            assert body["subjectToken"] == "attestation-token" and body["audience"].startswith("//iam.googleapis.com/")
            return httpx.Response(200, json={"access_token": "federated"})
        if str(request.url).endswith(":encrypt"):
            return httpx.Response(
                200, json={"ciphertext": base64.b64encode(b"K" + base64.b64decode(body["plaintext"])).decode()}
            )
        return httpx.Response(
            200, json={"plaintext": base64.b64encode(base64.b64decode(body["ciphertext"])[1:]).decode()}
        )

    att = FakeAttestor()
    sealer = GcpKmsSealer(
        att,
        "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/q",
        "projects/x/locations/global/keyRings/r/cryptoKeys/k",
        http=httpx.Client(transport=httpx.MockTransport(google)),
    )
    sealed = sealer.encrypt(b"secret")
    assert sealed == b"Ksecret" and sealer.decrypt(sealed) == b"secret"
    kms_calls = [s for s in seen if "cloudkms" in s[0]]
    assert all(auth == "Bearer federated" for _, _, auth in kms_calls) and len(kms_calls) == 2
    assert att.audiences == ["https://sts.googleapis.com"] * 2
