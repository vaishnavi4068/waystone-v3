"""Password hashing for dashboard login (PBKDF2-SHA256).

Plaintext passwords are never stored. Each member gets a unique password at seed
time (or an explicit one); the dashboard exchanges name + password for the same
bearer token the MCP connector already uses.
"""

from __future__ import annotations

import hashlib
import secrets

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 210_000
_SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("password must not be empty")
    raw_salt = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), raw_salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${raw_salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not password or not stored:
        return False
    try:
        algo, iters_s, salt_hex, digest_hex = stored.split("$")
        if algo != _ALGO:
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters_s)
        )
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(derived.hex(), digest_hex)


def generate_password() -> str:
    """High-entropy unique password suitable for handing to one team member."""
    return secrets.token_urlsafe(12)
