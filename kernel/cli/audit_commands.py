"""Audit CLI commands — hash chain verification.

Verifies the changes collection hash chain integrity,
reporting any breaks that indicate tampering or corruption.
"""

import typer

from kernel.cli.client import CLIClient

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
