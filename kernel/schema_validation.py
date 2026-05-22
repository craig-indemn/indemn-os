"""JSON Schema validation for entity fields.

Used by SurfaceConfig.config validation (against per-vendor schemas) and
Deployment.parameter_schema validation (the schema itself must be valid JSON
Schema).

JSON Schema files live in repo at indemn-os/schemas/. Loaded once at module
import time; cached in memory; reloaded by restarting the kernel process.

Library: jsonschema (Draft 2020-12).
"""

import functools
import json
from pathlib import Path

import jsonschema

# Repo root — kernel/schema_validation.py is at REPO/kernel/schema_validation.py
_REPO_ROOT = Path(__file__).parent.parent


@functools.lru_cache(maxsize=64)
def get_surface_config_schema(vendor: str) -> dict:
    """Load + cache the per-vendor JSON Schema for SurfaceConfig.config.

    Raises FileNotFoundError if no schema exists for the vendor — callers
    should surface this as a validation error to the user.
    """
    schema_path = _REPO_ROOT / "schemas" / "surface_configs" / f"{vendor}.schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(
            f"No JSON Schema found for SurfaceConfig vendor '{vendor}' at "
            f"{schema_path}. Add the schema file or fix the vendor name."
        )
    return json.loads(schema_path.read_text())


def validate_surface_config(vendor: str, config: dict) -> None:
    """Validate a SurfaceConfig.config dict against its per-vendor schema.

    Raises:
        jsonschema.ValidationError: config doesn't satisfy the schema
        FileNotFoundError: vendor has no schema file
    """
    schema = get_surface_config_schema(vendor)
    jsonschema.validate(instance=config, schema=schema)


def validate_parameter_schema_is_valid_json_schema(parameter_schema: dict) -> None:
    """Validate that a Deployment.parameter_schema is itself a valid JSON Schema.

    Doesn't validate any instance against it — just that the schema document
    is syntactically valid as a Draft 2020-12 JSON Schema. Used by Task 1.9.
    """
    if not parameter_schema:
        # An empty dict means "no schema" — trivially valid.
        return
    jsonschema.Draft202012Validator.check_schema(parameter_schema)


def validate_static_against_parameter_schema(parameter_schema: dict, static: dict) -> None:
    """Validate `static_parameters` dict against `parameter_schema` (Track 13e).

    Save-time validation catches **value-level operator errors** on the baked-in
    static parameters — wrong enum values, type mismatches, unknown fields
    when `additionalProperties: false`. It does NOT enforce the schema's
    `required` constraints: per design §5.4 + §5.6, `static_parameters` is a
    SUBSET of the eventual `static + dynamic_params` merge. Completeness
    (the `required` check) is enforced at `/sessions` on the merged set, not
    at Deployment save_tracked.

    Concretely: for a `session_actor` Deployment with `parameter_schema.required
    = ["actor_id"]`, an operator's `static_parameters = {}` is valid at save
    time — `actor_id` is dynamic (extracted from the JWT at session start).
    Strict enforcement here would forbid that pattern, which is exactly the
    design's worked example for Sales-Web (§5.6).

    No-op when parameter_schema is empty (no schema = anything goes).

    Raises:
        jsonschema.ValidationError: a value in `static` violates the schema
            (wrong enum, wrong type, unknown field) — NOT for missing required.
    """
    if not parameter_schema:
        return
    # Strip `required` so save-time only checks value-level constraints.
    # Completeness is `/sessions`' job on merged static+dynamic.
    schema_no_required = {k: v for k, v in parameter_schema.items() if k != "required"}
    jsonschema.validate(instance=static, schema=schema_no_required)
