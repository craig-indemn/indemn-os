"""Adapter base class and error hierarchy.

Provider-specific adapters inherit Adapter. Each method is optional —
adapters implement only what their provider supports.
"""

from abc import ABC
from decimal import Decimal
from typing import Optional


class AdapterError(Exception):
    """Base adapter error."""

    pass


class AdapterAuthError(AdapterError):
    """Authentication failed — refresh credentials and retry."""

    pass


class AdapterRateLimitError(AdapterError):
    """Rate limited — backoff and retry."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class AdapterTimeoutError(AdapterError):
    """Operation timed out — retry with longer timeout."""

    pass


class AdapterNotFoundError(AdapterError):
    """Resource not found — don't retry."""

    pass


class AdapterValidationError(AdapterError):
    """Invalid request — don't retry."""

    pass


class Adapter(ABC):
    """Base adapter. Provider-specific adapters inherit this."""

    def __init__(self, config: dict, credentials: dict):
        self.config = config
        self.credentials = credentials

    # Outbound
    async def fetch(self, **params) -> list[dict]:
        raise NotImplementedError

    async def send(self, payload: dict) -> dict:
        raise NotImplementedError

    async def charge(self, amount: Decimal, currency: str = "usd", **params) -> dict:
        raise NotImplementedError

    # Inbound
    async def validate_webhook(self, headers: dict, body: bytes) -> bool:
        raise NotImplementedError

    async def parse_webhook(self, body: dict) -> dict:
        """Returns: {entity_type, lookup_by, lookup_value, operation, params}"""
        raise NotImplementedError

    # Auth
    async def auth_initiate(self, redirect_uri: str) -> str:
        raise NotImplementedError

    async def auth_callback(self, code: str, state: str) -> dict:
        raise NotImplementedError

    # OAuth token refresh [G-26]
    async def refresh_token(self) -> dict:
        """Refresh OAuth tokens. Returns new credentials to store."""
        raise NotImplementedError

    async def test(self) -> dict:
        """Test connectivity with a minimal read-only operation.

        Tries fetch(limit=1) by default. Subclasses that don't implement
        fetch() (e.g. payment adapters) should override this method with
        a provider-specific connectivity check.
        """
        try:
            result = await self.fetch(limit=1)
            return {"status": "ok", "sample_count": len(result)}
        except NotImplementedError:
            return {"status": "ok", "message": "adapter reachable (no fetch method)"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def needs_token_refresh(self) -> bool:
        """Check if the token is expired or about to expire."""
        return False
