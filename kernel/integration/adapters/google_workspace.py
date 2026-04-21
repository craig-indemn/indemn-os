"""Google Workspace adapter — domain-wide delegation for org-level Drive access.

Fetches Google Meet meeting data: Gemini notes, word-for-word transcripts, and
recording links. Groups related files per meeting. Uses google-api-python-client
(synchronous) wrapped in asyncio.to_thread().

Google Meet creates up to 3 files per meeting:
  - "Title - YYYY/MM/DD HH:MM TZ - Notes by Gemini" (AI summary doc)
  - "Title - YYYY/MM/DD HH:MM TZ - Transcript" (word-for-word speaker-attributed)
  - "Title - YYYY/MM/DD HH:MM TZ - Recording" (video/mp4)
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

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
ADMIN_SCOPES = ["https://www.googleapis.com/auth/admin.directory.user.readonly"]

# Patterns for Google Meet file suffixes
_NOTES_SUFFIX = re.compile(r"^(.+?) - (\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}) (\w+) - Notes by Gemini$")
_TRANSCRIPT_SUFFIX = re.compile(r"^(.+?) - (\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}) (\w+) - Transcript$")
_RECORDING_SUFFIX = re.compile(r"^(.+?) - (\d{4}/\d{2}/\d{2}) (\d{2}:\d{2}) (\w+) - Recording$")


def _parse_meeting_key(name: str) -> tuple[str, str, str] | None:
    """Extract (title, date, time) from any Google Meet file name."""
    for pattern in [_NOTES_SUFFIX, _TRANSCRIPT_SUFFIX, _RECORDING_SUFFIX]:
        m = pattern.match(name)
        if m:
            return (m.group(1), m.group(2), m.group(3))
    return None


class GoogleWorkspaceAdapter(Adapter):
    """Google Workspace adapter with domain-wide delegation."""

    def __init__(self, config: dict, credentials: dict):
        super().__init__(config, credentials)
        from google.oauth2 import service_account

        self._base_creds = service_account.Credentials.from_service_account_info(
            credentials, scopes=DRIVE_SCOPES
        )

    def _drive_service(self, user_email: str):
        from googleapiclient.discovery import build

        creds = self._base_creds.with_subject(user_email)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _admin_service(self):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            self.credentials,
            scopes=ADMIN_SCOPES,
            subject=self.config["admin_email"],
        )
        return build("admin", "directory_v1", credentials=creds, cache_discovery=False)

    async def fetch(
        self,
        since: str = None,
        user_emails: list = None,
        limit: int = 500,
        **params,
    ) -> list[dict]:
        """Fetch Google Meet meetings from all users' Drives.

        Searches for Notes, Transcripts, and Recordings. Groups related files
        per meeting. Returns Meeting entity-shaped dicts.
        """
        if not user_emails:
            try:
                user_emails = await self._list_domain_users()
            except Exception as e:
                fallback = self.config.get("user_emails")
                if fallback:
                    logger.warning("Admin SDK failed (%s), using config fallback", e)
                    user_emails = fallback
                else:
                    raise AdapterAuthError(
                        f"Cannot list domain users: {e}. "
                        f"Authorize admin.directory.user.readonly in DWD, "
                        f"or set user_emails in config."
                    )

        logger.info("Scanning %d users for meeting files", len(user_emails))

        # Search for ALL meeting-related files across all users
        # Google Meet files: Notes by Gemini, Transcript, Recording
        all_files: dict[str, dict] = {}  # file_id -> file_data
        queries = [
            ("Notes by Gemini", "application/vnd.google-apps.document"),
            ("Transcript", "application/vnd.google-apps.document"),
            ("Recording", "video/mp4"),
        ]

        for email in user_emails:
            for query_text, mime_type in queries:
                try:
                    files = await self._search_user_drive(email, query_text, mime_type, since)
                    for f in files:
                        if f["id"] not in all_files:
                            all_files[f["id"]] = {**f, "owner_email": email}
                except Exception as e:
                    logger.warning("Skipping %s/%s: %s", email, query_text, e)
                    continue

        logger.info("Found %d unique files across %d users", len(all_files), len(user_emails))

        # Group files by meeting (same title + date)
        meetings: dict[tuple, dict] = {}  # (title, date) -> {notes, transcript, recording}
        for file_data in all_files.values():
            key = _parse_meeting_key(file_data["name"])
            if not key:
                continue
            if key not in meetings:
                meetings[key] = {
                    "title": key[0],
                    "date_str": key[1],
                    "time_str": key[2],
                    "notes": None,
                    "transcript": None,
                    "recording": None,
                }
            name = file_data["name"]
            if "Notes by Gemini" in name:
                meetings[key]["notes"] = file_data
            elif "- Transcript" in name:
                meetings[key]["transcript"] = file_data
            elif "- Recording" in name:
                meetings[key]["recording"] = file_data

        logger.info("Grouped into %d unique meetings", len(meetings))

        # For each meeting, export docs and build Meeting entity data
        results = []
        for meeting_key, meeting_files in meetings.items():
            if len(results) >= limit:
                break
            try:
                result = await self._build_meeting(meeting_files)
                results.append(result)
            except Exception as e:
                logger.warning("Failed to build meeting %s: %s", meeting_files["title"], e)
                continue

        return results

    async def _build_meeting(self, meeting_files: dict) -> dict:
        """Build a Meeting entity dict from grouped files."""
        title = meeting_files["title"]
        date_str = meeting_files["date_str"]
        time_str = meeting_files["time_str"]

        meeting_date = (
            datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M").isoformat() + "Z"
        )

        # Use the Notes file ID as the primary external_ref (most meetings have notes)
        # Fall back to transcript ID if no notes
        notes_file = meeting_files.get("notes")
        transcript_file = meeting_files.get("transcript")
        recording_file = meeting_files.get("recording")

        primary_file = notes_file or transcript_file
        if not primary_file:
            raise ValueError(f"Meeting '{title}' has no notes or transcript")

        external_ref = primary_file["id"]

        # Export the actual transcript (word-for-word) if available
        transcript_text = ""
        team_members = []
        if transcript_file:
            transcript_text = await self._export_doc(
                transcript_file["owner_email"], transcript_file["id"]
            )
            team_members = _parse_attendees_from_transcript(transcript_text)

        # Export the Gemini notes (AI summary) if available
        notes_text = ""
        if notes_file:
            notes_text = await self._export_doc(notes_file["owner_email"], notes_file["id"])
            # If we didn't get attendees from transcript, try from notes
            if not team_members:
                team_members = _parse_attendees_from_notes(notes_text)

        # Extract summary from Gemini notes
        summary = _extract_summary(notes_text) if notes_text else ""

        # Build URLs
        transcript_url = None
        if transcript_file:
            transcript_url = f"https://docs.google.com/document/d/{transcript_file['id']}/edit"
        elif notes_file:
            transcript_url = f"https://docs.google.com/document/d/{notes_file['id']}/edit"

        recording_url = None
        if recording_file:
            recording_url = f"https://drive.google.com/file/d/{recording_file['id']}/view"

        return {
            "title": title,
            "date": meeting_date,
            "source": "google_meet",
            "transcript": transcript_text or notes_text,
            "summary": summary,
            "transcript_ref": transcript_url,
            "recording_ref": recording_url,
            "external_ref": external_ref,
            "team_members": team_members,
        }

    async def _list_domain_users(self) -> list[str]:
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
        self,
        email: str,
        query: str,
        mime_type: str,
        since: str = None,
    ) -> list[dict]:
        """Search a user's Drive for files matching query and mime type."""

        def _sync_search():
            service = self._drive_service(email)
            drive_query = f"name contains '{query}' and mimeType='{mime_type}' and trashed=false"
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
                            " modifiedTime, createdTime, webViewLink)"
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
        def _sync_export():
            service = self._drive_service(email)
            return service.files().export(fileId=file_id, mimeType="text/plain").execute()

        content = await asyncio.to_thread(_sync_export)
        return content.decode("utf-8") if isinstance(content, bytes) else content

    async def test(self) -> dict:
        try:
            admin_email = self.config.get("admin_email")
            if not admin_email:
                return {"status": "error", "message": "admin_email not set in config"}
            await self._search_user_drive(
                admin_email, "test", "application/vnd.google-apps.document", None
            )
            return {"status": "ok", "domain": self.config["domain"]}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# --- Parsing helpers ---


