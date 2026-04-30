"""Tests for GoogleWorkspaceAdapter — Bug #36 (`until` parameter + strict params).

Bug #36 (2026-04-29 Armadillo trace setup): the adapter accepted any kwargs via
`**params` and silently discarded them. Operators passing `until=...` to scope a
narrow-window fetch got the full `since`-onward window instead, up to the API's
500-message cap. Same defect on Meet conferences. Material side-effect: 500
unintended Email entities + 6 Meeting entities cascaded through EC/TS/IE.

Fix: add `until` plumbing (Gmail `before:<date>` + sub-day client-side filter,
Meet `start_time<="{until}"` AND-combined). Replace `**params` with
`**unknown_params` and raise `AdapterValidationError` listing what's supported.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from kernel.integration.adapter import AdapterValidationError
from kernel.integration.adapters.google_workspace import GoogleWorkspaceAdapter


# --- Helpers ---


def _make_adapter():
    """Build an adapter with the GCP service-account credential parser mocked.

    The adapter constructor eagerly calls
    `service_account.Credentials.from_service_account_info(credentials, ...)`
    twice (Meet + Drive) — that needs a well-formed JSON SA dict. For unit
    tests we don't have one (and don't want one), so we patch the parser to
    return a sentinel credentials object. Tests then patch `_meet_service` /
    `_gmail_service` per-test as needed.
    """
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=MagicMock(name="fake_creds"),
    ):
        return GoogleWorkspaceAdapter(config={}, credentials={})


# --- Bug #36: unknown-param rejection ---


class TestUnknownParamsRejection:
    """The adapter must reject unknown kwargs explicitly. Silent-absorbed
    `**params` was Bug #36's enabling failure: operators passed `until=...`
    and got 500 emails ingested instead of the intended narrow window."""

    @pytest.mark.asyncio
    async def test_fetch_rejects_unknown_kwarg(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterValidationError) as exc:
            await adapter.fetch(query="meeting")
        assert "query" in str(exc.value)
        assert "since, until, user_emails, limit" in str(exc.value)

    @pytest.mark.asyncio
    async def test_fetch_emails_rejects_unknown_kwarg(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterValidationError) as exc:
            await adapter.fetch_emails(mailbox="inbox")
        assert "mailbox" in str(exc.value)
        assert "since, until, user_emails, limit" in str(exc.value)

    @pytest.mark.asyncio
    async def test_fetch_lists_all_unknowns(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterValidationError) as exc:
            await adapter.fetch(foo=1, bar=2)
        # Sorted in the message for stability
        msg = str(exc.value)
        assert "['bar', 'foo']" in msg


# --- Bug #36: Meet filter string construction (`_list_user_conferences`) ---


class TestMeetFilterConstruction:
    """The Meet conferenceRecords filter combines `since` (lower bound on
    end_time) and `until` (upper bound on start_time) with AND. Together
    they mean "conferences overlapping the [since, until] window"."""

    def _capture_filter(self, since, until):
        """Run _list_user_conferences with mocked Meet service; return the
        `filter` kwarg that was passed to conferenceRecords().list(...)."""
        adapter = _make_adapter()
        captured = {}

        list_call = MagicMock()
        list_call.execute.return_value = {"conferenceRecords": []}
        records = MagicMock()
        records.list = MagicMock(side_effect=lambda **kw: (captured.update(kw), list_call)[1])
        service = MagicMock()
        service.conferenceRecords.return_value = records

        with patch.object(adapter, "_meet_service", return_value=service):
            import asyncio

            asyncio.run(adapter._list_user_conferences("u@x.com", since, until))
        return captured.get("filter", "")

    def test_no_bounds(self):
        assert self._capture_filter(None, None) == ""

    def test_since_only(self):
        f = self._capture_filter("2026-04-28T18:00:00Z", None)
        assert f == 'end_time>="2026-04-28T18:00:00Z"'

    def test_until_only(self):
        f = self._capture_filter(None, "2026-04-28T22:00:00Z")
        assert f == 'start_time<="2026-04-28T22:00:00Z"'

    def test_since_and_until_anded(self):
        f = self._capture_filter("2026-04-28T18:00:00Z", "2026-04-28T22:00:00Z")
        assert "end_time>=" in f
        assert "start_time<=" in f
        assert " AND " in f


# --- Bug #36: Gmail query string construction (`_list_gmail_messages`) ---


class TestGmailQueryConstruction:
    """Gmail's filter is date-precision. since→`after:YYYY/MM/DD`, until→
    `before:YYYY/MM/DD`. Sub-day until is filtered client-side post-fetch."""

    def _capture_query(self, since, until):
        adapter = _make_adapter()
        captured = {}

        list_call = MagicMock()
        list_call.execute.return_value = {"messages": []}
        messages = MagicMock()
        messages.list = MagicMock(side_effect=lambda **kw: (captured.update(kw), list_call)[1])
        users = MagicMock()
        users.messages.return_value = messages
        service = MagicMock()
        service.users.return_value = users

        with patch.object(adapter, "_gmail_service", return_value=service):
            import asyncio

            asyncio.run(adapter._list_gmail_messages("u@x.com", since, until, max_results=10))
        return captured.get("q", "")

    def test_no_bounds(self):
        assert self._capture_query(None, None) == ""

    def test_since_only(self):
        assert self._capture_query("2026-04-02T18:00:00Z", None) == "after:2026/04/02"

    def test_until_only(self):
        assert self._capture_query(None, "2026-04-29T00:00:00Z") == "before:2026/04/29"

    def test_since_and_until(self):
        q = self._capture_query("2026-04-02T18:00:00Z", "2026-04-03T00:00:00Z")
        assert q == "after:2026/04/02 before:2026/04/03"


# --- Bug #36: Sub-day client-side filter in `fetch_emails` ---


class TestSubDayClientSideFilter:
    """Gmail's filter is date-precision. For sub-day `until`, fetch_emails
    must drop messages whose parsed `Date:` header is past `until`."""

    @pytest.mark.asyncio
    async def test_messages_past_until_are_dropped(self):
        adapter = _make_adapter()
        until = "2026-04-02T22:00:00Z"
        # Three messages: one before until, one at until, one after
        msg_keep_1 = {"date": "2026-04-02T18:30:00Z", "external_ref": "m1", "subject": "keep"}
        msg_keep_2 = {"date": "2026-04-02T22:00:00Z", "external_ref": "m2", "subject": "edge"}
        msg_drop = {"date": "2026-04-02T23:00:00Z", "external_ref": "m3", "subject": "drop"}

        with patch.object(adapter, "_list_domain_users", return_value=["u@x.com"]):
            with patch.object(
                adapter,
                "_list_gmail_messages",
                return_value=[{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
            ):
                with patch.object(
                    adapter,
                    "_get_gmail_message",
                    side_effect=[msg_keep_1, msg_keep_2, msg_drop],
                ):
                    result = await adapter.fetch_emails(
                        since="2026-04-02T18:00:00Z", until=until
                    )

        external_refs = {m["external_ref"] for m in result}
        assert "m1" in external_refs  # before until
        assert "m2" in external_refs  # at until (boundary kept; > test)
        assert "m3" not in external_refs  # past until — dropped

    @pytest.mark.asyncio
    async def test_messages_with_unparseable_date_kept(self):
        """If we can't parse a message's date, keep it rather than silently
        dropping. The date-precision API filter already constrained the set."""
        adapter = _make_adapter()
        msg = {"date": "not-a-real-date", "external_ref": "weird", "subject": "?"}

        with patch.object(adapter, "_list_domain_users", return_value=["u@x.com"]):
            with patch.object(adapter, "_list_gmail_messages", return_value=[{"id": "x"}]):
                with patch.object(adapter, "_get_gmail_message", return_value=msg):
                    result = await adapter.fetch_emails(
                        since="2026-04-02T00:00:00Z", until="2026-04-03T00:00:00Z"
                    )

        assert any(m["external_ref"] == "weird" for m in result)


# --- Existing-behavior regression: until=None means no upper bound ---


class TestNoUntilUnchangedBehavior:
    """When `until` isn't passed, query/filter must be identical to pre-fix
    behavior. Pin so we don't regress the common case."""

    def test_meet_filter_with_only_since_unchanged(self):
        adapter = _make_adapter()
        captured = {}

        list_call = MagicMock()
        list_call.execute.return_value = {"conferenceRecords": []}
        records = MagicMock()
        records.list = MagicMock(side_effect=lambda **kw: (captured.update(kw), list_call)[1])
        service = MagicMock()
        service.conferenceRecords.return_value = records

        with patch.object(adapter, "_meet_service", return_value=service):
            import asyncio

            asyncio.run(adapter._list_user_conferences("u@x.com", "2026-04-28T18:00:00Z"))

        # Identical to the pre-Bug-#36 behavior: only end_time clause
        assert captured.get("filter") == 'end_time>="2026-04-28T18:00:00Z"'

    def test_gmail_query_with_only_since_unchanged(self):
        adapter = _make_adapter()
        captured = {}

        list_call = MagicMock()
        list_call.execute.return_value = {"messages": []}
        messages = MagicMock()
        messages.list = MagicMock(side_effect=lambda **kw: (captured.update(kw), list_call)[1])
        users = MagicMock()
        users.messages.return_value = messages
        service = MagicMock()
        service.users.return_value = users

        with patch.object(adapter, "_gmail_service", return_value=service):
            import asyncio

            asyncio.run(
                adapter._list_gmail_messages("u@x.com", "2026-04-02T18:00:00Z", max_results=10)
            )

        # Identical to pre-Bug-#36: just `after:` clause
        assert captured.get("q") == "after:2026/04/02"


