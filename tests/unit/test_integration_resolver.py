"""Tests for kernel.integration.resolver — Bug #45c.

Pins the contract that org-level integrations created without explicit
`access.roles` gating are reachable to any actor in the org.

Pre-fix: `access.roles: {$in: role_names}` against null `access` returned
no match silently, making the integration unreachable. Operator intent
when `access` is null/missing is "no gate, any actor in this org can
use it" — not "no access."

Post-fix: Step 3 query is a $or that matches either:
  (a) explicit role gate (`access.roles` non-empty + intersects actor roles)
  (b) no gate at all (access null/missing OR access.roles empty/missing)

Plus: when no integration matches but org has integrations of that
system_type, the error message tells the operator what to check.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from kernel.integration.adapter import AdapterNotFoundError
from kernel.integration.resolver import resolve_integration


@pytest.fixture
def org_id():
    return ObjectId()


@pytest.fixture
def actor_id():
    return ObjectId()


def _set_context(actor_id, org_id):
    """Set the contextvars used by the resolver. Returns a callable that
    resets them. ContextVars are read-only attributes so we can't mock
    `.get`; use the actual set/reset machinery."""
    from kernel.integration import resolver as r

    actor_token = r.current_actor_id.set(str(actor_id))
    org_token = r.current_org_id.set(org_id)

    def _reset():
        r.current_actor_id.reset(actor_token)
        r.current_org_id.reset(org_token)

    return _reset


@pytest.fixture
def mock_actor(actor_id):
    a = MagicMock()
    a.id = actor_id
    a.owner_actor_id = None
    a.role_ids = [ObjectId()]
    return a


@pytest.fixture
def mock_role():
    r = MagicMock()
    r.name = "team_member"
    return r


def _patch_models(personal=None, owner=None, org_match=None, org_count=0, actor=None, roles=()):
    """Patch the Beanie model lookups inside resolver. Returns a list of
    context managers to enter.

    Note: with `actor.owner_actor_id = None`, Step 2 is SKIPPED (no second
    find_one call). So the find_one side_effect needs personal + org_match
    only — the `owner` arg is unused unless the test sets actor.owner_actor_id.
    """
    from kernel.integration import resolver as r

    if actor is not None and getattr(actor, "owner_actor_id", None):
        integration_find_one = AsyncMock(side_effect=[personal, owner, org_match])
    else:
        integration_find_one = AsyncMock(side_effect=[personal, org_match])
    integration_find = MagicMock()
    count_query = MagicMock()
    count_query.count = AsyncMock(return_value=org_count)
    integration_find.return_value = count_query
    actor_get = AsyncMock(return_value=actor)
    role_find = MagicMock()
    role_to_list = AsyncMock(return_value=list(roles))
    role_find.return_value.to_list = role_to_list

    return [
        patch.object(r.Integration, "find_one", new=integration_find_one),
        patch.object(r.Integration, "find", new=integration_find),
        patch.object(r.Actor, "get", new=actor_get),
        patch.object(r.Role, "find", new=role_find),
    ]


class TestStep3OrgIntegrationGating:
    """The new $or query in Step 3."""

    @pytest.mark.asyncio
    async def test_explicit_role_gate_match(self, mock_actor, mock_role, actor_id, org_id):
        """access.roles=['team_member'] + actor has 'team_member' → match."""
        match = MagicMock()
        match.name = "Slack Workspace"
        reset = _set_context(actor_id, org_id)
        patches = _patch_models(
            personal=None, owner=None, org_match=match, actor=mock_actor, roles=[mock_role],
        )
        for p in patches:
            p.start()
        try:
            result = await resolve_integration("messaging")
            assert result is match
        finally:
            for p in patches:
                p.stop()
            reset()

    @pytest.mark.asyncio
    async def test_null_access_treated_as_no_gate(
        self, mock_actor, mock_role, actor_id, org_id
    ):
        """access: null on the integration → reachable. Bug #45c primary fix.

        We can't easily verify the exact $or shape from outside; the test ensures
        the integration is returned when find_one yields it, regardless of what
        the caller's roles are. The real validation that the $or works is the
        live deploy."""
        match = MagicMock()
        match.access = None
        reset = _set_context(actor_id, org_id)
        patches = _patch_models(
            personal=None, owner=None, org_match=match, actor=mock_actor, roles=[mock_role],
        )
        for p in patches:
            p.start()
        try:
            result = await resolve_integration("messaging")
            assert result is match
        finally:
            for p in patches:
                p.stop()
            reset()

    @pytest.mark.asyncio
    async def test_no_match_with_existing_integrations_clear_error(
        self, mock_actor, mock_role, actor_id, org_id
    ):
        """When find_one returns None but the org has 2 integrations of this
        system_type, the error message hints at status/access mismatch."""
        reset = _set_context(actor_id, org_id)
        patches = _patch_models(
            personal=None, owner=None, org_match=None, org_count=2,
            actor=mock_actor, roles=[mock_role],
        )
        for p in patches:
            p.start()
        try:
            with pytest.raises(AdapterNotFoundError) as exc:
                await resolve_integration("messaging")
            msg = str(exc.value)
            assert "2 found" in msg
            assert "status=active" in msg
            assert "access.roles" in msg
        finally:
            for p in patches:
                p.stop()
            reset()

    @pytest.mark.asyncio
    async def test_no_integrations_at_all_create_hint(
        self, mock_actor, mock_role, actor_id, org_id
    ):
        """When org has zero integrations of this system_type, error tells
        operator to create one (existing behavior)."""
        reset = _set_context(actor_id, org_id)
        patches = _patch_models(
            personal=None, owner=None, org_match=None, org_count=0,
            actor=mock_actor, roles=[mock_role],
        )
        for p in patches:
            p.start()
        try:
            with pytest.raises(AdapterNotFoundError) as exc:
                await resolve_integration("messaging")
            msg = str(exc.value)
            assert "Create one with: indemn integration create" in msg
        finally:
            for p in patches:
                p.stop()
            reset()


class TestResolverQueryShape:
    """Pin the actual $or shape of the Step 3 query — easier to verify than
    behavior since the find_one mock is too coarse."""

    @pytest.mark.asyncio
    async def test_step3_query_uses_or_with_null_access_branches(
        self, mock_actor, mock_role, actor_id, org_id
    ):
        """The Integration.find_one call for Step 3 should issue a $or query
        with branches for: explicit access.roles match, null access, missing
        access, missing access.roles, empty access.roles list."""
        import contextlib

        from kernel.integration import resolver as r

        captured: list = []

        async def fake_find_one(query):
            captured.append(query)
            # First two calls (personal, owner) miss; third (org) returns None
            # so we provoke the AdapterNotFoundError path. We just need to
            # capture the third query shape.
            return None

        cnt = MagicMock()
        cnt.count = AsyncMock(return_value=0)
        find_mock = MagicMock(return_value=cnt)
        rf_mock = MagicMock()
        rf_mock.return_value.to_list = AsyncMock(return_value=[mock_role])

        reset = _set_context(actor_id, org_id)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(r.Integration, "find_one", new=fake_find_one))
            stack.enter_context(patch.object(r.Integration, "find", new=find_mock))
            stack.enter_context(patch.object(r.Actor, "get", new=AsyncMock(return_value=mock_actor)))
            stack.enter_context(patch.object(r.Role, "find", new=rf_mock))

            try:
                await resolve_integration("messaging")
            except AdapterNotFoundError:
                pass
        reset()

        # With actor.owner_actor_id=None, Step 2 is skipped → Step 3 is the
        # SECOND find_one call (Step 1 personal first, then Step 3 org).
        assert len(captured) == 2
        step3 = captured[1]
        assert "$or" in step3
        or_branches = step3["$or"]
        # Look for each expected branch
        assert any(b == {"access.roles": {"$in": ["team_member"]}} for b in or_branches)
        assert any(b == {"access": None} for b in or_branches)
        assert any(b == {"access": {"$exists": False}} for b in or_branches)
        assert any(b == {"access.roles": {"$exists": False}} for b in or_branches)
        assert any(b == {"access.roles": []} for b in or_branches)
