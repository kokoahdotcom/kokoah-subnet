"""The miner's secret for a sealed machine (spec v6, section 16): what encrypts the stored copy of the agent account's
key. Whoever has that copy and the secret can take the account's money, so the secret is either random or made
from a strong password with a slow, memory-hard derivation (scrypt, 128 MiB per try), salted with the miner's own
address so no two miners share a table of guesses. The password never leaves the miner's computer; only the
derived secret is given to the machine.
"""

from __future__ import annotations

import hashlib
import secrets

from zxcvbn import zxcvbn

SALT_PREFIX = "kokoah-agent-secret-1 master="
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1
SECRET_BYTES = 32
MIN_LENGTH = 16
MIN_GUESSES_LOG10 = 18.0  # zxcvbn's estimate: about 10^18 guesses, each costing a full scrypt run
CONTEXT_WORDS = ("kokoah", "bittensor", "hyperliquid", "subnet", "agent", "miner", "secret", "password", "wallet")


def password_problems(password: str, context: tuple[str, ...] = ()) -> list[str]:
    """Why the password is too weak (empty when it is strong enough). `context`: the miner's own words (hotkey,
    address, wallet name), which an attacker would try first."""
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"use at least {MIN_LENGTH} characters")
    result = zxcvbn(password[:128], user_inputs=[*CONTEXT_WORDS, *context])
    if float(result["guesses_log10"]) < MIN_GUESSES_LOG10:
        warning = str(result.get("feedback", {}).get("warning") or "").strip()
        problems.append(
            "too easy to guess"
            + (f" ({warning.rstrip('.')})" if warning else "")
            + "; five or more random words, or a long mix of letters, digits and symbols, works well"
        )
    return problems


def derive_secret(password: str, master: str) -> bytes:
    """The machine secret made from the password: the same password and address always give the same secret."""
    salt = (SALT_PREFIX + master.lower()).encode()
    return hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=256 * 2**20, dklen=SECRET_BYTES
    )


def random_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)
