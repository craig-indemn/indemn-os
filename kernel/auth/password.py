"""Argon2id password hashing.

Credentials never live in the database as plaintext. Passwords are hashed
with Argon2id (the recommended algorithm for password hashing).
"""

from argon2 import PasswordHasher, Type

# Argon2id with secure defaults
_hasher = PasswordHasher(
    time_cost=3,  # iterations
    memory_cost=65536,  # 64MB
    parallelism=4,
    hash_len=32,
    type=Type.ID,  # Argon2id
)


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _hasher.hash(password)


def verify_password(password: str, hash: str) -> bool:
    """Verify a password against an Argon2id hash."""
    try:
        return _hasher.verify(hash, password)
    except Exception:
        return False
