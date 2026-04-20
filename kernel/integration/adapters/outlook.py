"""Outlook adapter — Microsoft Graph API for email.

Supports: fetch emails, send emails, OAuth token refresh.
Uses async httpx for all HTTP calls. [G-31]
"""

from datetime import datetime, timedelta, timezone

import httpx

from kernel.integration.adapter import (
    Adapter,
    AdapterAuthError,
    AdapterRateLimitError,
    AdapterTimeoutError,
)
from kernel.integration.registry import register_adapter


class OutlookAdapter(Adapter):
    """Microsoft Graph API adapter for Outlook email."""

    def needs_token_refresh(self) -> bool:
        """Check if the access token is expired or about to expire."""
        expires_at = self.credentials.get("expires_at")
        if not expires_at:
            return False
        expiry = datetime.fromisoformat(expires_at)
        return expiry < datetime.now(timezone.utc) + timedelta(minutes=5)

    async def refresh_token(self) -> dict:
        """Refresh OAuth tokens using the refresh token. [G-26]"""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://login.microsoftonline.com/{self.config['tenant_id']}/oauth2/v2.0/token",
                data={
                    "client_id": self.config["client_id"],
                    "client_secret": self.credentials["client_secret"],
                    "refresh_token": self.credentials["refresh_token"],
                    "grant_type": "refresh_token",
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            if response.status_code != 200:
                raise AdapterAuthError(f"Token refresh failed: {response.text}")

            token_data = response.json()
            return {
                **self.credentials,
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", self.credentials["refresh_token"]),
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])
                ).isoformat(),
            }

    async def fetch(
        self, since: str = None, folder: str = "inbox", limit: int = 50, **params
    ) -> list[dict]:
        """Fetch emails from Outlook inbox."""
        headers = {"Authorization": f"Bearer {self.credentials['access_token']}"}
        url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages"
        query_params = {"$top": limit, "$orderby": "receivedDateTime desc"}
        if since:
            query_params["$filter"] = f"receivedDateTime ge {since}"

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=query_params, timeout=30.0)

            if response.status_code == 401:
                raise AdapterAuthError("Outlook: access token expired")
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "60"))
                raise AdapterRateLimitError("Outlook rate limited", retry_after=retry_after)
            if response.status_code >= 500:
                raise AdapterTimeoutError(f"Outlook server error: {response.status_code}")

            response.raise_for_status()
            messages = response.json().get("value", [])

        return [self._map_to_os(msg) for msg in messages]

    async def send(self, payload: dict) -> dict:
        """Send an email via Outlook."""
        headers = {"Authorization": f"Bearer {self.credentials['access_token']}"}
        url = "https://graph.microsoft.com/v1.0/me/sendMail"
        body = {"message": self._map_from_os(payload)}

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=body, timeout=30.0)
            if response.status_code == 401:
                raise AdapterAuthError("Outlook: access token expired")
            response.raise_for_status()

        return {"status": "sent"}

    def _map_to_os(self, outlook_msg: dict) -> dict:
        """Map Outlook message format to OS format."""
        return {
            "external_id": outlook_msg["id"],
            "from_address": (
                outlook_msg.get("from", {}).get("emailAddress", {}).get("address", "")
            ),
            "to_addresses": [
                r.get("emailAddress", {}).get("address", "")
                for r in outlook_msg.get("toRecipients", [])
            ],
            "subject": outlook_msg.get("subject", ""),
            "body": outlook_msg.get("body", {}).get("content", ""),
            "received_at": outlook_msg.get("receivedDateTime"),
            "thread_id": outlook_msg.get("conversationId"),
            "has_attachments": outlook_msg.get("hasAttachments", False),
        }

    def _map_from_os(self, email_data: dict) -> dict:
        """Map OS format to Outlook message format."""
        return {
            "toRecipients": [
                {"emailAddress": {"address": to}} for to in email_data.get("to_addresses", [])
            ],
            "subject": email_data.get("subject", ""),
            "body": {"contentType": "HTML", "content": email_data.get("body", "")},
        }


register_adapter("outlook", "v2", OutlookAdapter)
