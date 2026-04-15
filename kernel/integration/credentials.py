"""Credential management — AWS Secrets Manager with TTL caching.

Credentials are stored in Secrets Manager and cached in-process
with a 5-minute TTL. Cache is invalidated on store/rotate.
"""

import logging
import time

import boto3
import orjson

from kernel.config import settings

logger = logging.getLogger(__name__)

_secrets_client = None
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # 5 minutes


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        kwargs = {"region_name": settings.aws_region}
        if settings.aws_access_key_id:
            kwargs["aws_access_key_id"] = settings.aws_access_key_id
            kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
        _secrets_client = boto3.client("secretsmanager", **kwargs)
    return _secrets_client


async def fetch_credentials(secret_ref: str) -> dict:
    """Fetch credentials from Secrets Manager with TTL caching."""
    now = time.time()
    if secret_ref in _cache:
        creds, cached_at = _cache[secret_ref]
        if now - cached_at < CACHE_TTL:
            return creds

    client = _get_secrets_client()
    response = client.get_secret_value(SecretId=secret_ref)
    creds = orjson.loads(response["SecretString"])
    _cache[secret_ref] = (creds, now)
    return creds


async def store_credentials(secret_ref: str, credentials: dict):
    """Store credentials in Secrets Manager. Creates if not exists."""
    client = _get_secrets_client()
    secret_string = orjson.dumps(credentials).decode()
    try:
        client.update_secret(SecretId=secret_ref, SecretString=secret_string)
    except client.exceptions.ResourceNotFoundException:
        client.create_secret(Name=secret_ref, SecretString=secret_string)
    # Invalidate cache [G-28]
    _cache.pop(secret_ref, None)


def invalidate_cached_credentials(secret_ref: str):
    """Invalidate cached credentials (e.g., after rotation)."""
    _cache.pop(secret_ref, None)