# --- TD-1 sub-piece 5: Drive document ingestion (fetch_documents) ---


class TestFetchDocumentsContract:
    """fetch_documents() returns Document-shaped dicts from Drive, paginated +
    filtered by modifiedTime, with file-type-aware content extraction."""

    @pytest.mark.asyncio
    async def test_rejects_unknown_kwarg(self):
        """Bug #36 discipline applies — strict params, no silent absorption."""
        adapter = _make_adapter()
        with pytest.raises(AdapterValidationError) as exc:
            await adapter.fetch_documents(folder="root")
        assert "folder" in str(exc.value)
        assert "since, until, user_emails, limit" in str(exc.value)

    @pytest.mark.asyncio
    async def test_returns_dicts_with_document_fields(self):
        """Each dict returned must have the fields Document entity requires
        plus external_ref (= drive_file_id) for fetch_new dedup."""
        adapter = _make_adapter()

        # Mock drive_service.files().list().execute() to return one file
        files_call = MagicMock()
        files_call.execute.return_value = {
            "files": [
                {
                    "id": "drive-id-abc",
                    "name": "Q4 Strategy.pdf",
                    "mimeType": "application/pdf",
                    "size": "12345",
                    "createdTime": "2026-04-15T10:00:00.000Z",
                    "modifiedTime": "2026-04-20T15:30:00.000Z",
                    "webViewLink": "https://drive.google.com/file/d/drive-id-abc/view",
                    "owners": [{"emailAddress": "owner@indemn.ai"}],
                    "trashed": False,
                }
            ]
        }
        files = MagicMock()
        files.list = MagicMock(return_value=files_call)
        service = MagicMock()
        service.files.return_value = files

        with patch.object(adapter, "_drive_service", return_value=service):
            results = await adapter.fetch_documents(user_emails=["u@x.com"])

        assert len(results) == 1
        d = results[0]
        # Required Document fields
        assert d["name"] == "Q4 Strategy.pdf"
        assert d["mime_type"] == "application/pdf"
        assert d["drive_file_id"] == "drive-id-abc"
        assert d["drive_url"] == "https://drive.google.com/file/d/drive-id-abc/view"
        assert d["file_size"] == 12345  # coerced int from Drive's string
        assert d["source"] == "google_drive"
        # external_ref = drive_file_id (so generic fetch_new dedup works)
        assert d["external_ref"] == "drive-id-abc"

    @pytest.mark.asyncio
    async def test_filters_by_since_via_modified_time(self):
        """`since` translates to `modifiedTime > '...'` in Drive's q syntax."""
        adapter = _make_adapter()
        captured = {}

        files_call = MagicMock()
        files_call.execute.return_value = {"files": []}
        files = MagicMock()
        files.list = MagicMock(side_effect=lambda **kw: (captured.update(kw), files_call)[1])
        service = MagicMock()
        service.files.return_value = files

        with patch.object(adapter, "_drive_service", return_value=service):
            await adapter.fetch_documents(
                user_emails=["u@x.com"],
                since="2026-04-01T00:00:00Z",
            )

        q = captured.get("q", "")
        assert "modifiedTime > '2026-04-01T00:00:00Z'" in q
        assert "trashed = false" in q  # always exclude trashed

    @pytest.mark.asyncio
    async def test_filters_by_until_via_modified_time(self):
        """`until` translates to `modifiedTime < '...'` AND-combined with since."""
        adapter = _make_adapter()
        captured = {}

        files_call = MagicMock()
        files_call.execute.return_value = {"files": []}
        files = MagicMock()
        files.list = MagicMock(side_effect=lambda **kw: (captured.update(kw), files_call)[1])
        service = MagicMock()
        service.files.return_value = files

        with patch.object(adapter, "_drive_service", return_value=service):
            await adapter.fetch_documents(
                user_emails=["u@x.com"],
                since="2026-04-01T00:00:00Z",
                until="2026-04-30T00:00:00Z",
            )

        q = captured.get("q", "")
        assert "modifiedTime > '2026-04-01T00:00:00Z'" in q
        assert "modifiedTime < '2026-04-30T00:00:00Z'" in q

    @pytest.mark.asyncio
    async def test_dedups_files_visible_to_multiple_users(self):
        """Same drive_file_id seen across multiple users → returned once."""
        adapter = _make_adapter()
        same_file = {
            "id": "shared-doc-1",
            "name": "Shared Document.gdoc",
            "mimeType": "application/vnd.google-apps.document",
            "size": "0",
            "createdTime": "2026-04-15T10:00:00.000Z",
            "modifiedTime": "2026-04-20T15:30:00.000Z",
            "webViewLink": "https://docs.google.com/document/d/shared-doc-1",
            "trashed": False,
        }

        files_call = MagicMock()
        files_call.execute.return_value = {"files": [same_file]}
        files = MagicMock()
        files.list = MagicMock(return_value=files_call)
        service = MagicMock()
        service.files.return_value = files
        # Mock export for Google Doc content extraction
        export_call = MagicMock()
        export_call.execute.return_value = b"doc content text"
        files.export = MagicMock(return_value=export_call)
        files.export_media = MagicMock(return_value=export_call)

        with patch.object(adapter, "_drive_service", return_value=service):
            results = await adapter.fetch_documents(
                user_emails=["a@x.com", "b@x.com", "c@x.com"]
            )

        assert len(results) == 1
        assert results[0]["drive_file_id"] == "shared-doc-1"

    @pytest.mark.asyncio
    async def test_paginates_via_page_token(self):
        """If files.list returns nextPageToken, the adapter calls again with pageToken."""
        adapter = _make_adapter()

        page1 = {
            "files": [
                {
                    "id": "f1",
                    "name": "File 1",
                    "mimeType": "application/pdf",
                    "size": "100",
                    "createdTime": "2026-04-15T10:00:00.000Z",
                    "modifiedTime": "2026-04-20T15:30:00.000Z",
                    "webViewLink": "https://drive.google.com/file/d/f1/view",
                    "trashed": False,
                }
            ],
            "nextPageToken": "tok-1",
        }
        page2 = {
            "files": [
                {
                    "id": "f2",
                    "name": "File 2",
                    "mimeType": "application/pdf",
                    "size": "200",
                    "createdTime": "2026-04-15T10:00:00.000Z",
                    "modifiedTime": "2026-04-21T15:30:00.000Z",
                    "webViewLink": "https://drive.google.com/file/d/f2/view",
                    "trashed": False,
                }
            ]
        }

        call_count = {"n": 0}

        def list_side_effect(**kw):
            call = MagicMock()
            call_count["n"] += 1
            if "pageToken" in kw and kw["pageToken"] == "tok-1":
                call.execute.return_value = page2
            else:
                call.execute.return_value = page1
            return call

        files = MagicMock()
        files.list = MagicMock(side_effect=list_side_effect)
        service = MagicMock()
        service.files.return_value = files

        with patch.object(adapter, "_drive_service", return_value=service):
            results = await adapter.fetch_documents(user_emails=["u@x.com"])

        assert len(results) == 2
        assert {r["drive_file_id"] for r in results} == {"f1", "f2"}
        assert call_count["n"] == 2  # paginated through both pages
