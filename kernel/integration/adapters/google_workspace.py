"""Google Workspace adapter — Google Meet API for meeting ingestion.

Uses the Meet REST API (meet.googleapis.com/v2) as the primary discovery
mechanism. Each conference record natively links its recordings, transcripts,
smart notes (Gemini), and participants. Drive API used only for content download.
Admin SDK used for user enumeration and user ID → email resolution.

Captures EVERYTHING the API provides:
- Conference: start/end time, space, meeting code/URL
- Participants: name, Google user ID, email, join/leave times, type
- Recordings: Drive file ID, URL, start/end time
- Transcripts: Doc ID, URL, structured entries (speaker, text, timestamp)
- Smart Notes: Doc ID, URL, content with Summary/Decisions/Next Steps/Details
- Transcript entries expire after 30 days — captured on every ingestion run

Domain-wide delegation: service account impersonates each user to get their
conferences. All sync Google API calls wrapped in asyncio.to_thread().
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
        # Cache: Google user ID → email (built from Admin SDK)
        self._user_id_to_email: dict[str, str] = {}

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

        Captures everything: conference metadata, participants with IDs/emails,
        recordings, transcripts (doc + structured entries), smart notes.
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
        real_users = [
            e
            for e in user_emails
            if not any(skip in e for skip in ["backup@", "demo@", "indemn@indemn"])
        ]

        # Build user ID → email mapping for participant resolution
        await self._build_user_id_map(real_users)

        logger.info("Scanning %d users for conference records", len(real_users))

        # Collect conference records — dedup by conference name
        all_conferences: dict[str, dict] = {}
        for email in real_users:
            try:
                conferences = await self._list_user_conferences(email, since)
                for conf in conferences:
                    conf_id = conf["name"]
                    if conf_id not in all_conferences:
                        all_conferences[conf_id] = {
                            **conf,
                            "discovered_via": email,
                        }
            except Exception as e:
                logger.warning("Skipping %s: %s", email, e)
                continue

        logger.info(
            "Found %d unique conferences across %d users",
            len(all_conferences),
            len(real_users),
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
        """Build a Meeting entity dict with ALL available data."""
        discovered_via = conf["discovered_via"]
        conf_name = conf["name"]
        start_time = conf.get("startTime", "")
        end_time = conf.get("endTime", "")

        # Duration
        duration_minutes = None
        if start_time and end_time:
            try:
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                duration_minutes = int((end_dt - start_dt).total_seconds() / 60)
            except Exception:
                pass

        # Get space info (meeting code, URL) + all artifacts in parallel
        space_info, raw_participants, recordings, transcripts, smart_notes = await asyncio.gather(
            self._get_space(discovered_via, conf.get("space", "")),
            self._get_participants(discovered_via, conf_name),
            self._get_recordings(discovered_via, conf_name),
            self._get_transcripts(discovered_via, conf_name),
            self._get_smart_notes(discovered_via, conf_name),
        )

        # Meeting code and URL from space
        meeting_code = space_info.get("meetingCode", "")
        meeting_url = space_info.get("meetingUri", "")

        # Build structured participant data with email resolution
        participants = []
        organizer = discovered_via
        team_member_names = []
        for p in raw_participants:
            signed = p.get("signedinUser", {})
            anon = p.get("anonymousUser", {})
            phone = p.get("phoneUser", {})

            name = (
                signed.get("displayName")
                or anon.get("displayName")
                or phone.get("displayName")
                or "Unknown"
            )

            # Skip AI/bot participants
            if any(skip in name.lower() for skip in ["ai assistant", "notetaker", "bot"]):
                continue

            user_id = signed.get("user", "")
            email = self._resolve_email(user_id)
            p_type = "signed_in" if signed else "anonymous" if anon else "phone"

            participants.append(
                {
                    "name": name,
                    "user_id": user_id,
                    "email": email,
                    "joined": p.get("earliestStartTime", ""),
                    "left": p.get("latestEndTime", ""),
                    "type": p_type,
                }
            )
            team_member_names.append(name)

            # First signed-in participant who matches discovered_via is likely organizer
            if email == discovered_via:
                organizer = email

        # Recording
        recording_url = None
        if recordings:
            dd = recordings[0].get("driveDestination", {})
            recording_url = dd.get("exportUri")

        # Transcript doc
        transcript_text = ""
        transcript_url = None
        transcript_doc_id = None
        if transcripts:
            dd = transcripts[0].get("docsDestination", {})
            transcript_doc_id = dd.get("document")
            transcript_url = dd.get("exportUri")

        # Smart Notes (Gemini) doc
        notes_text = ""
        notes_url = None
        notes_doc_id = None
        if smart_notes:
            dd = smart_notes[0].get("docsDestination", {})
            notes_doc_id = dd.get("document")
            notes_url = dd.get("exportUri")

        # Download transcript entries (structured, expire after 30 days)
        # These are the primary transcript source — speaker-attributed with timestamps
        transcript_entries = []
        if transcripts:
            t_name = transcripts[0]["name"]
            transcript_entries = await self._get_transcript_entries(discovered_via, t_name)

        # Build transcript text from structured entries (preferred over Doc export)
        if transcript_entries:
            transcript_text = self._entries_to_text(transcript_entries)
        elif transcript_doc_id:
            # Fallback: export the Doc if entries aren't available
            try:
                transcript_text = await self._export_doc(discovered_via, transcript_doc_id)
            except Exception as e:
                logger.warning("Failed to export transcript doc: %s", e)

        # Download notes content (separate doc from transcript)
        if notes_doc_id and notes_doc_id != transcript_doc_id:
            try:
                notes_text = await self._export_doc(discovered_via, notes_doc_id)
            except Exception as e:
                logger.warning("Failed to export notes doc: %s", e)
        elif notes_doc_id == transcript_doc_id:
            # Same doc for both — export it for notes
            try:
                notes_text = await self._export_doc(discovered_via, notes_doc_id)
            except Exception as e:
                logger.warning("Failed to export shared doc: %s", e)

        # Extract summary from Gemini notes
        summary = _extract_summary(notes_text) if notes_text else ""

        # Build title
        title = _extract_title(notes_text) or _extract_title(transcript_text) or "Untitled Meeting"

        # Use transcript URL if available, else notes URL
        transcript_ref = transcript_url or notes_url

        return {
            "title": title,
            "date": start_time,
            "duration_minutes": duration_minutes,
            "source": "google_meet",
            "meeting_code": meeting_code,
            "meeting_url": meeting_url,
            "organizer": organizer,
            "participants": participants,
            "team_members": team_member_names,
            "transcript": transcript_text,
            "notes": notes_text,
            "summary": summary,
            "transcript_ref": transcript_ref,
            "recording_ref": recording_url,
            "external_ref": conf["name"],
        }

    def _entries_to_text(self, entries: list[dict]) -> str:
        """Convert structured transcript entries to readable text."""
        lines = []
        for entry in entries:
            # Resolve participant ID to name
            p_ref = entry.get("participant", "")
            # participant ref is like "conferenceRecords/.../participants/USER_ID"
            p_id = p_ref.rsplit("/", 1)[-1] if "/" in p_ref else p_ref
            user_key = f"users/{p_id}"
            email = self._user_id_to_email.get(user_key, "")
            # Use email username as speaker label, or the raw ID
            speaker = email.split("@")[0] if email else p_id

            # Look up display name from participants we've seen
            for uid, em in self._user_id_to_email.items():
                if uid == user_key:
                    speaker = em.split("@")[0]
                    break

            timestamp = entry.get("startTime", "")[:19]
            text = entry.get("text", "")
            lines.append(f"[{timestamp}] {speaker}: {text}")
        return "\n".join(lines)

    def _resolve_email(self, user_id: str) -> str:
        """Resolve Google user ID to email address."""
        return self._user_id_to_email.get(user_id, "")

    async def _build_user_id_map(self, user_emails: list[str]):
        """Build Google user ID → email mapping from Admin SDK."""

        def _sync():
            service = self._admin_service()
            mapping = {}
            for email in user_emails:
                try:
                    user = service.users().get(userKey=email).execute()
                    gid = user.get("id", "")
                    if gid:
                        mapping[f"users/{gid}"] = email
                except Exception:
                    continue
            return mapping

        self._user_id_to_email = await asyncio.to_thread(_sync)
        logger.info("Built user ID map: %d users resolved", len(self._user_id_to_email))

    # --- API wrappers ---

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

    async def _get_space(self, email: str, space_name: str) -> dict:
        if not space_name:
            return {}

        def _sync():
            service = self._meet_service(email)
            try:
                return service.spaces().get(name=space_name).execute()
            except Exception:
                return {}

        return await asyncio.to_thread(_sync)

    async def _get_participants(self, email: str, conf_name: str) -> list[dict]:
        def _sync():
            service = self._meet_service(email)
            all_parts = []
            page_token = None
            while True:
                kwargs = {"parent": conf_name, "pageSize": 100}
                if page_token:
                    kwargs["pageToken"] = page_token
                result = service.conferenceRecords().participants().list(**kwargs).execute()
                all_parts.extend(result.get("participants", []))
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
            return all_parts

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

    async def _get_transcript_entries(self, email: str, transcript_name: str) -> list[dict]:
        """Get ALL transcript entries (structured utterances). Expire after 30 days."""

        def _sync():
            service = self._meet_service(email)
            all_entries = []
            page_token = None
            while True:
                kwargs = {"parent": transcript_name, "pageSize": 100}
                if page_token:
                    kwargs["pageToken"] = page_token
                result = (
                    service.conferenceRecords().transcripts().entries().list(**kwargs).execute()
                )
                all_entries.extend(result.get("transcriptEntries", []))
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
            return all_entries

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
            users = await self._list_domain_users()
            conferences = await self._list_user_conferences(admin_email)
            return {
                "status": "ok",
                "domain": self.config["domain"],
                "users": len(users),
                "recent_conferences": len(conferences),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


# --- Parsing helpers ---


def _normalize(text: str) -> str:
    """Normalize line endings and BOM from Google Docs export."""
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


def _extract_title(text: str) -> str:
    """Extract meeting title from Notes or Transcript content."""
    if not text:
        return ""
    text = _normalize(text)
    lines = text.strip().split("\n")

    # Notes format: "📝 Notes\nDate\nTitle\nInvited..."
    if lines and "Notes" in lines[0]:
        if len(lines) >= 3:
            return lines[2].strip()

    # Transcript format: "Title - Date - Transcript\nAttendees\n..."
    if lines and "Transcript" in lines[0]:
        m = re.match(r"^(.+?) - \d{4}/\d{2}/\d{2} .+ - Transcript$", lines[0])
        if m:
            return m.group(1)

    # Structured entries format: "[timestamp] speaker: text"
    if lines and lines[0].startswith("["):
        return ""  # No title in structured entries

    # Fallback: first non-empty line that isn't a header/date
    for line in lines[:5]:
        line = line.strip()
        if (
            line
            and line not in ("Notes", "Transcript", "")
            and "📝" not in line
            and not re.match(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$", line)
        ):
            return line

    return ""


def _extract_summary(notes_text: str) -> str:
    """Extract the Summary section from Gemini notes."""
    if not notes_text:
        return ""

    text = _normalize(notes_text)

    match = re.search(
        r"\nSummary\n(.+?)(?=\n(?:Rate this Summary|Decisions|Next steps|Details))",
        text,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()

    match = re.search(r"\nSummary\n(.+?)(?=\n\n\n)", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    return ""


register_adapter("google", "v1", GoogleWorkspaceAdapter)
