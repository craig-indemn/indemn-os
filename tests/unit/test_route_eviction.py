"""Tests for kernel.api.registration._evict_routes_for_prefix.

Bug #29 (os-bugs-and-shakeout): replacing an entity definition's fields
left the OLD route closures in `app.router.routes`. FastAPI's
`include_router` appends, doesn't replace, so new routes were registered
ALONGSIDE the old ones — and FastAPI matches the first registered route,
so write operations kept validating against the stale class.

These tests cover the helper that fixes that: `_evict_routes_for_prefix`
removes every route whose path matches the prefix or starts with prefix+"/"
before `include_router` re-adds them. The full entity-class roundtrip is
covered by integration tests; this file pins the eviction logic itself
(the part most likely to silently get the matching wrong as the codebase
evolves).
"""

from types import SimpleNamespace

from fastapi import FastAPI

from kernel.api.registration import _evict_routes_for_prefix


def _route(path: str):
    """Build a route stand-in. Real Starlette routes have many attributes;
    only `path` is read by the eviction helper, so a SimpleNamespace is
    enough to test the logic without instantiating real routes."""
    return SimpleNamespace(path=path)


def _app_with_routes(*paths: str) -> FastAPI:
    """Build an app with `app.router.routes` populated from the given paths."""
    app = FastAPI()
    # FastAPI/Starlette pre-populate a few default routes (docs, openapi);
    # replace wholesale so tests assert against exactly the seeded set.
    app.router.routes = [_route(p) for p in paths]
    return app


def test_evicts_exact_prefix_match():
    """A route at exactly the prefix path (no trailing component) is evicted.

    Some FastAPI route registrations end up with a path that equals the
    prefix exactly — guard against missing those.
    """
    app = _app_with_routes("/api/companys")
    n = _evict_routes_for_prefix(app, "/api/companys")
    assert n == 1
    assert len(app.router.routes) == 0


def test_evicts_routes_under_prefix():
    """All routes mounted under `/api/companys/` are evicted."""
    app = _app_with_routes(
        "/api/companys/",
        "/api/companys/{entity_id}",
        "/api/companys/{entity_id}/transition",
        "/api/companys/bulk",
    )
    n = _evict_routes_for_prefix(app, "/api/companys")
    assert n == 4
    assert len(app.router.routes) == 0


def test_does_not_evict_unrelated_routes():
    """Routes for other entities or system endpoints survive eviction."""
    app = _app_with_routes(
        "/api/companys/",
        "/api/companys/{entity_id}",
        "/api/contacts/",
        "/api/_meta/entities",
        "/health",
        "/auth/login",
    )
    n = _evict_routes_for_prefix(app, "/api/companys")
    assert n == 2
    surviving = sorted(r.path for r in app.router.routes)
    assert surviving == [
        "/api/_meta/entities",
        "/api/contacts/",
        "/auth/login",
        "/health",
    ]


def test_does_not_evict_prefix_lookalikes():
    """`/api/companys2` and `/api/companys-internal` start with `/api/companys`
    but are NOT under it (no `/` separator after the prefix). They must
    survive eviction. This guards against a naive `startswith(prefix)`
    implementation."""
    app = _app_with_routes(
        "/api/companys/",
        "/api/companys/{entity_id}",
        "/api/companys2/",  # different entity, prefix lookalike
        "/api/companys-internal/audit",  # also different
    )
    n = _evict_routes_for_prefix(app, "/api/companys")
    assert n == 2
    surviving = sorted(r.path for r in app.router.routes)
    assert surviving == ["/api/companys-internal/audit", "/api/companys2/"]


def test_returns_zero_when_no_match():
    """A prefix with no matching routes returns 0 and leaves the app intact."""
    app = _app_with_routes("/api/contacts/", "/health")
    n = _evict_routes_for_prefix(app, "/api/companys")
    assert n == 0
    assert len(app.router.routes) == 2


def test_handles_route_without_path_attribute():
    """Some Starlette `Mount` objects lack a `path` attribute. The helper
    must skip them (not crash) and leave them in place."""
    app = FastAPI()
    app.router.routes = [
        _route("/api/companys/"),
        SimpleNamespace(),  # no `path` attribute — Mount-style
        _route("/api/companys/{entity_id}"),
    ]
    n = _evict_routes_for_prefix(app, "/api/companys")
    assert n == 2
    # The unattributed route survives.
    assert len(app.router.routes) == 1


def test_re_registration_replaces_old_routes_end_to_end():
    """After eviction + include_router, only the new closures are reachable.

    This is the actual bug-29 scenario: same entity registered twice with
    different stage enums. Before the fix, the OLD closure handled requests.
    After the fix, the NEW closure handles them.
    """
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    app = FastAPI()

    # First registration — "v1" handler.
    router_v1 = APIRouter(prefix="/api/companys", tags=["Company"])

    @router_v1.get("/")
    def list_v1():
        return {"version": "v1"}

    app.include_router(router_v1)

    # Sanity: first registration works.
    client = TestClient(app)
    assert client.get("/api/companys/").json() == {"version": "v1"}

    # Second registration — "v2" handler — without eviction would not win
    # over the v1 closure (FastAPI matches the first match).
    router_v2 = APIRouter(prefix="/api/companys", tags=["Company"])

    @router_v2.get("/")
    def list_v2():
        return {"version": "v2"}

    _evict_routes_for_prefix(app, "/api/companys")
    app.include_router(router_v2)

    # After eviction + re-include, the new handler wins.
    assert client.get("/api/companys/").json() == {"version": "v2"}
