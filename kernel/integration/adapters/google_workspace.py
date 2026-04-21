"""Google Workspace adapter — domain-wide delegation for org-level Drive access.

Supports: fetch meeting transcripts (Gemini "Notes by Gemini" docs) across all
users in the domain via service account impersonation. Uses google-api-python-client
(synchronous) wrapped in asyncio.to_thread().
"""

import asyncio
import logging
import re
from datetime import datetime

from kernel.integration.adapter import (
    Adapter,
    AdapterAuthError,
)
from kernel.integration.registry import register_adapter

logger = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
ADMIN_SCOPES = ["https://www.googleapis.com/auth/admin.directory.user.readonly"]


class GoogleWorkspaceAdapter(Adapter):
    """Google Workspace adapter with domain-wide delegation."""

    def __init__(self, config: dict, credentials: dict):
        super().__init__(config, credentials)
        # config: {"domain": "indemn.ai", "admin_email": "craig@indemn.ai"}
        # credentials: full service account JSON key dict from Secrets Manager
        from google.oauth2 import service_account

        self._base_creds = service_account.Credentials.from_service_account_info(
            credentials, scopes=DRIVE_SCOPES
        )

    def _drive_service(self, user_email: str):
        """Create a Drive API service impersonating a specific user."""
        from googleapiclient.discovery import build

        creds = self._base_creds.with_subject(user_email)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _admin_service(self):
        """Create Admin SDK service impersonating the admin email."""
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            self.credentials, scopes=ADMIN_SCOPES, subject=self.config["admin_email"]
        )
        return build("admin", "directory_v1", credentials=creds, cache_discovery=False)

    async def fetch(
        self,
        query: str = "Notes by Gemini",
        since: str = None,
        user_emails: list = None,
        limit: int = 500,
        **params,
    ) -> list[dict]:
        """Fetch Gemini meeting transcripts from all users' Drives.

        Args:
            query: Search string for file names (default: "Notes by Gemini")
            since: ISO datetime — only fetch docs modified after this time
            user_emails: Specific users to scan (default: all domain users via Admin SDK)
            limit: Max total docs to return across all users
        """
        # 1. Get user list
        if not user_emails:
            try:
                user_emails = await self._list_domain_users()
            except Exception as e:
                # Admin SDK might not be authorized — check config fallback
                fallback = self.config.get("user_emails")
                if fallback:
                    logger.warning("Admin SDK failed (%s), using config user_emails fallback", e)
                    user_emails = fallback
                else:
                    raise AdapterAuthError(
                        f"Cannot list domain users (Admin SDK): {e}. "
                        f"Either authorize admin.directory.user.readonly scope in DWD, "
                        f"or provide user_emails in adapter config."
                    )

        logger.info("Scanning %d users for '%s' docs", len(user_emails), query)

        # 2. For each user, search Drive — dedup by file ID across users
        all_files: dict[str, dict] = {}
        for email in user_emails:
            try:
                files = await self._search_user_drive(email, query, since)
                for f in files:
                    if f["id"] not in all_files:
                        all_files[f["id"]] = {**f, "owner_email": email}
            except Exception as e:
                logger.warning("Skipping %s: %s", email, e)
                continue

            if len(all_files) >= limit:
                break

        logger.info("Found %d unique docs across %d users", len(all_files), len(user_emails))

        # 3. Export each doc as text and parse into Meeting fields
        results = []
        for file_id, file_data in all_files.items():
            try:
                content = await self._export_doc(file_data["owner_email"], file_id)
                meeting = parse_gemini_transcript(content, file_data["name"], file_data)
                results.append(meeting)
            except Exception as e:
                logger.warning("Failed to export %s (%s): %s", file_data["name"], file_id, e)
                continue

        return results

    async def _list_domain_users(self) -> list[str]:
        """List all active users in the domain via Admin SDK."""

        def _sync_list():
            service = self._admin_service()
            users = []
            page_token = None
            while True:
                response = (
                    service.users()
                    .list(
                        domain=self.config["domain"],
                        pageToken=page_token,
                        maxResults=500,
                        query="isSuspended=false",
                    )
                    .execute()
                )
                users.extend([u["primaryEmail"] for u in response.get("users", [])])
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return users

        return await asyncio.to_thread(_sync_list)

    async def _search_user_drive(
        self, email: str, query: str, since: str = None
    ) -> list[dict]:
        """Search a user's Drive for matching files."""

        def _sync_search():
            service = self._drive_service(email)
            drive_query = (
                f"name contains '{query}' "
                f"and mimeType='application/vnd.google-apps.document' "
                f"and trashed=false"
            )
            if since:
                drive_query += f" and modifiedTime > '{since}'"

            files = []
            page_token = None
            while True:
                response = (
                    service.files()
                    .list(
                        q=drive_query,
                        pageSize=100,
                        fields=(
                            "nextPageToken, files(id, name, mimeType,"
                            " modifiedTime, createdTime)"
                        ),
                        pageToken=page_token,
                    )
                    .execute()
                )
                files.extend(response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return files

        return await asyncio.to_thread(_sync_search)

    async def _export_doc(self, email: str, file_id: str) -> str:
        """Export a Google Doc as plain text."""

        def _sync_export():
            service = self._drive_service(email)
            return (
                service.files().export(fileId=file_id, mimeType="text/plain").execute()
            )

        content = await asyncio.to_thread(_sync_export)
        return content.decode("utf-8") if isinstance(content, bytes) else content

    async def test(self) -> dict:
        """Verify service account can impersonate and access Drive."""
        try:
            admin_email = self.config.get("admin_email")
            if not admin_email:
                return {"status": "error", "message": "admin_email not set in config"}
            await self._search_user_drive(admin_email, "test", None)
            return {"status": "ok", "domain": self.config["domain"]}
        except Exception as e:
            return {"status": "error", "message": str(e)}


def parse_gemini_transcript(
    raw_text: str, doc_name: str, doc_metadata: dict
) -> dict:
    """Parse a Gemini 'Notes by Gemini' document into Meeting entity fields.

    Gemini format: Notes header → Summary → Decisions → Next steps → Details.
    Returns dict matching Meeting entity field names.
    """
    # Parse doc name: "<Title> - YYYY/MM/DD HH:MM TZ - Notes by Gemini"
    name_match = re.match(
        r"^(.+?) - (\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}) (\w+) - Notes by Gemini$",
        doc_name,
    )
    if name_match:
        title = name_match.group(1)
        date_str = name_match.group(2)
        time_str = name_match.group(3)
        meeting_date = (
            datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M").isoformat()
            + "Z"
        )
    else:
        title = doc_name
        meeting_date = doc_metadata.get("createdTime")

    # Parse "Invited <names>" line
    team_members = []
    invited_match = re.search(r"Invited (.+?)(?:\n|Attachments)", raw_text)
    if invited_match:
        names_raw = invited_match.group(1).strip()
        # Gemini lists space-separated full names: "George Remmer Peter Duffy"
        team_members = re.findall(
            r"[A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?", names_raw
        )

    # Extract Summary section
    summary = ""
    summary_match = re.search(
        r"\nSummary\n(.+?)(?=\n(?:Rate this Summary|Decisions|Next steps|Details)\n)",
        raw_text,
        re.DOTALL,
    )
    if summary_match:
        summary = summary_match.group(1).strip()

    return {
        "title": title,
        "date": meeting_date,
        "source": "google_meet",
        "transcript": raw_text,
        "summary": summary,
        "transcript_ref": doc_metadata.get("id"),
        "external_ref": doc_metadata.get("id"),
        "team_members": team_members,
    }


register_adapter("google", "v1", GoogleWorkspaceAdapter)
