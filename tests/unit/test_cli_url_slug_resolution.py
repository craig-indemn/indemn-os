"""Tests for Bug #48 — CLI URL slug honors meta `collection` field.

`indemn_os/src/indemn_os/main.py::_register_entity_commands` and
`indemn_os/src/indemn_os/bulk_commands.py::register_bulk_commands` build
URLs like `/api/{slug}/...` for every CLI subcommand. Pre-fix, both used
`slug = entity_name.lower()` and concatenated `s` — naive plural. Now
they read `meta["collection"]` (populated by the kernel meta endpoint via
`_route_slug_for`) so the URL matches the actual route registered in
`kernel/api/registration.py`.

Pinning the URL templates via source inspection rather than spinning up
the full Typer app — the failure mode this guards against (regressing
back to `s` concatenation) is a one-line edit; source-level pins catch it
without test-environment overhead.
"""

import inspect

from indemn_os import bulk_commands, main as indemn_main


def test_main_register_entity_commands_uses_meta_collection():
    """Pin: `_register_entity_commands` derives `slug` from `meta["collection"]`,
    NOT from `name.lower() + "s"`. Without this the CLI 404s on every
    entity whose `collection_name != name.lower() + "s"` (e.g. SlackMessage)."""
    src = inspect.getsource(indemn_main._register_entity_commands)
    # The new resolution: meta.get("collection") with naive-plural fallback
    assert 'meta.get("collection")' in src, (
        "CLI must read collection from meta endpoint per Bug #48"
    )
    # Pin the fallback shape — keeps the CLI working against older API
    # instances that haven't deployed the meta-endpoint fix yet
    assert 'cli_name + "s"' in src or "cli_name + 's'" in src


def test_main_url_templates_use_slug_directly_not_s_concat():
    """Pin: URL templates use `f"/api/{slug}/..."` not `f"/api/{slug}s/..."`.
    The slug IS the plural collection name now — concatenating `s` would
    double-pluralize (e.g. `slack_messagess`)."""
    src = inspect.getsource(indemn_main._register_entity_commands)
    # No `{slug}s` patterns left
    assert "{slug}s" not in src, (
        "All URL templates must use {slug} directly — slug is the URL "
        "collection from meta, no `+s` concat"
    )
    # And canonical `/api/{slug}/` template should appear
    assert '/api/{slug}/' in src


def test_bulk_commands_url_templates_use_slug_directly():
    """Same pin applied to bulk_commands.py — bulk routes are at
    `/api/{collection}/bulk`, not `/api/{name.lower()}s/bulk`."""
    src = inspect.getsource(bulk_commands)
    assert "{slug}s" not in src
    assert "/api/{slug}/bulk" in src


def test_bulk_commands_accepts_url_slug_param():
    """Pin: register_bulk_commands accepts `url_slug` kwarg sourced from the
    meta endpoint's `collection` field. Without this kwarg, bulk URLs would
    use naive plural and 404 on the same SlackMessage class as Bug #48."""
    sig = inspect.signature(bulk_commands.register_bulk_commands)
    assert "url_slug" in sig.parameters


def test_cli_subcommand_name_stays_singular():
    """Pin: the Typer subcommand name uses `cli_name` (singular,
    `name.lower()`), not the URL slug. Operators invoke
    `indemn slackmessage list`, not `indemn slack_messages list` —
    the public CLI verb is decoupled from the URL collection."""
    src = inspect.getsource(indemn_main._register_entity_commands)
    assert "parent.add_typer(entity_app, name=cli_name)" in src
