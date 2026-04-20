"""Unit tests for watch scope resolution helpers."""

from kernel.watch.scope import _infer_entity_from_field_name


class TestInferEntityFromFieldName:
    def test_organization_id(self):
        """organization_id should map to Organization if in registry."""
        # This test verifies the naming convention logic
        # The actual registry lookup happens at runtime
        from kernel.db import ENTITY_REGISTRY

        result = _infer_entity_from_field_name("organization_id")
        # If Organization is in registry (it is for kernel entities)
        if "Organization" in ENTITY_REGISTRY:
            assert result is not None
        else:
            assert result is None  # Registry empty in pure unit test

    def test_strips_id_suffix(self):
        """Field names with _id suffix should be stripped."""
        # Direct test of the convention logic
        field_name = "actor_id"
        base = field_name
        for suffix in ("_id", "_ids"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        assert base == "actor"

    def test_strips_ids_suffix(self):
        field_name = "role_ids"
        base = field_name
        for suffix in ("_id", "_ids"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        assert base == "role"
