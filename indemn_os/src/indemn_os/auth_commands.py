"""Authentication CLI commands."""

import json
import os
from pathlib import Path

import httpx
import typer

auth_app = typer.Typer(
    name="auth",
    help="Authentication. Set INDEMN_SERVICE_TOKEN env var for service-token auth (skips login).",
)

TOKEN_FILE = Path.home() / ".indemn" / "credentials"


@auth_app.command("login")
def login(
    org: str = typer.Option(..., "--org", help="Organization slug"),
    email: str = typer.Option(..., "--email"),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True),
):
    """Log in and store access token."""
    base_url = os.environ.get("INDEMN_API_URL", "http://localhost:8000")
    with httpx.Client(base_url=base_url) as client:
        r = client.post(
            "/auth/login",
            json={
                "org_slug": org,
                "email": email,
                "password": password,
            },
        )
        if r.status_code != 200:
            typer.echo(f"Login failed: {r.text}", err=True)
            raise typer.Exit(1)
        data = r.json()

    if data.get("requires_mfa"):
        typer.echo("MFA required — use the UI to complete login")
        raise typer.Exit(1)

    token = data["access_token"]
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "access_token": token,
                "refresh_token": data.get("refresh_token", ""),
                "api_url": base_url,
                "org_slug": org,
                "email": email,
            }
        )
    )
    TOKEN_FILE.chmod(0o600)
    typer.echo(f"Logged in as {email} @ {org}")
    typer.echo(f"Token stored in {TOKEN_FILE}")


@auth_app.command("logout")
def logout():
    """Revoke session and remove stored token."""
    # Revoke server-side session if we have a token
    if TOKEN_FILE.exists():
        try:
            creds = json.loads(TOKEN_FILE.read_text())
            token = creds.get("access_token", "")
            if token:
                base_url = creds.get(
                    "api_url", os.environ.get("INDEMN_API_URL", "http://localhost:8000")
                )
                with httpx.Client(base_url=base_url) as client:
                    client.post(
                        "/auth/logout",
                        headers={"Authorization": f"Bearer {token}"},
                    )
        except Exception:
            pass  # Best-effort revocation — token file cleanup is the critical path
        TOKEN_FILE.unlink()
    typer.echo("Logged out")


@auth_app.command("token")
def show_token():
    """Show current token info."""
    import jwt as pyjwt

    # Check env first, then file
    token = os.environ.get("INDEMN_SERVICE_TOKEN")
    source = "env:INDEMN_SERVICE_TOKEN"

    if not token and TOKEN_FILE.exists():
        creds = json.loads(TOKEN_FILE.read_text())
        token = creds.get("access_token")
        source = str(TOKEN_FILE)

    if not token:
        typer.echo("No token found", err=True)
        raise typer.Exit(1)

    try:
        payload = pyjwt.decode(token, options={"verify_signature": False})
        typer.echo(f"Source: {source}")
        typer.echo(f"Actor: {payload.get('actor_id')}")
        typer.echo(f"Org: {payload.get('org_id')}")
        typer.echo(f"Roles: {payload.get('roles')}")
        from datetime import datetime, timezone

        exp = datetime.fromtimestamp(payload.get("exp", 0), tz=timezone.utc)
        typer.echo(f"Expires: {exp.isoformat()}")
    except Exception:
        typer.echo(f"Opaque token (not JWT): {token[:20]}...")
        typer.echo(f"Source: {source}")
