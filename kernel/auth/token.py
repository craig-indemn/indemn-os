"""Service token management.

Long-lived opaque tokens for associates, Tier 3 API keys, and CLI automation.
Tokens are hashed before storage (never stored in plaintext).
Validated by hash lookup against the actor's authentication_methods.
"""

import secrets

from kernel.auth.password import hash_password, verify_password


def generate_service_token() -> str:
    """Generate a cryptographically secure opaque token.
    The raw token is returned to the caller ONCE and never stored.
    Only the hash is stored."""
    return f"indemn_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    """Hash a token for storage. Uses the same Argon2id as passwords."""
    return hash_password(token)


def verify_token(token: str, token_hash: str) -> bool:
    """Verify a token against its stored hash."""
    return verify_password(token, token_hash)


async def authenticate_by_token(token: str):
    """Authenticate an actor by service token.
    Looks up actors with token-type auth methods and verifies.
    Returns the matching Actor or None."""
    from kernel_entities.actor import Actor

    # Find actors with token auth methods
    actors = await Actor.find(
        {
            "status": "active",
            "authentication_methods": {"$elemMatch": {"type": "token"}},
        }
    ).to_list()

    for actor in actors:
        for method in actor.authentication_methods:
            if method.get("type") == "token":
                if verify_token(token, method.get("token_hash", "")):
                    return actor
    return None
