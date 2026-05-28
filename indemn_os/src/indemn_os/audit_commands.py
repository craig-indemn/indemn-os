"""Audit CLI commands — hash chain verification.

Verifies the changes collection hash chain integrity,
reporting any breaks that indicate tampering or corruption.
"""

import typer

from indemn_os.client import CLIClient

audit_app = typer.Typer(name="audit", help="Audit and integrity verification")


@audit_app.command("verify")
def verify_hash_chain(
    org: str = typer.Option(None, "--org"),
    entity_type: str = typer.Option(None, "--entity-type"),
    limit: int = 1000,
):
    """Verify the changes collection hash chain integrity.
    Reports any breaks in the chain."""
    client = CLIClient()
    params = {"limit": limit}
    if org:
        params["org"] = org
    if entity_type:
        params["entity_type"] = entity_type
    result = client.get("/api/_platform/audit/verify", params=params)
    if result["chain_valid"]:
        typer.echo(f"Hash chain verified: {result['records_checked']} records, no breaks")
    else:
        typer.echo(f"CHAIN BROKEN at record {result['break_at']}:")
        typer.echo(f"  Expected: {result['expected_hash']}")
        typer.echo(f"  Found:    {result['actual_hash']}")


@audit_app.command("completeness-boundary")
def completeness_boundary():
    """Show the audit-completeness boundary (Session-35 D2).

    The boundary = min(timestamp) across create-type ChangeRecords with a
    non-empty changes array. Entities created BEFORE this timestamp have
    incomplete audit (no per-field FieldChange entries on their create record)
    and are skipped by Stage C eval reconstruction (sub-piece 12 D-J).

    Returns `null` when no qualifying records exist (pre-Stage-A-A2-deploy).
    Cached per kernel process — re-derives on each process restart.
    """
    client = CLIClient()
    result = client.get("/api/_platform/audit/completeness-boundary")
    if result.get("pre_stage_a"):
        typer.echo("Audit-completeness boundary: not set (pre-Stage-A — no qualifying records)")
    else:
        typer.echo(f"Audit-completeness boundary: {result['boundary']}")
        typer.echo("Entities created at or after this timestamp have complete per-field audit.")
