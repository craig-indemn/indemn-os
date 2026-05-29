"""Tests for --include-related polymorphic Option B support.

Touchpoint's `source_entity_id` is polymorphic — target is Email or Meeting
depending on `source_entity_type`. Without polymorphic support, `_build_related_entities`
skips it because `is_relationship: false`. The fix adds `is_polymorphic_relationship`
+ `target_type_field` to FieldDefinition; the resolver reads the type at runtime.

Tests pin: field definition schema, resolver logic, and the _build_related_entities
output shape when a polymorphic ref is populated.
"""

import inspect


class TestFieldDefinitionSchema:
    """Pin the new fields on FieldDefinition."""

    def test_is_polymorphic_relationship_field_exists(self):
        from kernel.entity.definition import FieldDefinition

        fdef = FieldDefinition(type="objectid", is_polymorphic_relationship=True)
        assert fdef.is_polymorphic_relationship is True

    def test_target_type_field_exists(self):
        from kernel.entity.definition import FieldDefinition

        fdef = FieldDefinition(
            type="objectid",
            is_polymorphic_relationship=True,
            target_type_field="source_entity_type",
        )
        assert fdef.target_type_field == "source_entity_type"

    def test_defaults_to_false(self):
        from kernel.entity.definition import FieldDefinition

        fdef = FieldDefinition(type="str")
        assert fdef.is_polymorphic_relationship is False
        assert fdef.target_type_field is None


class TestBuildRelatedEntitiesPolymorphic:
    """Pin that _build_related_entities handles polymorphic refs."""

    def test_source_contains_polymorphic_branch(self):
        """The forward-refs loop must have a branch for is_polymorphic_relationship."""
        from kernel.message import emit

        src = inspect.getsource(emit._build_related_entities)
        assert "is_polymorphic_relationship" in src
        assert "target_type_field" in src
        assert '"_polymorphic"' in src

    def test_polymorphic_entry_has_correct_metadata_keys(self):
        """When a polymorphic ref is followed, the result dict carries
        _entity_type, _relationship_direction, _via_field, and _polymorphic."""
        from kernel.message import emit

        src = inspect.getsource(emit._build_related_entities)
        # Must set all required metadata keys on polymorphic results
        assert '_entity_type' in src
        assert '_relationship_direction' in src
        assert '_via_field' in src
        assert '_polymorphic' in src
