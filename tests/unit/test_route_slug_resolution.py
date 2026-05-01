"""Tests for `_route_slug_for` — Bug #39.

Pins the resolution order:
  1. `entity_cls._collection_name` (domain entities, honors --collection-name)
  2. `entity_cls.Settings.name` (kernel Beanie entities)
  3. naive `entity_name.lower() + 's'` (last-resort fallback)

Pre-fix: register_entity_routes always used (3), so explicit
`--collection-name` overrides applied to MongoDB but not the API route.
Post-fix: route prefix matches the actual collection name.
"""

from kernel.api.registration import _route_slug_for


class _DomainEntity:
    """Mimics a dynamically-created domain entity class — has `_collection_name`."""

    _collection_name = "review_items"


class _KernelEntity:
    """Mimics a kernel Beanie entity — has Settings.name."""

    class Settings:
        name = "actors"


class _BareEntity:
    """No collection_name, no Settings — falls back to naive plural."""

    pass


class _BothSet:
    """Both attributes set; _collection_name takes precedence."""

    _collection_name = "explicit_override"

    class Settings:
        name = "from_settings"


def test_domain_entity_uses_collection_name():
    assert _route_slug_for("ReviewItem", _DomainEntity) == "review_items"


def test_kernel_entity_uses_settings_name():
    assert _route_slug_for("Actor", _KernelEntity) == "actors"


def test_bare_entity_falls_back_to_naive_plural():
    assert _route_slug_for("Foo", _BareEntity) == "foos"


def test_collection_name_takes_precedence_over_settings():
    """When both are set, _collection_name wins (operator override is most explicit)."""
    assert _route_slug_for("Whatever", _BothSet) == "explicit_override"


def test_handles_empty_collection_name_string():
    """An empty `_collection_name` should fall through to the next option,
    not be returned literally as the route prefix."""

    class _EmptyCN:
        _collection_name = ""

        class Settings:
            name = "fallback"

    assert _route_slug_for("X", _EmptyCN) == "fallback"
