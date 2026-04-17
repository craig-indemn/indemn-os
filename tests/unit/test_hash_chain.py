"""Unit tests for the hash chain tamper-evidence mechanism."""

from datetime import datetime, timezone

from bson import ObjectId

from kernel.changes.collection import FieldChange
from kernel.changes.hash_chain import compute_hash


class FakeRecord:
    """Lightweight stand-in for ChangeRecord to test hashing without Beanie init."""

    def __init__(
        self,
        entity_type="Submission",
        entity_id=None,
        change_type="update",
        actor_id="actor-1",
        timestamp=None,
        changes=None,
        previous_hash=None,
    ):
        self.entity_type = entity_type
        self.entity_id = entity_id or ObjectId()
        self.change_type = change_type
        self.actor_id = actor_id
        self.timestamp = timestamp or datetime.now(timezone.utc)
        self.changes = changes or []
        self.previous_hash = previous_hash
        self.current_hash = None


def test_compute_hash_deterministic():
    """Same content → same hash."""
    record = FakeRecord()
    h1 = compute_hash(record)
    h2 = compute_hash(record)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_different_content_different_hash():
    r1 = FakeRecord(entity_type="Submission")
    r2 = FakeRecord(entity_type="Email")
    assert compute_hash(r1) != compute_hash(r2)


def test_hash_chain_links():
    """Each record's hash includes the previous hash, linking them."""
    r1 = FakeRecord(previous_hash=None)
    r1.current_hash = compute_hash(r1)

    r2 = FakeRecord(previous_hash=r1.current_hash)
    r2.current_hash = compute_hash(r2)

    # Verify: r2's hash depends on r1's hash
    assert r2.previous_hash == r1.current_hash

    # Tamper: change r1's content after the fact
    r1_tampered = FakeRecord(previous_hash=None, actor_id="tampered-actor")
    r1_tampered_hash = compute_hash(r1_tampered)
    assert r1_tampered_hash != r1.current_hash  # Chain broken


def test_hash_includes_changes():
    shared_ts = datetime.now(timezone.utc)
    shared_eid = ObjectId()
    r1 = FakeRecord(changes=[], timestamp=shared_ts, entity_id=shared_eid)
    r2 = FakeRecord(
        changes=[FieldChange(field="status", old_value="received", new_value="triaging")],
        timestamp=shared_ts,
        entity_id=shared_eid,
    )
    assert compute_hash(r1) != compute_hash(r2)


def test_hash_includes_previous_hash():
    r1 = FakeRecord(previous_hash=None)
    r2 = FakeRecord(previous_hash="abc123")
    # Same content except previous_hash → different hash
    r2.entity_id = r1.entity_id
    r2.timestamp = r1.timestamp
    assert compute_hash(r1) != compute_hash(r2)


def test_hash_survives_datetime_timezone_roundtrip():
    """MongoDB returns naive datetimes; Python creates aware ones.
    Hash must be identical regardless of timezone awareness."""
    aware_ts = datetime(2026, 4, 17, 12, 30, 45, 123000, tzinfo=timezone.utc)
    naive_ts = datetime(2026, 4, 17, 12, 30, 45, 123000)  # Same time, no tzinfo

    r_aware = FakeRecord(timestamp=aware_ts)
    r_naive = FakeRecord(timestamp=naive_ts)
    r_naive.entity_id = r_aware.entity_id
    assert compute_hash(r_aware) == compute_hash(r_naive)


def test_hash_survives_datetime_in_changes():
    """FieldChange old_value/new_value with datetimes must hash consistently
    regardless of timezone awareness (write-time aware, read-time naive)."""
    aware_dt = datetime(2026, 1, 15, 8, 0, 0, 500000, tzinfo=timezone.utc)
    naive_dt = datetime(2026, 1, 15, 8, 0, 0, 500000)
    shared_ts = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
    shared_eid = ObjectId()

    r_write = FakeRecord(
        changes=[FieldChange(field="due_at", old_value=aware_dt, new_value=aware_dt)],
        timestamp=shared_ts,
        entity_id=shared_eid,
    )
    r_read = FakeRecord(
        changes=[FieldChange(field="due_at", old_value=naive_dt, new_value=naive_dt)],
        timestamp=shared_ts,
        entity_id=shared_eid,
    )
    assert compute_hash(r_write) == compute_hash(r_read)


def test_hash_truncates_microseconds():
    """MongoDB stores milliseconds. Sub-millisecond digits must not affect hash."""
    ts_micro = datetime(2026, 4, 17, 12, 0, 0, 123456, tzinfo=timezone.utc)
    ts_milli = datetime(2026, 4, 17, 12, 0, 0, 123000, tzinfo=timezone.utc)

    r1 = FakeRecord(timestamp=ts_micro)
    r2 = FakeRecord(timestamp=ts_milli)
    r2.entity_id = r1.entity_id
    assert compute_hash(r1) == compute_hash(r2)
