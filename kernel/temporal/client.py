"""Temporal client factory."""

from temporalio.client import Client

from kernel.config import settings

_client: Client = None


async def get_temporal_client() -> Client:
    """Get or create the Temporal client.

    Returns None if temporal_address is not configured or connection fails.
    """
    global _client
    if _client is None:
        if not settings.temporal_address:
            return None
        connect_kwargs = {
            "target_host": settings.temporal_address,
            "namespace": settings.temporal_namespace,
        }
        if settings.temporal_api_key:
            connect_kwargs["api_key"] = settings.temporal_api_key
            connect_kwargs["tls"] = True  # Temporal Cloud requires TLS
        _client = await Client.connect(**connect_kwargs)
    return _client