def _parse_attendees_from_transcript(text: str) -> list[str]:
    """Parse comma-separated attendee names from a Transcript doc.

    Format: "Attendees\\nCraig Certo, Kyle Geoghan, Ganesh Iyer, ..."
    """
    match = re.search(r"Attendees\n(.+?)\nTranscript", text, re.DOTALL)
    if match:
        raw = match.group(1).strip()
        # Comma-separated full names — clean and split
        names = [n.strip() for n in raw.split(",") if n.strip()]
        # Filter out non-person entries like "Indemn AI Assistant"
        return [n for n in names if not re.search(r"\bAI\b|\bBot\b|\bAssistant\b", n)]
    return []


def _parse_attendees_from_notes(text: str) -> list[str]:
    """Parse attendee names from a Notes by Gemini doc.

    Format: "Invited George Remmer Peter Duffy Ganesh Iyer ..."
    Space-separated full names — harder to parse. Try splitting by known
    two-word name patterns.
    """
    match = re.search(r"Invited (.+?)(?:\n|Attachments)", text)
    if match:
        raw = match.group(1).strip()
        # Try comma-separated first (some Gemini versions use commas)
        if "," in raw:
            return [n.strip() for n in raw.split(",") if n.strip()]
        # Fall back to two-word pattern matching
        return re.findall(r"[A-Z][a-z]+ [A-Z][a-z]+", raw)
    return []


def _extract_summary(notes_text: str) -> str:
    """Extract the Summary section from Gemini notes."""
    # Try matching between "Summary" header and the next known section
    match = re.search(
        r"\nSummary\n(.+?)(?=\n(?:Rate this Summary|Decisions|Next steps|Details))",
        notes_text,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()

    # Fallback: look for Summary after the header block
    match = re.search(r"\nSummary\n(.+?)(?=\n\n\n)", notes_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    return ""


register_adapter("google", "v1", GoogleWorkspaceAdapter)
