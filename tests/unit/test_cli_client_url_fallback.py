"""Tests for CLIClient base_url + token resolution.

Pre-fix: CLIClient defaulted base_url to `http://localhost:8000` and never
consulted the credentials file's `api_url` field. Running `indemn auth login
--org _platform --email ...` set api_url=https://api.os.indemn.ai in the
credentials file but `indemn <anything>` afterward routed to localhost,
producing cryptic 401 errors with no obvious cause. The credentials file
already had the right URL — the client just wasn't reading it.

Fix: 3-tier resolution for both fields:
  1. environment variable (INDEMN_API_URL / INDEMN_SERVICE_TOKEN)
  2. ~/.indemn/credentials  (set by `indemn auth login`)
  3. fallback default       (localhost / empty token)

These tests pin all three tiers for both fields plus the precedence ordering.
"""

import json
from pathlib import Path

import pytest
from indemn_os.client import CLIClient


@pytest.fixture
def temp_credentials(tmp_path, monkeypatch):
    """Create a fake credentials file at $HOME/.indemn/credentials."""
    home = tmp_path
    creds_dir = home / ".indemn"
    creds_dir.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("INDEMN_API_URL", raising=False)
    monkeypatch.delenv("INDEMN_SERVICE_TOKEN", raising=False)
    return creds_dir / "credentials"


def test_env_var_wins_over_credentials_file_for_base_url(temp_credentials, monkeypatch):
    temp_credentials.write_text(json.dumps({
        "access_token": "from-creds-file",
        "api_url": "https://from-creds-file.example.com",
    }))
    monkeypatch.setenv("INDEMN_API_URL", "https://env-wins.example.com")
    client = CLIClient()
    assert client.base_url == "https://env-wins.example.com"


def test_credentials_file_used_when_env_var_unset(temp_credentials):
    """The original bug: env var unset, credentials had api_url, but the
    client defaulted to localhost anyway. Post-fix the credentials file's
    api_url is the fallback."""
    temp_credentials.write_text(json.dumps({
        "access_token": "tok",
        "api_url": "https://api.os.indemn.ai",
    }))
    client = CLIClient()
    assert client.base_url == "https://api.os.indemn.ai"


def test_localhost_default_when_no_env_no_creds(monkeypatch, tmp_path):
    """Bare environment, no creds file: dev-friendly localhost default
    (matches pre-fix behavior so existing local-dev workflows are
    unchanged)."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("INDEMN_API_URL", raising=False)
    monkeypatch.delenv("INDEMN_SERVICE_TOKEN", raising=False)
    client = CLIClient()
    assert client.base_url == "http://localhost:8000"
    assert client.token == ""


def test_service_token_env_wins_over_credentials_for_token(temp_credentials, monkeypatch):
    """Harness containers set INDEMN_SERVICE_TOKEN — must take precedence
    over any credentials file that might also be present."""
    temp_credentials.write_text(json.dumps({
        "access_token": "user-token",
        "api_url": "https://api.os.indemn.ai",
    }))
    monkeypatch.setenv("INDEMN_SERVICE_TOKEN", "service-token")
    client = CLIClient()
    assert client.token == "service-token"


def test_credentials_token_used_when_env_unset(temp_credentials):
    """Standard developer workflow: `indemn auth login` writes the token
    into credentials, subsequent CLI calls pick it up."""
    temp_credentials.write_text(json.dumps({
        "access_token": "user-jwt",
        "api_url": "https://api.os.indemn.ai",
    }))
    client = CLIClient()
    assert client.token == "user-jwt"


def test_corrupted_credentials_file_falls_through_gracefully(temp_credentials):
    """A junk creds file should not crash the CLI — fall through to env / defaults."""
    temp_credentials.write_text("not valid json {{{")
    client = CLIClient()
    # No env, junk file → defaults
    assert client.base_url == "http://localhost:8000"
    assert client.token == ""


def test_missing_credentials_file_falls_through(monkeypatch, tmp_path):
    """No credentials file at all: defaults, no exception."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("INDEMN_API_URL", raising=False)
    monkeypatch.delenv("INDEMN_SERVICE_TOKEN", raising=False)
    # tmp_path has no .indemn dir
    client = CLIClient()
    assert client.base_url == "http://localhost:8000"
    assert client.token == ""
