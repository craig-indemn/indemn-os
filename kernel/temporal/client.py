"""Temporal client factory.

Phase 1: stub. Phase 2: full Temporal Cloud connection.
"""

from kernel.config import settings

_client = None


async def get_temporal_client():
    """Get or create the Temporal client.

    Phase 1: returns None (Temporal not used yet).
    Phase 2: connects to Temporal Cloud.
    """
    global _client
    if _client is None:
        if not settings.temporal_address:
            return None
        try:
            from temporalio.client import Client

            connect_kwargs = {
                "target_host": settings.temporal_address,
                "namespace": settings.temporal_namespace,
            }
            if settings.temporal_api_key:
                connect_kwargs["api_key"] = settings.temporal_api_key
                connect_kwargs["tls"] = True
            _client = await Client.connect(**connect_kwargs)
        except Exception:
            return None
    return _client
