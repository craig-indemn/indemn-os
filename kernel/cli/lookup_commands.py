"""Lookup CLI commands — CRUD + CSV import. [G-60]

Lookups are key-value tables used by the rules engine for classification.
Bulk-importable via CSV, maintained by non-technical users.
"""

import csv

import typer

from kernel.cli.client import CLIClient, render

lookup_app = typer.Typer(name="lookup", help="Lookup table management")


@lookup_app.command("list")
def list_lookups():
    """List all lookups."""
    client = CLIClient()
    result = client.get("/api/lookups/")
    render(result, "table")


@lookup_app.command("get")
def get_lookup(name: str):
    """Get a lookup by name."""
    client = CLIClient()
    result = client.get(f"/api/lookups/{name}")
    render(result, "json")


@lookup_app.command("import")
def import_lookup(
    name: str,
    from_csv: str = typer.Option(..., "--from-csv", help="Path to CSV file"),
    org: str = typer.Option(None, "--org"),
):
    """Import lookup data from CSV. First column is key, second is value."""
    data = {}
    with open(from_csv) as f:
        reader = csv.DictReader(f)
        for row in reader:
            cols = list(row.values())
            if len(cols) >= 2:
                data[cols[0]] = cols[1]

    client = CLIClient()
    response = client.post(
        "/api/lookups",
        json={"name": name, "data": data},
    )
    render(response, "json")
    typer.echo(f"Imported {len(data)} entries into lookup '{name}'")
