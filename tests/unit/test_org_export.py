"""Unit tests for org export/import serialization helpers."""

from datetime import datetime, timezone

from bson import ObjectId

from kernel.api.org_lifecycle import _serialize_bson


class TestSerializeBson:
    def test_objectid_to_string(self):
        oid = ObjectId()
        assert _serialize_bson(oid) == str(oid)

    def test_datetime_to_iso(self):
        dt = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert _serialize_bson(dt) == dt.isoformat()

    def test_nested_dict(self):
        oid = ObjectId()
        data = {"name": "test", "owner_id": oid, "meta": {"ref": oid}}
        result = _serialize_bson(data)
        assert result["name"] == "test"
        assert result["owner_id"] == str(oid)
        assert result["meta"]["ref"] == str(oid)

    def test_list_with_objectids(self):
        oid1 = ObjectId()
        oid2 = ObjectId()
        result = _serialize_bson([oid1, "keep", oid2])
        assert result == [str(oid1), "keep", str(oid2)]

    def test_primitive_passthrough(self):
        assert _serialize_bson("hello") == "hello"
        assert _serialize_bson(42) == 42
        assert _serialize_bson(True) is True
        assert _serialize_bson(None) is None
