"""Google Workspace adapter — Google Meet API for meeting ingestion.

Uses the Meet REST API (meet.googleapis.com/v2) as the primary discovery
mechanism. Each conference record natively links its recordings, transcripts,
smart notes (Gemini), and participants. Drive API used only for content download.

Domain-wide delegation: service account impersonates each user to get their
organized conferences. All Google API calls are synchronous (google-api-python-client)
wrapped in asyncio.to_thread().
"""

import asyncio
import logging
from datetime import datetime

from kernel.integration.adapter import (
    Adapter,
    AdapterAuthError,
)
from kernel.integration.registry import register_adapter

logger = logging.getLogger(__name__)

MEET_SCOPES = ["https://www.googleapis.com/auth/meetings.space.readonly"]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
ADMIN_SCOPES = ["https://www.googleapis.com/auth/admin.directory.user.readonly"]


class GoogleWorkspaceAdapter(Adapter):
    """Google Workspace adapter with domain-wide delegation."""

    def __init__(self, config: dict, credentials: dict):
        super().__init__(config, credentials)
        from google.oauth2 import service_account

        self._sa_info = credentials
        self._meet_creds_base = service_account.Credentials.from_service_account_info(
            credentials, scopes=MEET_SCOPES
        )
        self._drive_creds_base = service_account.Credentials.from_service_account_info(
            credentials, scopes=DRIVE_SCOPES
        )

    def _meet_service(self, user_email: str):
        from googleapiclient.discovery import build

        creds = self._meet_creds_base.with_subject(user_email)
        return build("meet", "v2", credentials=creds, cache_discovery=False)

    def _drive_service(self, user_email: str):
        from googleapiclient.discovery import build

        creds = self._drive_creds_base.with_subject(user_email)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _admin_service(self):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            self._sa_info,
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
        """Fetch meetings via Google Meet API across all domain users.

        Uses conferenceRecords as the anchor — each record natively links
        recordings, transcripts, smart notes, and participants.
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

        # Filter out service/system accounts
        user_emails = [
            e
            for e in user_emails
            if not any(skip in e for skip in ["backup@", "demo@", "indemn@indemn"])
        ]

        logger.info("Scanning %d users for conference records", len(user_emails))

        # Collect conference records from all users (each user's organized meetings)
        # Dedup by conference record name (same meeting seen by different users)
        all_conferences: dict[str, dict] = {}
        for email in user_emails:
            try:
                conferences = await self._list_user_conferences(email, since)
                for conf in conferences:
                    conf_id = conf["name"]
                    if conf_id not in all_conferences:
                        all_conferences[conf_id] = {
                            **conf,
                            "organizer_email": email,
                        }
            except Exception as e:
                logger.warning("Skipping %s: %s", email, e)
                continue

        logger.info(
            "Found %d unique conferences across %d users",
            len(all_conferences),
            len(user_emails),
        )

        # Build Meeting entity data for each conference
        results = []
        for conf_id, conf in all_conferences.items():
            if len(results) >= limit:
                break
            try:
                meeting = await self._build_meeting(conf)
                results.append(meeting)
            except Exception as e:
                logger.warning("Failed to build meeting %s: %s", conf_id, e)
                continue

        return results

    async def _build_meeting(self, conf: dict) -> dict:
        """Build a Meeting entity dict from a conference record + its artifacts."""
        organizer_email = conf["organizer_email"]
        conf_name = conf["name"]
        start_time = conf.get("startTime", "")
        end_time = conf.get("endTime", "")

        # Calculate duration
        duration_minutes = None
        if start_time and end_time:
            try:
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                duration_minutes = int((end_dt - start_dt).total_seconds() / 60)
            except Exception:
                pass

        # Get all artifacts in parallel
        participants, recordings, transcripts, smart_notes = await asyncio.gather(
            self._get_participants(organizer_email, conf_name),
            self._get_recordings(organizer_email, conf_name),
            self._get_transcripts(organizer_email, conf_name),
            self._get_smart_notes(organizer_email, conf_name),
        )

        # Recording info
        recording_url = None
        if recordings:
            dd = recordings[0].get("driveDestination", {})
            recording_url = dd.get("exportUri")

        # Transcript: prefer the Doc, download content
        transcript_text = ""
        transcript_url = None
        transcript_doc_id = None
        if transcripts:
            dd = transcripts[0].get("docsDestination", {})
            transcript_doc_id = dd.get("document")
            transcript_url = dd.get("exportUri")
            if transcript_doc_id:
                try:
                    transcript_text = await self._export_doc(organizer_email, transcript_doc_id)
                except Exception as e:
                    logger.warning("Failed to export transcript %s: %s", transcript_doc_id, e)

        # Smart Notes (Gemini): download content
        notes_text = ""
        notes_url = None
        notes_doc_id = None
        if smart_notes:
            dd = smart_notes[0].get("docsDestination", {})
            notes_doc_id = dd.get("document")
            notes_url = dd.get("exportUri")
            # Only download if it's a different doc than the transcript
            if notes_doc_id and notes_doc_id != transcript_doc_id:
                try:
                    notes_text = await self._export_doc(organizer_email, notes_doc_id)
                except Exception as e:
                    logger.warning("Failed to export notes %s: %s", notes_doc_id, e)
            elif notes_doc_id == transcript_doc_id:
                # Same doc serves as both — notes IS the transcript content
                notes_text = transcript_text

        # Extract summary from Gemini notes
        summary = _extract_summary(notes_text) if notes_text else ""

        # Build title from the first line of notes or transcript, or fallback
        title = _extract_title(notes_text) or _extract_title(transcript_text) or "Untitled Meeting"

        # Participant names (filter out bots)
        team_members = [p["name"] for p in participants if p.get("name") and p["name"] != "?"]

        # Use conference record name as external_ref (globally unique)
        external_ref = conf_name

        return {
            "title": title,
            "date": start_time,
            "duration_minutes": duration_minutes,
            "source": "google_meet",
            "transcript": transcript_text,
            "summary": summary,
            "notes": notes_text,
            "transcript_ref": transcript_url or notes_url,
            "recording_ref": recording_url,
            "external_ref": external_ref,
            "team_members": team_members,
        }

    async def _list_domain_users(self) -> list[str]:
        def _sync():
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

        return await asyncio.to_thread(_sync)

    async def _list_user_conferences(self, email: str, since: str = None) -> list[dict]:
        """List conference records organized by this user."""

        def _sync():
            service = self._meet_service(email)
            filter_str = ""
            if since:
                filter_str = f'end_time>="{since}"'

            records = []
            page_token = None
            while True:
                kwargs = {"pageSize": 50}
                if filter_str:
                    kwargs["filter"] = filter_str
                if page_token:
                    kwargs["pageToken"] = page_token
                response = service.conferenceRecords().list(**kwargs).execute()
                records.extend(response.get("conferenceRecords", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return records

        return await asyncio.to_thread(_sync)

    async def _get_participants(self, email: str, conf_name: str) -> list[dict]:
        def _sync():
            service = self._meet_service(email)
            result = service.conferenceRecords().participants().list(parent=conf_name).execute()
            participants = []
            for p in result.get("participants", []):
                signed_in = p.get("signedinUser", {})
                anon = p.get("anonymousUser", {})
                phone = p.get("phoneUser", {})
                name = (
                    signed_in.get("displayName")
                    or anon.get("displayName")
                    or phone.get("displayName")
                    or "?"
                )
                # Filter out AI/bot participants
                if any(skip in name.lower() for skip in ["ai assistant", "bot", "notetaker"]):
                    continue
                participants.append(
                    {
                        "name": name,
                        "user_id": signed_in.get("user", ""),
                        "email": email if name != "?" else "",
                    }
                )
            return participants

        return await asyncio.to_thread(_sync)

    async def _get_recordings(self, email: str, conf_name: str) -> list[dict]:
        def _sync():
            service = self._meet_service(email)
            result = service.conferenceRecords().recordings().list(parent=conf_name).execute()
            return result.get("recordings", [])

        return await asyncio.to_thread(_sync)

    async def _get_transcripts(self, email: str, conf_name: str) -> list[dict]:
        def _sync():
            service = self._meet_service(email)
            result = service.conferenceRecords().transcripts().list(parent=conf_name).execute()
            return result.get("transcripts", [])

        return await asyncio.to_thread(_sync)

    async def _get_smart_notes(self, email: str, conf_name: str) -> list[dict]:
        def _sync():
            service = self._meet_service(email)
            try:
                result = service.conferenceRecords().smartNotes().list(parent=conf_name).execute()
                return result.get("smartNotes", [])
            except Exception:
                return []

        return await asyncio.to_thread(_sync)

    async def _export_doc(self, email: str, doc_id: str) -> str:
        def _sync():
            service = self._drive_service(email)
            return service.files().export(fileId=doc_id, mimeType="text/plain").execute()

        content = await asyncio.to_thread(_sync)
        return content.decode("utf-8") if isinstance(content, bytes) else content

    async def test(self) -> dict:
        try:
            admin_email = self.config.get("admin_email")
            if not admin_email:
                return {"status": "error", "message": "admin_email not set in config"}
            conferences = await self._list_user_conferences(admin_email)
            users = await self._list_domain_users()
            return {
                "status": "ok",
                "domain": self.config["domain"],
                "users": len(users),
                "recent_conferences": len(conferences),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


# --- Parsing helpers ---


def _extract_title(text: str) -> str:
    """Extract meeting title from Notes or Transcript content.

    Notes format: "📝 Notes\\nDate\\nTitle\\nInvited..."
    Transcript format: "Title - Date - Transcript\\nAttendees\\n..."
    """
    if not text:
        return ""
    lines = text.strip().split("\n")

    # Notes format: skip emoji line + date line, title is line 3
    if lines and "Notes" in lines[0]:
        if len(lines) >= 3:
            return lines[2].strip()

    # Transcript format: first line is "Title - Date - Transcript"
    if lines and "Transcript" in lines[0]:
        import re

        m = re.match(r"^(.+?) - \d{4}/\d{2}/\d{2} .+ - Transcript$", lines[0])
        if m:
            return m.group(1)

    # Fallback: first non-empty line that isn't a header
    for line in lines[:5]:
        line = line.strip()
        if line and line not in ("Notes", "Transcript", "") and "📝" not in line:
            return line

    return ""


def _extract_summary(notes_text: str) -> str:
    """Extract the Summary section from Gemini notes."""
    import re

    if not notes_text:
        return ""

    # Match between "Summary" header and the next known section
    match = re.search(
        r"\nSummary\n(.+?)(?=\n(?:Rate this Summary|Decisions|Next steps|Details))",
        notes_text,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()

    # Fallback: after Summary header until double blank line
    match = re.search(r"\nSummary\n(.+?)(?=\n\n\n)", notes_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    return ""


register_adapter("google", "v1", GoogleWorkspaceAdapter)
