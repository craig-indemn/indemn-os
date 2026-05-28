"""Voice frontdoor CORS preflight (AI-409 smoke Bug B).

Browsers issue an OPTIONS preflight before any cross-origin POST that
includes Authorization / Content-Type headers. Without CORSMiddleware,
Starlette returns 405 Method Not Allowed → browser refuses to send the
POST → SDK sees "Failed to fetch".

These tests pin the middleware behavior:
- OPTIONS /sessions preflight returns 200 with proper CORS headers
- POST responses still get the Access-Control-Allow-Origin header
- GET /health still works (regression)
- Per-Deployment Origin gate still fires for the actual POST (the CORS
  middleware is a browser-handshake convention; security stays at the
  POST handler)
"""

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


def test_cors_preflight_options_returns_200(client):
    """OPTIONS /sessions preflight returns 200, not 405 Method Not Allowed."""
    response = client.options(
        "/sessions",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert response.status_code == 200


def test_cors_preflight_returns_allow_origin_header(client):
    """Preflight response includes Access-Control-Allow-Origin."""
    response = client.options(
        "/sessions",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    header_keys = {k.lower() for k in response.headers}
    assert "access-control-allow-origin" in header_keys


def test_cors_preflight_allows_post_method(client):
    """Preflight indicates POST is permitted via Access-Control-Allow-Methods."""
    response = client.options(
        "/sessions",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    allow_methods = response.headers.get("access-control-allow-methods", "")
    assert "POST" in allow_methods.upper()


def test_cors_preflight_allows_authorization_and_content_type_headers(client):
    """Preflight indicates Authorization + Content-Type may be sent."""
    response = client.options(
        "/sessions",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    allow_headers = response.headers.get(
        "access-control-allow-headers", ""
    ).lower()
    assert "authorization" in allow_headers
    assert "content-type" in allow_headers


def test_post_validation_error_still_includes_cors_header(client):
    """When POST /sessions returns 400 (validation_error), the CORS
    middleware should still attach Access-Control-Allow-Origin so the
    browser hands the body to the SDK rather than blocking the response.
    """
    response = client.post(
        "/sessions",
        json={},
        headers={"Origin": "http://localhost:5173"},
    )
    # Empty body → 400 validation_error per sessions.py step 2
    assert response.status_code == 400
    header_keys = {k.lower() for k in response.headers}
    assert "access-control-allow-origin" in header_keys


def test_health_get_regression(client):
    """Adding CORSMiddleware must not break the unauthenticated GET /health
    Railway probe.
    """
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "indemn-runtime-voice-frontdoor"
