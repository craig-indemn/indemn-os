"""Pin the surgical-drain behavior of `indemn queue drain --entity-id`.

Pre-fix, `queue drain` only supported role-wide oldest-first re-emission.
That's the wrong tool for surgical drain: when the parked queue holds work
for many different entities, an operator wanting to process ONE specific
entity's message had to either drain the whole role (chaotic), or fall
back to direct MongoDB writes (no audit trail).

`--entity-id` adds the surgical option: filter parked messages by the
target entity_id before re-emitting, so the next-pending message in the
queue is exactly the one for that entity.

These tests pin:
- CLI passes `entity_id` in the POST body when --entity-id is provided
- CLI omits `entity_id` when not provided (backward-compat)
- API server-side filter includes entity_id in the find query
- API server-side rejects invalid entity_id with 400
"""

from pathlib import Path

# --- CLI side ---


def test_drain_cli_passes_entity_id_when_provided(monkeypatch):
    """When --entity-id is provided, the request body includes it."""
    # The CLI module imports httpx; we stub the CLIClient.post to capture the body
    from indemn_os import queue_commands

    captured = {}

    class FakeClient:
        def post(self, path, json=None, params=None):
            captured["path"] = path
            captured["body"] = json
            return {"reemitted": 1, "remaining_parked": 0, "role": "touchpoint_synthesizer"}

    monkeypatch.setattr(queue_commands, "CLIClient", lambda: FakeClient())

    queue_commands.drain_parked(
        role="touchpoint_synthesizer",
        limit=1,
        entity_id="6a04f110152462cd04502f17",
    )

    assert captured["path"] == "/api/queue/drain"
    assert captured["body"]["entity_id"] == "6a04f110152462cd04502f17"
    assert captured["body"]["role"] == "touchpoint_synthesizer"
    assert captured["body"]["limit"] == 1


def test_drain_cli_omits_entity_id_when_not_provided(monkeypatch):
    """Backward-compat: --entity-id is optional. When not provided,
    the body omits entity_id (doesn't send None)."""
    from indemn_os import queue_commands

    captured = {}

    class FakeClient:
        def post(self, path, json=None, params=None):
            captured["body"] = json
            return {"reemitted": 5, "remaining_parked": 12, "role": "email_classifier"}

    monkeypatch.setattr(queue_commands, "CLIClient", lambda: FakeClient())

    queue_commands.drain_parked(role="email_classifier", limit=20, entity_id=None)

    assert "entity_id" not in captured["body"]
    assert captured["body"]["role"] == "email_classifier"


# --- API server-side ---
# The drain_parked endpoint is async, MongoDB-coupled, and auth-protected.
# Pin the new behavior via source inspection rather than instantiating a
# FastAPI test client (which would need a full Beanie + Motor setup).


def _read_queue_routes_source():
    return (
        Path(__file__).resolve().parents[2]
        / "kernel" / "api" / "queue_routes.py"
    ).read_text()


def test_api_drain_reads_entity_id_from_body():
    """The API extracts entity_id from the request body."""
    src = _read_queue_routes_source()
    assert 'data.get("entity_id")' in src, (
        "drain_parked must read entity_id from the request body"
    )


def test_api_drain_adds_entity_id_to_filter():
    """When entity_id is provided, the MongoDB filter narrows to it."""
    src = _read_queue_routes_source()
    assert 'filter_query["entity_id"] = ObjectId(entity_id_raw)' in src, (
        "drain_parked must add entity_id to the find filter as an ObjectId"
    )


def test_api_drain_rejects_invalid_entity_id():
    """Bad ObjectId hex must surface as a 400, not a 500.

    Without this, an operator typo silently dispatches the request,
    MongoDB ObjectId construction raises, and the error becomes an
    opaque server error."""
    src = _read_queue_routes_source()
    # Pin the validation: must wrap ObjectId() in try/except and raise 400
    assert (
        'try:' in src
        and 'ObjectId(entity_id_raw)' in src
        and "HTTPException(\n                400" in src
    ), (
        "drain_parked must validate entity_id and raise 400 on bad input"
    )


def test_api_drain_preserves_role_filter_with_entity_id():
    """entity_id is an additional narrow, NOT a replacement for the role
    filter. A bad caller passing entity_id=X without role should still hit
    the role-required 400."""
    src = _read_queue_routes_source()
    # Role check comes BEFORE entity_id check
    role_check_idx = src.index('if not role_name:')
    entity_id_idx = src.index('entity_id_raw = data.get("entity_id")')
    assert role_check_idx < entity_id_idx, (
        "Role required-check must precede entity_id processing"
    )
