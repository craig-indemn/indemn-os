"""Adapter dispatch — resolve, instantiate, auto-refresh.

get_adapter() is the primary entry point for all adapter usage.
Handles credential resolution, caching, and OAuth token refresh.
execute_with_retry() wraps adapter methods with auth error retry.
"""

import logging

from kernel.integration.adapter import Adapter, AdapterAuthError
from kernel.integration.credentials import fetch_credentials, store_credentials
from kernel.integration.registry import get_adapter_class
from kernel.integration.resolver import resolve_integration

logger = logging.getLogger(__name__)


async def get_adapter(
    system_type: str,
    actor_id=None,
    org_id=None,
    require_org_only: bool = False,
) -> Adapter:
    """Resolve integration, fetch credentials, instantiate adapter.

    Handles OAuth token refresh transparently. [G-26]
    """
    integration = await resolve_integration(system_type, actor_id, org_id, require_org_only)
    credentials = await fetch_credentials(integration.secret_ref)
    adapter_cls = get_adapter_class(integration.provider, integration.provider_version)
    adapter = adapter_cls(config=integration.config, credentials=credentials)

    # Store secret_ref on adapter for retry logic
    adapter._secret_ref = integration.secret_ref

    # Check if token needs refresh (OAuth adapters)
    if adapter.needs_token_refresh():
        try:
            new_credentials = await adapter.refresh_token()
            await store_credentials(integration.secret_ref, new_credentials)
            adapter = adapter_cls(config=integration.config, credentials=new_credentials)
            adapter._secret_ref = integration.secret_ref
        except Exception as e:
            logger.warning("Token refresh failed for %s: %s", integration.name, e)

    return adapter


async def execute_with_retry(adapter: Adapter, method_name: str, *args, **kwargs):
    """Execute an adapter method with automatic retry on errors.

    AdapterAuthError: refresh token and retry once.
    AdapterRateLimitError: backoff using retry_after and retry once.
    AdapterTimeoutError: retry once immediately.
    """
    import asyncio

    from kernel.integration.adapter import AdapterRateLimitError, AdapterTimeoutError

    method = getattr(adapter, method_name)
    try:
        return await method(*args, **kwargs)
    except AdapterAuthError:
        # Refresh token and retry
        if hasattr(adapter, "refresh_token"):
            try:
                new_creds = await adapter.refresh_token()
                await store_credentials(adapter._secret_ref, new_creds)
                adapter.credentials = new_creds
                return await method(*args, **kwargs)
            except Exception:
                raise
        raise
    except AdapterRateLimitError as e:
        # Backoff using retry_after, then retry once
        wait = e.retry_after or 60
        logger.warning("Rate limited, waiting %d seconds before retry", wait)
        await asyncio.sleep(wait)
        return await method(*args, **kwargs)
    except AdapterTimeoutError:
        # Retry once on timeout
        logger.warning("Timeout on %s, retrying once", method_name)
        return await method(*args, **kwargs)
