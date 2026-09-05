"""Password hashing for dashboard login (PBKDF2-SHA256).

The five team members have fixed default passwords. The dashboard exchanges
name + password for the same bearer token the MCP connector already uses.
"""

from __future__ import annotations

import hashlib
import secrets

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 210_000
_SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8

# Fixed roster. Password is the lowercase name + 1234 (Mark → mark1234).
DEFAULT_DASHBOARD_USERS = ("Mark", "Manoj", "Brent", "Akash", "Kole")
DEFAULT_DASHBOARD_PASSWORDS = {
    name: f"{name.lower()}1234" for name in DEFAULT_DASHBOARD_USERS
}


def default_password_for(name: str) -> str | None:
    needle = name.strip().lower()
    for user, password in DEFAULT_DASHBOARD_PASSWORDS.items():
        if user.lower() == needle:
            return password
    return None


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
