"""Unit tests for event metadata building."""

from kernel.message.event_metadata import build_event_metadata


class FakeEntity:
    _pending_transition = None


def test_method_metadata():
    entity = FakeEntity()
    meta = build_event_metadata(entity, method="classify", changes=[])
    assert meta == {"method": "classify"}


def test_transition_metadata():
    entity = FakeEntity()
    entity._pending_transition = {"from": "received", "to": "triaging", "reason": None}
    meta = build_event_metadata(entity, method=None, changes=[])
    assert meta["state_transition"] == {"from": "received", "to": "triaging", "reason": None}
    # Cleared after capture
    assert entity._pending_transition is None


def test_fields_changed_metadata():
    entity = FakeEntity()
    changes = [
        {"field": "status", "old_value": "a", "new_value": "b"},
        {"field": "name", "old_value": "x", "new_value": "y"},
    ]
    meta = build_event_metadata(entity, method=None, changes=changes)
    assert meta["fields_changed"] == ["status", "name"]


def test_combined_metadata():
    entity = FakeEntity()
    entity._pending_transition = {"from": "a", "to": "b", "reason": "test"}
    changes = [{"field": "status", "old_value": "a", "new_value": "b"}]
    meta = build_event_metadata(entity, method="transition", changes=changes)
    assert "method" in meta
    assert "state_transition" in meta
    assert "fields_changed" in meta


def test_empty_metadata():
    entity = FakeEntity()
    meta = build_event_metadata(entity, method=None, changes=[])
    assert meta == {}
