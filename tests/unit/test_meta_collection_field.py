"""Tests for Bug #48 — `/api/_meta/entities` must include `collection`.

The CLI client uses the meta endpoint to auto-register entity subcommands.
Pre-fix the list endpoint omitted the URL slug entirely; the CLI fell back
to naive `name.lower() + "s"` and 404'd on entities whose `--collection-name`
diverged from the naive plural (e.g. SlackMessage → slack_messages per
Bug #15 inflect engine, OR via explicit operator override per Bug #39).

The detail endpoint already returned `collection` but used a broken
`cls.Settings.name if hasattr(cls, "Settings") else entity_name.lower() + "s"`
which fell back to naive plural for domain entities (no Beanie Settings;
they store collection_name on `_collection_name`).

Both list and detail endpoints now use `_route_slug_for(name, cls)` —
identical logic to `kernel/api/registration.py::register_entity_routes`,
guaranteeing route + meta agreement.
"""

import inspect
import re

import kernel.api.meta as meta_module


def test_list_endpoint_imports_route_slug_for():
    """Pin: meta.py imports `_route_slug_for`. Without this the list endpoint
    can't compute the collection slug consistently with route registration."""
    src = inspect.getsource(meta_module)
    assert "from kernel.api.registration import _route_slug_for" in src


def test_list_endpoint_includes_collection_field():
    """Pin: the per-entity dict returned by `get_entity_metadata` includes
    a `collection` key sourced from `_route_slug_for`. The pre-fix omission
    was the proximate cause of `indemn slackmessage list` returning 404."""
    src = inspect.getsource(meta_module.get_entity_metadata)
    assert '"collection"' in src
    assert "_route_slug_for(name, cls)" in src


def test_detail_endpoint_uses_route_slug_for():
    """Pin: the detail endpoint uses `_route_slug_for(entity_name, cls)`,
    not the broken `cls.Settings.name if hasattr(cls, "Settings")` fallback
    (which fell through to naive plural for every domain entity)."""
    src = inspect.getsource(meta_module.get_entity_detail_metadata)
    assert "_route_slug_for(entity_name, cls)" in src
    # The pre-fix fallback chain must be gone from the actual return statement.
    # (A reference in a comment explaining the prior behavior is fine — match
    # only un-commented lines.)
    for line in src.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"'):
            continue
        assert 'Settings.name' not in line, f"Pre-fix code path still present: {line!r}"


def test_route_slug_for_domain_entity_with_collection_name():
    """End-to-end: a domain entity class with `_collection_name` resolves
    to that name. This is the SlackMessage case — `collection_name=
    "slack_messages"` set at entity creation time, and the meta endpoint
    must surface that to the CLI client."""
    from kernel.api.registration import _route_slug_for

    class _SlackMessageMimic:
        _collection_name = "slack_messages"

    assert _route_slug_for("SlackMessage", _SlackMessageMimic) == "slack_messages"
