"""Lookup CLI commands — CRUD + CSV import.

Lookups are key-value tables used by the rules engine for classification.
Bulk-importable via CSV, maintained by non-technical users.
"""

import csv

import typer

from kernel.cli.client import CLIClient, render

lookup_app = typer.Typer(name="lookup", help="Lookup table management")


@lookup_app.command("list")
def list_lookups(fmt: str = typer.Option("table", "--format")):
    """List all lookups."""
    client = CLIClient()
    result = client.get("/api/lookups/")
    render(result, fmt)


@lookup_app.command("get")
def get_lookup(name: str, fmt: str = typer.Option("json", "--format")):
    """Get a lookup by name."""
    client = CLIClient()
    result = client.get(f"/api/lookups/{name}")
    render(result, fmt)


@lookup_app.command("create")
def create_lookup(
    name: str = typer.Option(..., "--name", help="Lookup name"),
    data: str = typer.Option(..., "--data", help="JSON key-value data"),
):
    """Create a new lookup table from inline JSON."""
    import orjson

    client = CLIClient()
    response = client.post(
        "/api/lookups",
        json={"name": name, "data": orjson.loads(data)},
    )
    render(response, "json")
    typer.echo(f"Created lookup: {name}")


@lookup_app.command("import")
def import_lookup(
    name: str,
    from_csv: str = typer.Option(..., "--from-csv", help="Path to CSV file"),
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
