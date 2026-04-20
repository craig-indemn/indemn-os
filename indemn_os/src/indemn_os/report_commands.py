"""Report CLI commands — accuracy comparison for parallel run validation."""

import typer

from indemn_os.client import CLIClient

report_app = typer.Typer(name="report", help="Reporting and comparison tools")


@report_app.command("compare")
def compare_report(
    old_system_export: str = typer.Option(
        ..., "--old-system-export", help="Path to CSV/JSON from old system"
    ),
    os_entity: str = typer.Option(
        ..., "--os-entity", help="Entity type in the OS to compare against"
    ),
    match_field: str = typer.Option(
        "external_id",
        "--match-field",
        help="Field to join old and new records",
    ),
    compare_fields: str = typer.Option(
        ...,
        "--compare-fields",
        help="Comma-separated fields to compare",
    ),
    output: str = typer.Option(
        "comparison-report.csv",
        "--output",
    ),
):
    """Compare old system decisions against OS entity data.

    Joins records by match_field, compares specified fields,
    and reports matches/mismatches.
    """
    # Load old system data
    old_data = _load_export(old_system_export)
    fields = [f.strip() for f in compare_fields.split(",")]

    client = CLIClient()
    result = client.post(
        "/api/_platform/report/compare",
        json={
            "old_data": old_data,
            "entity_type": os_entity,
            "match_field": match_field,
            "compare_fields": fields,
        },
    )

    # Write output in CSV format
    _write_csv(result, output, match_field, fields)

    # Summary
    summary = result.get("summary", {})
    typer.echo("Comparison complete:")
    typer.echo(f"  Total records: {summary.get('total', 0)}")
    typer.echo(f"  Matched: {summary.get('matched', 0)}")
    typer.echo(f"  Mismatched: {summary.get('mismatched', 0)}")
    typer.echo(f"  Missing in OS: {summary.get('missing_in_os', 0)}")
    typer.echo(f"  Extra in OS: {summary.get('extra_in_os', 0)}")
    typer.echo(f"  Output: {output}")


def _write_csv(result: dict, path: str, match_field: str, fields: list):
    """Write comparison results as CSV."""
    import csv

    comparisons = result.get("comparisons", [])
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        # Header
        header = [match_field, "status"]
        for field in fields:
            header.extend([f"{field}_old", f"{field}_new", f"{field}_match"])
        writer.writerow(header)
        # Rows
        for comp in comparisons:
            row = [comp.get(match_field, ""), comp.get("status", "")]
            field_data = comp.get("fields", {})
            for field in fields:
                fd = field_data.get(field, {})
                row.extend(
                    [
                        fd.get("old", ""),
                        fd.get("new", ""),
                        fd.get("match", ""),
                    ]
                )
            writer.writerow(row)


def _load_export(path: str) -> list[dict]:
    """Load old system export from CSV or JSON."""
    import csv
    from pathlib import Path

    p = Path(path)
    if p.suffix == ".csv":
        with open(p) as f:
            reader = csv.DictReader(f)
            return list(reader)
    else:
        import orjson

        with open(p, "rb") as f:
            data = orjson.loads(f.read())
        return data if isinstance(data, list) else [data]
