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
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _normalize_rfc3339(s: str) -> str:
    """Accept 'YYYY-MM-DD' or RFC3339 datetime; return RFC3339 with Z.

    Google Meet API rejects date-only filters as 'Invalid filter' — every caller
    needs a full RFC3339 timestamp. Normalize here so callers can pass either form.
    """
    if not s:
        return s
    if "T" in s:
        return s if (s.endswith("Z") or "+" in s[10:]) else s + "Z"
    return s + "T00:00:00Z"


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

    def _gmail_service(self, user_email: str):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            self._sa_info,
            scopes=GMAIL_SCOPES,
            subject=user_email,
        )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _calendar_service(self, user_email: str):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            self._sa_info,
            scopes=CALENDAR_SCOPES,
            subject=user_email,
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

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

        `since` accepts 'YYYY-MM-DD' or full RFC3339 datetime — normalized here.
        """
        since = _normalize_rfc3339(since)
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
            "Found %d unique conference records across %d users",
            len(all_conferences),
            len(real_users),
        )

        # Deduplicate by meeting code + date — Google creates multiple conference
        # records for the same meeting room (lobby entry vs actual meeting).
        # Keep the record with the most participants (the real meeting).
        by_code_date: dict[str, list[dict]] = {}
        for conf in all_conferences.values():
            space = conf.get("space", "")
            date = conf.get("startTime", "")[:10]  # YYYY-MM-DD
            key = f"{space}:{date}"
            by_code_date.setdefault(key, []).append(conf)

        deduped = []
        for key, confs in by_code_date.items():
            if len(confs) == 1:
                deduped.append(confs[0])
            else:
                # Keep the longest meeting (most likely the real one, not the lobby)
                best = max(
                    confs,
                    key=lambda c: (
                        c.get("endTime", "") > c.get("startTime", "") and c.get("endTime", "") or ""
                    ),
                )
                deduped.append(best)
                logger.info(
                    "Deduped %d conference records for %s, kept %s",
                    len(confs),
                    key,
                    best["name"][:40],
                )

        logger.info(
            "After dedup: %d unique meetings",
            len(deduped),
        )

        # Build Meeting entity data for each conference
        results = []
        for conf in deduped:
            if len(results) >= limit:
                break
            try:
                meeting = await self._build_meeting(conf)
                results.append(meeting)
            except Exception as e:
                logger.warning("Failed to build meeting %s: %s", conf["name"], e)
                continue

        return results

    async def fetch_emails(
        self,
        since: str = None,
        user_emails: list = None,
        limit: int = 500,
        **params,
    ) -> list[dict]:
        """Fetch emails via Gmail API across specified users.

        Returns list of dicts matching Email entity fields.
        Uses domain-wide delegation to impersonate each user.
        """
        if not user_emails:
            try:
                user_emails = await self._list_domain_users()
            except Exception as e:
                fallback = self.config.get("user_emails")
                if fallback:
                    user_emails = fallback
                else:
                    raise AdapterAuthError(f"Cannot list domain users: {e}")

        real_users = [
            e for e in user_emails
            if not any(skip in e for skip in ["backup@", "demo@", "indemn@indemn"])
        ]

        logger.info("Scanning %d users for emails", len(real_users))

        all_emails = []
        seen_message_ids = set()

        for email_addr in real_users:
            if len(all_emails) >= limit:
                break
            try:
                messages = await self._list_gmail_messages(email_addr, since, limit - len(all_emails))
                for msg_meta in messages:
                    msg_id = msg_meta["id"]
                    if msg_id in seen_message_ids:
                        continue
                    seen_message_ids.add(msg_id)
                    try:
                        email_data = await self._get_gmail_message(email_addr, msg_id)
                        if email_data:
                            all_emails.append(email_data)
                    except Exception as e:
                        logger.warning("Failed to get message %s for %s: %s", msg_id, email_addr, e)
            except Exception as e:
                logger.warning("Skipping %s: %s", email_addr, e)
                continue

        logger.info("Fetched %d emails across %d users", len(all_emails), len(real_users))
        return all_emails

    async def _list_gmail_messages(self, user_email: str, since: str = None, max_results: int = 500) -> list[dict]:
        """List message IDs from a user's Gmail inbox."""
        def _sync():
            service = self._gmail_service(user_email)
            query = ""
            if since:
                # Gmail query uses YYYY/MM/DD format
                date_str = since[:10].replace("-", "/")
                query = f"after:{date_str}"

            all_messages = []
            page_token = None
            while len(all_messages) < max_results:
                kwargs = {"userId": "me", "maxResults": min(100, max_results - len(all_messages))}
                if query:
                    kwargs["q"] = query
                if page_token:
                    kwargs["pageToken"] = page_token
                result = service.users().messages().list(**kwargs).execute()
                all_messages.extend(result.get("messages", []))
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
            return all_messages

        return await asyncio.to_thread(_sync)

    async def _get_gmail_message(self, user_email: str, message_id: str) -> dict | None:
        """Get full message content and parse into Email entity fields."""
        def _sync():
            service = self._gmail_service(user_email)
            return service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()

        msg = await asyncio.to_thread(_sync)

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

        # Parse recipients from headers
        def parse_addresses(header_val: str) -> list[str]:
            if not header_val:
                return []
            import email.utils
            return [addr for _, addr in email.utils.getaddresses([header_val]) if addr]

        sender = headers.get("from", "")
        # Extract just email from "Name <email>" format
        import email.utils as eu
        _, sender_email = eu.parseaddr(sender)

        to_addrs = parse_addresses(headers.get("to", ""))
        cc_addrs = parse_addresses(headers.get("cc", ""))
        bcc_addrs = parse_addresses(headers.get("bcc", ""))

        # Parse date
        date_str = headers.get("date", "")
        date_iso = None
        if date_str:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(date_str)
                date_iso = dt.isoformat()
            except Exception:
                date_iso = date_str

        # Extract body — prefer text/plain, fall back to text/html
        body = self._extract_body(msg.get("payload", {}))

        # Check for attachments
        has_attachments = self._has_attachments(msg.get("payload", {}))

        # Build attachment metadata for downstream processing
        attachment_meta = self._get_attachment_metadata(msg.get("payload", {}))

        return {
            "message_id": headers.get("message-id", message_id),
            "thread_id": msg.get("threadId", ""),
            "sender": sender_email or sender,
            "recipients": to_addrs,
            "cc": cc_addrs,
            "bcc": bcc_addrs,
            "date": date_iso,
            "subject": headers.get("subject", ""),
            "body": body,
            "has_attachments": has_attachments,
            "external_ref": headers.get("message-id", message_id),  # Email Message-ID header for cross-mailbox dedup
        }

    def _extract_body(self, payload: dict) -> str:
        """Extract email body text from MIME payload."""
        import base64

        # Simple single-part message
        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

        # Multipart — walk parts looking for text
        parts = payload.get("parts", [])
        text_parts = []
        html_parts = []

        def walk_parts(parts_list):
            for part in parts_list:
                mime = part.get("mimeType", "")
                if mime == "text/plain" and part.get("body", {}).get("data"):
                    text_parts.append(
                        base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                    )
                elif mime == "text/html" and part.get("body", {}).get("data"):
                    html_parts.append(
                        base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                    )
                elif part.get("parts"):
                    walk_parts(part["parts"])

        walk_parts(parts)

        # Prefer plain text, fall back to HTML
        if text_parts:
            return "\n".join(text_parts)
        if html_parts:
            return "\n".join(html_parts)
        return ""

    def _has_attachments(self, payload: dict) -> bool:
        """Check if the message has file attachments."""
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("filename"):
                return True
            if part.get("parts") and self._has_attachments({"parts": part["parts"]}):
                return True
        return False

    def _get_attachment_metadata(self, payload: dict) -> list[dict]:
        """Get metadata for attachments (not the content — downloaded later)."""
        attachments = []
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("filename"):
                attachments.append({
                    "filename": part["filename"],
                    "mime_type": part.get("mimeType", ""),
                    "size": part.get("body", {}).get("size", 0),
                    "attachment_id": part.get("body", {}).get("attachmentId", ""),
                })
            if part.get("parts"):
                attachments.extend(self._get_attachment_metadata({"parts": part["parts"]}))
        return attachments

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

        # Look up calendar event by meeting code for title + attendee emails
        cal_event = {}
        if meeting_code and start_time:
            cal_event = await self._find_calendar_event(discovered_via, meeting_code, start_time)

        # Calendar attendee emails (for enriching participant data)
        cal_attendee_emails = {a.get("email", ""): a for a in cal_event.get("attendees", [])}
        # Calendar organizer
        cal_organizer = cal_event.get("organizer", {}).get("email", "")

        # Build structured participant data by merging:
        # 1. Calendar attendees (everyone invited, with emails)
        # 2. Meet participants (who actually joined, with timestamps)
        participants = []
        organizer = cal_organizer or discovered_via
        team_member_names = []
        seen_emails = set()

        # First: Meet participants (actually joined — have join/leave times)
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

            # If we couldn't resolve email from Admin SDK, try calendar attendees
            # Match by: exact name, first name, or email prefix contains name
            if not email and cal_attendee_emails:
                name_lower = name.lower()
                first_name = name_lower.split()[0] if name_lower else ""
                for cal_email, cal_att in cal_attendee_emails.items():
                    cal_name = (cal_att.get("displayName") or "").lower()
                    cal_prefix = cal_email.split("@")[0].lower()
                    if (
                        (cal_name and cal_name == name_lower)
                        or (cal_name and first_name and cal_name.startswith(first_name))
                        or (first_name and first_name in cal_prefix)
                        or (cal_prefix and cal_prefix in name_lower)
                    ):
                        email = cal_email
                        break

            participants.append(
                {
                    "name": name,
                    "user_id": user_id,
                    "email": email,
                    "joined": p.get("earliestStartTime", ""),
                    "left": p.get("latestEndTime", ""),
                    "type": p_type,
                    "attended": True,
                }
            )
            team_member_names.append(name)
            if email:
                seen_emails.add(email)

        # Second: Calendar attendees who did NOT join the Meet call
        for cal_email, cal_att in cal_attendee_emails.items():
            if cal_email in seen_emails:
                continue
            cal_name = cal_att.get("displayName") or ""
            # Use display name if available, otherwise derive from email
            display = cal_name if cal_name else cal_email.split("@")[0].replace(".", " ").title()
            rsvp = cal_att.get("responseStatus", "")
            participants.append(
                {
                    "name": display,
                    "user_id": "",
                    "email": cal_email,
                    "joined": "",
                    "left": "",
                    "type": "invited",
                    "attended": False,
                    "rsvp": rsvp,
                }
            )
            team_member_names.append(display)

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
            transcript_text = self._entries_to_text(transcript_entries, participants)
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

        # Build title — prefer calendar event name, then notes, then transcript
        cal_title = cal_event.get("summary", "")
        title = (
            cal_title
            or _extract_title(notes_text)
            or _extract_title(transcript_text)
            or "Untitled Meeting"
        )

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

    def _entries_to_text(self, entries: list[dict], participants: list[dict]) -> str:
        """Convert structured transcript entries to readable text.

        Uses both Admin SDK user map AND the built participant list
        to resolve speaker IDs to names (handles external participants).
        """
        # Build participant ID → name lookup from the participants we've built
        id_to_name: dict[str, str] = {}
        for p in participants:
            uid = p.get("user_id", "")
            if uid:
                # Extract numeric ID from "users/12345"
                numeric = uid.rsplit("/", 1)[-1] if "/" in uid else uid
                id_to_name[numeric] = p.get("name", "")

        lines = []
        for entry in entries:
            p_ref = entry.get("participant", "")
            # participant ref: "conferenceRecords/.../participants/USER_ID"
            p_id = p_ref.rsplit("/", 1)[-1] if "/" in p_ref else p_ref

            # Resolve: participant list first (has external names), then Admin SDK
            speaker = id_to_name.get(p_id, "")
            if not speaker:
                user_key = f"users/{p_id}"
                email = self._user_id_to_email.get(user_key, "")
                speaker = email.split("@")[0] if email else p_id

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

    async def _find_calendar_event(self, email: str, meeting_code: str, start_time: str) -> dict:
        """Find calendar event by meeting code. Returns event dict or {}."""

        def _sync():
            service = self._calendar_service(email)
            # Search around the meeting start time (±2 hours)
            try:
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            except Exception:
                return {}
            from datetime import timedelta

            time_min = (start_dt - timedelta(hours=2)).isoformat()
            time_max = (start_dt + timedelta(hours=2)).isoformat()

            try:
                events = (
                    service.events()
                    .list(
                        calendarId="primary",
                        timeMin=time_min,
                        timeMax=time_max,
                        maxResults=20,
                        singleEvents=True,
                    )
                    .execute()
                )
                # Find event with matching conference/meeting code
                for event in events.get("items", []):
                    conf_data = event.get("conferenceData", {})
                    if conf_data.get("conferenceId") == meeting_code:
                        return event
                    # Also check entry points for the meeting URI
                    for ep in conf_data.get("entryPoints", []):
                        if meeting_code in ep.get("uri", ""):
                            return event
            except Exception as e:
                logger.warning("Calendar lookup failed for %s: %s", email, e)
            return {}

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
