"""Tests for Bug #27 — `created_by` auto-populate on insert.

Pre-fix: `save_tracked()` didn't touch `created_by`. Both
`KernelBaseEntity` and `DomainBaseEntity` declared
`created_by: Optional[str] = None`, callers didn't pass it explicitly,
and the field stayed null on EVERY entity. Live evidence at fix time:
all 446 Company records in dev had `created_by: null`, even though the
changes-collection record for each HAD an actor_id. The information was
there; the entity field just wasn't capturing it. Forensic gap when
looking at an entity directly.

Fix: in `save_tracked_impl`, on insert (`is_new=True`), populate
`entity.created_by` from `_resolve_created_by(actor_id)`. The resolver
prefers `current_effective_actor_id` (the associate the harness is
running as — same convention as the changes-collection's
`effective_actor_id` field, Bug #22) and falls back to `actor_id`
(the authenticated session identity) for human-driven mutations.

Caller-provided values (seed data, migrations, imported records) are
NOT overwritten — only fields that are still None at save time get the
auto-populate.

The save_tracked_impl integration is a one-line patch; these tests
pin the resolver's behavior directly + the no-overwrite contract via
inspection of the surrounding code.
"""

from contextvars import copy_context

import pytest

from kernel.context import current_effective_actor_id
from kernel.entity.save import _resolve_created_by


# --- The resolver ---


def _run_with_effective(effective):
    """Run resolver inside a contextvars context where
    current_effective_actor_id is pre-set."""
    ctx = copy_context()

    def _inner():
        if effective is not None:
            current_effective_actor_id.set(effective)
        return _resolve_created_by("token-actor")

    return ctx.run(_inner)


def test_resolver_falls_back_to_actor_id_when_no_effective_actor():
    """Human-driven mutation (no harness, no effective actor): the
    authenticated session's actor_id becomes created_by."""
    out = _run_with_effective(None)
    assert out == "token-actor"


def test_resolver_prefers_effective_actor_id_when_set():
    """Harness running as an associate: associate's id wins. The token
    owner (actor_id) is Platform Admin for every harness call, which
    would make per-associate forensics impossible if we didn't prefer
    the effective actor."""
    associate_id = "69e7c8b3bca4880e93ad5576"
    out = _run_with_effective(associate_id)
    assert out == associate_id


def test_resolver_falls_back_when_effective_is_empty_string():
    """Defensive: an empty-string effective_actor_id is falsy; falls back
    to actor_id rather than capturing an empty value."""
    out = _run_with_effective("")
    assert out == "token-actor"


# --- The save_tracked integration: source-pin so the wiring stays correct ---


def test_save_tracked_calls_resolver_only_on_insert():
    """The integration is a 3-line patch in save_tracked_impl. Pin the
    shape via source inspection so a future refactor can't silently move
    the populate to the wrong branch (e.g. updates) without the test
    catching it."""
    from pathlib import Path

    src = Path("/Users/home/Repositories/indemn-os/kernel/entity/save.py").read_text()
    # The populate must be guarded by `is_new` (not on update)
    assert "if is_new and hasattr(entity, \"created_by\") and entity.created_by is None:" in src, (
        "Bug #27 regression: created_by populate must be gated on is_new + "
        "field-exists + currently-None to preserve caller-provided values"
    )
    # And must call the resolver
    assert "entity.created_by = _resolve_created_by(actor_id)" in src, (
        "Bug #27 regression: created_by populate must use _resolve_created_by "
        "so the effective_actor_id preference is preserved"
    )


def test_resolver_signature_takes_actor_id():
    """The resolver's contract: takes the authenticated actor_id, returns
    the most-specific identity available (effective_actor_id ?? actor_id).
    Pin the signature so callers can rely on it."""
    import inspect

    sig = inspect.signature(_resolve_created_by)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "actor_id"
    assert params[0].annotation is str
