"""POST /sessions body parsing + required-fields validation (AI-407 §10.3.1).

Task 2.26: parse JSON body, return 400 on malformed JSON or missing
required deployment_id. First step in the §10.3.1 validation chain —
must complete before any downstream work (Origin / JWT / Deployment load
etc.) so we reject cheap-to-detect bad input early.

Error response shape per §10.3.1 table:
- 400 (malformed JSON / missing deployment_id) → {"error": "validation_error",
  "details": "<reason>"}
"""

import pytest


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from harness.app import app
    return TestClient(app)


class TestSessionsBodyParsing:
    def test_invalid_json_returns_400(self, client):
        response = client.post(
            "/sessions",
            data="not-valid-json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "validation_error"
        # details surface enough info for the SDK / operator to diagnose
        assert "details" in body

    def test_missing_deployment_id_returns_400(self, client):
        response = client.post("/sessions", json={})
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "validation_error"
        details = body.get("details", "")
        assert "deployment_id" in details.lower()

    def test_empty_body_returns_400(self, client):
        response = client.post(
            "/sessions",
            data="",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "validation_error"

    def test_non_dict_body_returns_400(self, client):
        """Body must be a JSON object — arrays, strings, numbers are rejected."""
        response = client.post(
            "/sessions",
            data='["deployment_id", "x"]',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "validation_error"

    def test_valid_body_passes_parsing_stage(self, client):
        """A well-formed body with deployment_id passes parsing — downstream
        validation may still reject it (Origin / JWT etc), but the parsing
        step itself doesn't 400.

        Currently returns 501 (next task in chain not yet implemented);
        the assertion is that we get past the 400 from this task.
        """
        response = client.post(
            "/sessions",
            json={"deployment_id": "dep_test", "dynamic_params": {}},
        )
        assert response.status_code != 400  # parsing passed
