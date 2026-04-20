"""Organization management commands — clone, diff, export, import, deploy."""

from pathlib import Path

import typer

from kernel.cli.client import CLIClient

org_app = typer.Typer(name="org", help="Organization management")


@org_app.command("clone")
def clone_org(
    source: str,
    as_name: str = typer.Option(..., "--as"),
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
    """Export org configuration to YAML files.

    Produces: org.yaml, entities/, roles/, rules/<EntityType>/,
    lookups/, skills/, actors/, integrations/, capabilities/
    """
    import yaml

    client = CLIClient()
    result = client.get("/api/_platform/org/export", params={"org": org_slug})

    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)

    # Write org.yaml (top-level org settings)
    org_settings = result.pop("org", {})
    with open(out / "org.yaml", "w") as f:
        yaml.dump(org_settings, f, default_flow_style=False)

    # Write rules organized by entity type subdirectories
    rules = result.pop("rules", {})
    if rules:
        rules_dir = out / "rules"
        rules_dir.mkdir(exist_ok=True)
        for name, data in rules.items():
            entity_type = data.get("entity_type", "general")
            type_dir = rules_dir / entity_type
            type_dir.mkdir(exist_ok=True)
            with open(type_dir / f"{name}.yaml", "w") as f:
                yaml.dump(data, f, default_flow_style=False)

    # Write capabilities as separate per-entity files
    capabilities = result.pop("capabilities", {})
    if capabilities:
        cap_dir = out / "capabilities"
        cap_dir.mkdir(exist_ok=True)
        for name, data in capabilities.items():
            with open(cap_dir / f"{name}.yaml", "w") as f:
                yaml.dump(data, f, default_flow_style=False)

    # Write skills as .md files (spec convention: skills are markdown)
    skills = result.pop("skills", {})
    if skills:
        skills_dir = out / "skills"
        skills_dir.mkdir(exist_ok=True)
        for name, data in skills.items():
            with open(skills_dir / f"{name}.md", "w") as f:
                f.write(data.get("content", ""))

    # Write remaining categories (entities, lookups, roles, etc.)
    for category, items in result.items():
        if not items:
            continue
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
    base = Path(from_dir)

    # Read rules with entity-type subdirectory structure
    rules_dir = base / "rules"
    if rules_dir.exists():
        config["rules"] = {}
        for entity_dir in rules_dir.iterdir():
            if entity_dir.is_dir():
                for f in entity_dir.glob("*.yaml"):
                    with open(f) as fh:
                        config["rules"][f.stem] = yaml.safe_load(fh)
            elif entity_dir.suffix == ".yaml":
                with open(entity_dir) as fh:
                    config["rules"][entity_dir.stem] = yaml.safe_load(fh)

    # Read capabilities
    cap_dir = base / "capabilities"
    if cap_dir.exists():
        config["capabilities"] = {}
        for f in cap_dir.glob("*.yaml"):
            with open(f) as fh:
                config["capabilities"][f.stem] = yaml.safe_load(fh)

    # Read skills from .md files
    skills_dir = base / "skills"
    if skills_dir.exists():
        config["skills"] = {}
        for f in skills_dir.glob("*.md"):
            config["skills"][f.stem] = {
                "name": f.stem,
                "type": "associate",
                "content": f.read_text(),
                "status": "active",
            }

    # Read remaining flat categories
    _HANDLED = {"rules", "capabilities", "skills", "org.yaml"}
    for cat_dir in base.iterdir():
        if cat_dir.name in _HANDLED:
            continue
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
    apply: bool = typer.Option(False, "--apply", help="Apply changes"),
):
    """Promote configuration from one org to another.

    Default is dry-run. Use --apply to execute.
    """
    # --apply overrides dry_run
    if apply:
        dry_run = False

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
