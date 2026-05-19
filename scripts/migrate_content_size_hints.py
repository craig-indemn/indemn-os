#!/usr/bin/env python3
"""Set `content_size_hint` on rich-content fields in dev OS entity
definitions.

Why: the kernel's response-serialization profile (`?context_profile=llm`)
applies per-field truncation per `FieldDefinition.content_size_hint`.
Fields without a hint fall back to the profile's default (`medium` = 50K).
Rich-content fields like Email.body or Meeting.transcript need an explicit
hint to avoid being truncated to 50K under `llm`.

Pattern (read-merge-modify):
  1. GET the EntityDefinition via the API.
  2. For each field in TARGETS:
     a. If the field doesn't exist on this entity → skip.
     b. If the hint is already correct → skip (no-op).
     c. Otherwise: clone the existing FieldDefinition JSON, merge
        `content_size_hint`, POST via `--modify-field`.
  3. Report a diff per entity.

The server side replaces the FULL FieldDefinition on `modify_fields`
(kernel/api/admin_routes.py:198-207) — so the script MUST send the
merged spec, not a partial one. The CLI's `entity modify --modify-field`
flag exists for this exact pattern; this script uses it.

Usage:
    # Dry-run (shows what would change, makes no writes):
    python scripts/migrate_content_size_hints.py

    # Apply changes:
    python scripts/migrate_content_size_hints.py --apply

Requires:
    INDEMN_SERVICE_TOKEN env var with kernel.entitydefinitions write
    permission. Recommended: `runtime-async-service-token` from AWS
    Secrets Manager.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

# (entity_name, field_name, hint) — set in priority order so a partial run
# is still useful. Add new rows here when a new rich-content field surfaces.
TARGETS: list[tuple[str, str, str]] = [
    # Email body — long quoted threads with forwards (~50K-200K)
    ("Email", "body", "rich"),
    # Email HTML body if present — same content shape as body
    ("Email", "html_body", "rich"),
    # Meeting transcript — 30K-300K for 1-3 hour meetings
    ("Meeting", "transcript", "rich"),
    # Meeting smart notes (Gemini auto-generated) — typically 5K-50K
    ("Meeting", "smart_notes", "long"),
    # Document content extract — proposal PDFs, spec docs (50K-500K)
    ("Document", "content", "rich"),
    # Slack message text — typically tiny but channel context can chain
    ("SlackMessage", "text", "medium"),
    # Touchpoint TS-curated summary — typically <2K but headroom helps
    ("Touchpoint", "summary", "medium"),
]


def run_indemn(*args: str) -> dict | list:
    """Run an `indemn` CLI command and return parsed JSON output."""
    cmd = ["indemn", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"indemn command failed: {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    # Most `indemn` commands return JSON by default
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        # CLI sometimes returns XML/text; treat as failure for parseability
        raise RuntimeError(
            f"indemn returned non-JSON output for {' '.join(cmd)}:\n{result.stdout}"
        )


def get_entity_definition(entity_name: str) -> dict | None:
    """Fetch the EntityDefinition for `entity_name`. Returns None if not
    found. Uses the entitydefinitions list endpoint + name filter
    (entitydefinition list does not accept a name arg; we filter
    client-side)."""
    # The EntityDefinition is itself queried via the auto-generated entity
    # routes — `indemn entitydefinition list` returns all defs.
    defs = run_indemn("entitydefinition", "list", "--limit", "200")
    items = defs.get("results", defs) if isinstance(defs, dict) else defs
    for d in items:
        if d.get("name") == entity_name:
            return d
    return None


def get_field_definition(entity_name: str, field_name: str) -> dict | None:
    """Return the current FieldDefinition JSON for a field, or None if the
    field doesn't exist on the entity."""
    defn = get_entity_definition(entity_name)
    if defn is None:
        return None
    fields = defn.get("fields") or {}
    return fields.get(field_name)


def apply_hint(
    entity_name: str, field_name: str, hint: str, dry_run: bool
) -> str:
    """Return a short status string describing what was done (or would be)."""
    current = get_field_definition(entity_name, field_name)
    if current is None:
        return f"  {entity_name}.{field_name}: SKIP (field not defined on entity)"

    existing_hint = current.get("content_size_hint")
    if existing_hint == hint:
        return f"  {entity_name}.{field_name}: SKIP (already {hint})"

    # Read-merge-modify pattern: clone current spec, set hint, post merged.
    merged = dict(current)
    merged["content_size_hint"] = hint

    payload = {field_name: merged}

    if dry_run:
        return (
            f"  {entity_name}.{field_name}: WOULD SET content_size_hint="
            f"{hint} (was {existing_hint!r})"
        )

    run_indemn("entity", "modify", entity_name, "--modify-field", json.dumps(payload))
    return (
        f"  {entity_name}.{field_name}: SET content_size_hint={hint} "
        f"(was {existing_hint!r})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the modifications (default is dry-run)",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("INDEMN_SERVICE_TOKEN") and not _has_cli_credentials():
        print(
            "ERROR: Neither INDEMN_SERVICE_TOKEN env var nor ~/.indemn/credentials "
            "is set. Set one before running.",
            file=sys.stderr,
        )
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Migration: content_size_hint on rich-content fields ({mode}) ===\n")

    by_entity: dict[str, list[str]] = {}
    for entity_name, field_name, hint in TARGETS:
        by_entity.setdefault(entity_name, []).append((field_name, hint))

    for entity_name, fields in by_entity.items():
        print(f"{entity_name}:")
        for field_name, hint in fields:
            try:
                status = apply_hint(entity_name, field_name, hint, dry_run=not args.apply)
            except Exception as e:
                status = f"  {entity_name}.{field_name}: ERROR {e!s}"
            print(status)
        print()

    if not args.apply:
        print(
            "Dry-run complete. Review the diff above; re-run with --apply to "
            "commit the changes."
        )
    else:
        print(
            "Apply complete. Verify via: indemn entitydefinition list | jq "
            "'.[] | select(.name==\"Email\") | .fields.body.content_size_hint'"
        )
    return 0


def _has_cli_credentials() -> bool:
    """Return True if ~/.indemn/credentials exists and looks usable."""
    creds_path = os.path.expanduser("~/.indemn/credentials")
    return os.path.exists(creds_path)


if __name__ == "__main__":
    sys.exit(main())
