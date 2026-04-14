"""Organization management commands — clone, diff, export, import, deploy."""

from pathlib import Path

import typer

from kernel.cli.client import CLIClient, render

org_app = typer.Typer(name="org", help="Organization management")


@org_app.command("clone")
def clone_org(
    source: str,
    as_name: str = typer.Option(..., "--as"),
    include_data: bool = False,
):
    """Clone an org's configuration into a new org.

    Copies: entity definitions, skills, rules, lookups, roles, watches,
    associate configs, capability activations, integration configs (no secrets).
    Does NOT copy: entity instances, messages, changes, sessions, attentions.
    """
    client = CLIClient()
    result = client.post(
        "/api/_platform/org/clone",
        json={
            "source_org_slug": source,
            "target_org_name": as_name,
            "include_data": include_data,
        },
    )
    typer.echo(f"Cloned {source} → {result['target_org_slug']} ({result['items_copied']} items)")


@org_app.command("diff")
def diff_orgs(org_a: str, org_b: str):
    """Show configuration differences between two orgs."""
    client = CLIClient()
    result = client.get(
        "/api/_platform/org/diff",
        params={"org_a": org_a, "org_b": org_b},
    )
    for diff in result.get("differences", []):
        typer.echo(f"  {diff['type']:20s} {diff['name']:30s} {diff['change']}")
    typer.echo(f"\n{len(result.get('differences', []))} differences found")


@org_app.command("export")
def export_org(
    org_slug: str,
    output: str = typer.Option(".", "--output"),
):
    """Export org configuration to YAML files."""
    import yaml

    client = CLIClient()
    result = client.get("/api/_platform/org/export", params={"org": org_slug})
    out = Path(output) / org_slug
    out.mkdir(parents=True, exist_ok=True)
    for category, items in result.items():
        cat_dir = out / category
        cat_dir.mkdir(exist_ok=True)
        for name, data in items.items():
            with open(cat_dir / f"{name}.yaml", "w") as f:
                yaml.dump(data, f, default_flow_style=False)
    typer.echo(f"Exported to {out}/")


@org_app.command("import")
def import_org(
    from_dir: str = typer.Option(..., "--from"),
    as_name: str = typer.Option(..., "--as"),
):
    """Import org configuration from exported YAML files."""
    import yaml

    client = CLIClient()
    config = {}
    for cat_dir in Path(from_dir).iterdir():
        if cat_dir.is_dir():
            config[cat_dir.name] = {}
            for f in cat_dir.glob("*.yaml"):
                with open(f) as fh:
                    config[cat_dir.name][f.stem] = yaml.safe_load(fh)
    result = client.post(
        "/api/_platform/org/import",
        json={"target_org_name": as_name, "config": config},
    )
    typer.echo(f"Imported into {result['org_slug']} ({result['items_imported']} items)")


@org_app.command("deploy")
def deploy_org(
    from_org: str = typer.Option(..., "--from-org"),
    to_org: str = typer.Option(..., "--to-org"),
    dry_run: bool = True,
):
    """Promote configuration from one org to another. Default is dry-run."""
    client = CLIClient()
    result = client.post(
        "/api/_platform/org/deploy",
        json={
            "source_org_slug": from_org,
            "target_org_slug": to_org,
            "dry_run": dry_run,
        },
    )
    if dry_run:
        typer.echo("DRY RUN — would apply:")
        for change in result.get("changes", []):
            typer.echo(f"  {change['type']:20s} {change['name']}")
    else:
        typer.echo(f"Deployed {len(result.get('applied', []))} changes")
