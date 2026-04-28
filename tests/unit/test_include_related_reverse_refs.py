"""Tests for `_build_related_entities` — the helper backing
`GET /api/{collection}/{id}?include_related=true&depth=N`.

Two bugs were live before this fix:

1. The API handler in `kernel/api/registration.py` read
   `context.get("related_entities", [])` from `_build_context`'s output —
   but `_build_context` keys related entities by lowercase entity-name
   (e.g. `"company"`, `"contact"`), never under `"related_entities"`. The
   `_related` field on the response was therefore ALWAYS `[]`, even for
   entities with forward relationship fields populated.

2. Even if (1) had been correct, `_build_context` only follows FORWARD
   relationship fields (fields on this entity with `is_relationship=true`).
   Reverse refs — entities elsewhere whose `relationship_target` points at
   THIS entity's type — were invisible. For Touchpoint, this meant
   `Email.touchpoint -> Touchpoint` (the back-pointer the customer-system
   pipeline writes) couldn't be navigated from the Touchpoint side.

The new helper walks BOTH directions and returns a flat list with three
metadata keys per entry — `_entity_type`, `_relationship_direction`
("forward" | "reverse"), `_via_field` — so consumers (the customer-system
Intelligence Extractor, the constellation queries from `vision.md` §5)
can tell how each related entity is related and traverse from there.

`_build_context` is left untouched: the watch-emit path consumes its
dict-keyed-by-target shape for message enrichment, and the entity-local
constraint on watch evaluation (vision-map §5.3) means we should not be
expanding cross-entity work in the emit transaction. The API path has
its own helper.

These tests pin:
  * depth <= 1 returns []
  * forward refs surface with direction="forward"
  * reverse refs surface with direction="reverse"
  * mixed-direction entity returns both
  * self-relationships exclude the entity itself
  * polymorphic / non-relationship fields are NOT followed
  * unknown relationship_target (not in ENTITY_REGISTRY) is skipped, not raised
  * forward field with null value is skipped
  * the metadata fields don't collide with entity fields by accident
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from kernel.message.emit import _build_related_entities


# --- Fixtures: tiny stand-ins that look like entities + EntityDefinitions ---


def _field(
    is_relationship=False,
    relationship_target=None,
):
    return SimpleNamespace(
        is_relationship=is_relationship,
        relationship_target=relationship_target,
    )


def _defn(name, fields):
    return SimpleNamespace(name=name, fields=fields)


def _entity(type_name, **fields):
    """Stand-in for a saved entity. `model_dump(by_alias=True)` returns the
    fields dict; `id` is taken from the `_id` key if present, else fields["id"].

    The function under test reads `type(entity).__name__`, so we build a real
    dynamic class with that name (SimpleNamespace can't have its `__class__`
    reassigned).
    """
    obj_id = fields.pop("_id", None) or fields.get("id")
    snapshot = dict(fields)

    def _model_dump(self, by_alias=True):
        return dict(snapshot, _id=obj_id) if by_alias else dict(snapshot)

    cls = type(type_name, (), {"model_dump": _model_dump})
    e = cls()
    e.id = obj_id
    for k, v in fields.items():
        setattr(e, k, v)
    return e


class _FakeQuery:
    """Stand-in for find_scoped(...).to_list() chain."""

    def __init__(self, results):
        self._results = results

    async def to_list(self):
        return list(self._results)


def _cls_with_results(results, captured_query=None):
    """Build an entity_cls stand-in whose find_scoped() returns `results`,
    and whose .get(id) returns the first result (or None)."""
    cls = SimpleNamespace()

    def find_scoped(query):
        if captured_query is not None:
            captured_query.append(query)
        return _FakeQuery(results)

    cls.find_scoped = find_scoped

    async def _get(_id):
        return results[0] if results else None

    cls.get = _get
    return cls


# --- Test cases ---


@pytest.mark.asyncio
async def test_depth_one_returns_empty_list():
    """Depth 1 means just-the-entity. _related stays []."""
    e = _entity("Touchpoint", id=ObjectId())
    out = await _build_related_entities(e, depth=1)
    assert out == []


@pytest.mark.asyncio
async def test_depth_zero_returns_empty_list():
    e = _entity("Touchpoint", id=ObjectId())
    out = await _build_related_entities(e, depth=0)
    assert out == []


@pytest.mark.asyncio
async def test_forward_ref_surfaces_with_forward_direction():
    """An Email with company=<id> should produce a forward-ref entry."""
    company_id = ObjectId()
    email_id = ObjectId()
    email = _entity("Email", id=email_id, company=company_id, subject="hi")
    company = _entity("Company", id=company_id, name="Acme")

    own_defn = _defn(
        "Email",
        {
            "subject": _field(),
            "company": _field(is_relationship=True, relationship_target="Company"),
        },
    )
    ed = SimpleNamespace(
        find_one=AsyncMock(return_value=own_defn),
        find_all=lambda: _FakeQuery([own_defn]),  # only Email defn — no reverse refs
    )
    company_cls = _cls_with_results([company])
    registry = {"Company": company_cls, "Email": _cls_with_results([])}

    with patch("kernel.entity.definition.EntityDefinition", ed), patch(
        "kernel.db.ENTITY_REGISTRY", registry
    ):
        out = await _build_related_entities(email, depth=2)

    assert len(out) == 1
    assert out[0]["_entity_type"] == "Company"
    assert out[0]["_relationship_direction"] == "forward"
    assert out[0]["_via_field"] == "company"
    assert out[0]["name"] == "Acme"


@pytest.mark.asyncio
async def test_reverse_ref_surfaces_with_reverse_direction():
    """A Company with no forward refs of its own but TWO Emails pointing at it
    should return both Emails as reverse refs."""
    company_id = ObjectId()
    company = _entity("Company", id=company_id, name="Acme")

    company_defn = _defn("Company", {"name": _field()})  # no relationship fields
    email_defn = _defn(
        "Email",
        {
            "subject": _field(),
            "company": _field(is_relationship=True, relationship_target="Company"),
        },
    )
    inbound_emails = [
        _entity("Email", id=ObjectId(), subject="one", company=company_id),
        _entity("Email", id=ObjectId(), subject="two", company=company_id),
    ]
    captured = []
    email_cls = _cls_with_results(inbound_emails, captured_query=captured)
    registry = {"Company": _cls_with_results([]), "Email": email_cls}

    ed = SimpleNamespace(
        find_one=AsyncMock(return_value=company_defn),
        find_all=lambda: _FakeQuery([company_defn, email_defn]),
    )

    with patch("kernel.entity.definition.EntityDefinition", ed), patch(
        "kernel.db.ENTITY_REGISTRY", registry
    ):
        out = await _build_related_entities(company, depth=2)

    assert len(out) == 2
    for entry in out:
        assert entry["_entity_type"] == "Email"
        assert entry["_relationship_direction"] == "reverse"
        assert entry["_via_field"] == "company"
    # Reverse query must filter by the inbound field equaling our entity's id
    assert captured == [{"company": company_id}]


@pytest.mark.asyncio
async def test_mixed_forward_and_reverse():
    """A Touchpoint with company=<id> (forward) AND two Emails pointing at it
    (reverse via Email.touchpoint) returns three entries: 1 forward + 2 reverse."""
    tp_id = ObjectId()
    company_id = ObjectId()
    tp = _entity("Touchpoint", id=tp_id, company=company_id)
    company = _entity("Company", id=company_id, name="Acme")

    tp_defn = _defn(
        "Touchpoint",
        {
            "company": _field(is_relationship=True, relationship_target="Company"),
        },
    )
    email_defn = _defn(
        "Email",
        {
            "subject": _field(),
            "touchpoint": _field(is_relationship=True, relationship_target="Touchpoint"),
        },
    )
    company_defn = _defn("Company", {"name": _field()})

    inbound_emails = [
        _entity("Email", id=ObjectId(), subject="A", touchpoint=tp_id),
        _entity("Email", id=ObjectId(), subject="B", touchpoint=tp_id),
    ]
    registry = {
        "Touchpoint": _cls_with_results([]),
        "Company": _cls_with_results([company]),
        "Email": _cls_with_results(inbound_emails),
    }
    ed = SimpleNamespace(
        find_one=AsyncMock(return_value=tp_defn),
        find_all=lambda: _FakeQuery([tp_defn, company_defn, email_defn]),
    )

    with patch("kernel.entity.definition.EntityDefinition", ed), patch(
        "kernel.db.ENTITY_REGISTRY", registry
    ):
        out = await _build_related_entities(tp, depth=2)

    forward = [e for e in out if e["_relationship_direction"] == "forward"]
    reverse = [e for e in out if e["_relationship_direction"] == "reverse"]
    assert len(forward) == 1
    assert forward[0]["_entity_type"] == "Company"
    assert forward[0]["_via_field"] == "company"
    assert len(reverse) == 2
    for r in reverse:
        assert r["_entity_type"] == "Email"
        assert r["_via_field"] == "touchpoint"


@pytest.mark.asyncio
async def test_self_relationship_excludes_self_from_reverse():
    """A Proposal with `supersedes -> Proposal` self-relationship: the OLDER
    proposal we point at appears as a forward ref. The current Proposal must
    NOT appear in its own reverse list (only OTHER Proposals that point AT it
    do). Verifies the `$ne` filter on self-relationship reverse queries."""
    p_now = ObjectId()
    p_old = ObjectId()
    p_newer = ObjectId()

    proposal = _entity("Proposal", id=p_now, supersedes=p_old, version=2)
    older = _entity("Proposal", id=p_old, version=1)
    newer = _entity("Proposal", id=p_newer, supersedes=p_now, version=3)

    proposal_defn = _defn(
        "Proposal",
        {
            "version": _field(),
            "supersedes": _field(is_relationship=True, relationship_target="Proposal"),
        },
    )

    captured = []

    proposal_cls = SimpleNamespace()

    async def _get(_id):
        if _id == p_old:
            return older
        if _id == p_newer:
            return newer
        return None

    proposal_cls.get = _get

    def find_scoped(query):
        captured.append(query)
        return _FakeQuery([newer])  # only the newer one matches "supersedes=p_now AND _id != p_now"

    proposal_cls.find_scoped = find_scoped
    registry = {"Proposal": proposal_cls}
    ed = SimpleNamespace(
        find_one=AsyncMock(return_value=proposal_defn),
        find_all=lambda: _FakeQuery([proposal_defn]),
    )

    with patch("kernel.entity.definition.EntityDefinition", ed), patch(
        "kernel.db.ENTITY_REGISTRY", registry
    ):
        out = await _build_related_entities(proposal, depth=2)

    forward = [e for e in out if e["_relationship_direction"] == "forward"]
    reverse = [e for e in out if e["_relationship_direction"] == "reverse"]
    # forward: the older one this proposal supersedes
    assert len(forward) == 1
    assert forward[0]["version"] == 1
    # reverse: the newer one that supersedes us — but NOT us
    assert len(reverse) == 1
    assert reverse[0]["version"] == 3
    # The query must exclude self via _id $ne
    assert captured == [{"supersedes": p_now, "_id": {"$ne": p_now}}]


@pytest.mark.asyncio
async def test_non_relationship_field_is_not_followed():
    """A field with is_relationship=false (e.g. Touchpoint's polymorphic
    Option B `source_entity_id`) should NEVER trigger a load — even if its
    value happens to be an ObjectId."""
    tp_id = ObjectId()
    pretend_target = ObjectId()
    tp = _entity(
        "Touchpoint",
        id=tp_id,
        source_entity_type="Email",
        source_entity_id=pretend_target,  # ObjectId-looking, but is_relationship=False
    )

    tp_defn = _defn(
        "Touchpoint",
        {
            "source_entity_type": _field(),  # plain field
            "source_entity_id": _field(is_relationship=False),  # explicitly NOT followed
        },
    )

    sentinel_cls = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("should not be loaded")),
        find_scoped=lambda q: _FakeQuery([]),
    )
    registry = {"Touchpoint": sentinel_cls, "Email": sentinel_cls}
    ed = SimpleNamespace(
        find_one=AsyncMock(return_value=tp_defn),
        find_all=lambda: _FakeQuery([tp_defn]),
    )

    with patch("kernel.entity.definition.EntityDefinition", ed), patch(
        "kernel.db.ENTITY_REGISTRY", registry
    ):
        out = await _build_related_entities(tp, depth=2)

    # No relationship fields → nothing forward, nothing reverse
    assert out == []


@pytest.mark.asyncio
async def test_unknown_relationship_target_is_skipped():
    """If `relationship_target` names an entity that isn't in ENTITY_REGISTRY
    (e.g. a stale entity def whose target was removed), don't crash —
    silently skip. Reverse direction handles the same way (source class
    missing from registry → skipped)."""
    e_id = ObjectId()
    e = _entity("WeirdEntity", id=e_id, points_at=ObjectId())

    own_defn = _defn(
        "WeirdEntity",
        {
            "points_at": _field(is_relationship=True, relationship_target="GhostEntity"),
        },
    )
    ed = SimpleNamespace(
        find_one=AsyncMock(return_value=own_defn),
        find_all=lambda: _FakeQuery([own_defn]),
    )

    with patch("kernel.entity.definition.EntityDefinition", ed), patch(
        "kernel.db.ENTITY_REGISTRY", {}
    ):
        out = await _build_related_entities(e, depth=2)

    assert out == []  # Skipped — no exception raised


@pytest.mark.asyncio
async def test_forward_field_with_null_value_is_skipped():
    """`Email.company` is Optional. An Email with company=None should not
    attempt to load anything."""
    e = _entity("Email", id=ObjectId(), subject="orphan", company=None)
    own_defn = _defn(
        "Email",
        {
            "subject": _field(),
            "company": _field(is_relationship=True, relationship_target="Company"),
        },
    )

    sentinel_cls = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("should not be loaded")),
        find_scoped=lambda q: _FakeQuery([]),
    )
    registry = {"Company": sentinel_cls, "Email": sentinel_cls}
    ed = SimpleNamespace(
        find_one=AsyncMock(return_value=own_defn),
        find_all=lambda: _FakeQuery([own_defn]),
    )

    with patch("kernel.entity.definition.EntityDefinition", ed), patch(
        "kernel.db.ENTITY_REGISTRY", registry
    ):
        out = await _build_related_entities(e, depth=2)

    assert out == []


@pytest.mark.asyncio
async def test_metadata_keys_overwrite_collision_safely():
    """If a related entity happens to have a field literally named `_entity_type`
    (extremely unlikely — leading-underscore Pydantic names are reserved by
    convention), our metadata write wins. This is the design choice: the
    metadata keys are response-shape, not part of the entity contract."""
    company_id = ObjectId()
    email_id = ObjectId()
    email = _entity("Email", id=email_id, company=company_id)
    company = _entity(
        "Company",
        id=company_id,
        name="Acme",
        # Pretend a malicious / sloppy field name. Verify our overlay wins.
        _entity_type="WrongValue",
    )

    own_defn = _defn(
        "Email",
        {"company": _field(is_relationship=True, relationship_target="Company")},
    )
    company_cls = _cls_with_results([company])
    registry = {"Company": company_cls, "Email": _cls_with_results([])}
    ed = SimpleNamespace(
        find_one=AsyncMock(return_value=own_defn),
        find_all=lambda: _FakeQuery([own_defn]),
    )

    with patch("kernel.entity.definition.EntityDefinition", ed), patch(
        "kernel.db.ENTITY_REGISTRY", registry
    ):
        out = await _build_related_entities(email, depth=2)

    assert len(out) == 1
    # Metadata write happens AFTER serialization — our value wins
    assert out[0]["_entity_type"] == "Company"
