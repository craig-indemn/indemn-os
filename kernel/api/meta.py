"""Entity metadata endpoint — returns everything the UI and CLI need to auto-generate.

Phase 1: basic entity list metadata.
Phase 4 adds: per-entity detail metadata with full field info [G-33].
"""

from typing import get_args

from fastapi import APIRouter, Depends, HTTPException

from kernel.auth.middleware import get_current_actor
from kernel.db import ENTITY_REGISTRY

meta_router = APIRouter(prefix="/api/_meta", tags=["meta"])


@meta_router.get("/entities")
async def get_entity_metadata(actor=Depends(get_current_actor)):
    """Return metadata for all entity types accessible to the current actor."""
    result = []
    for name, cls in ENTITY_REGISTRY.items():
        if not _has_any_permission(actor, name):
            continue

        meta = {
            "name": name,
            "fields": _get_field_metadata(cls, name),
            "state_machine": getattr(cls, "_state_machine", None),
            "capabilities": [
                {
                    "name": cap.capability if hasattr(cap, "capability") else cap.get("capability"),
                    "config": cap.config if hasattr(cap, "config") else cap.get("config"),
                }
                for cap in getattr(cls, "_activated_capabilities", [])
            ],
            "is_kernel_entity": getattr(cls, "_is_kernel_entity", False),
            "exposed_methods": [
                {"name": attr._exposed_name}
                for attr_name in dir(cls)
                if (attr := getattr(cls, attr_name, None)) and getattr(attr, "_exposed", False)
            ],
            "permissions": {
                "read": _check_permission(actor, name, "read"),
                "write": _check_permission(actor, name, "write"),
            },
        }
        result.append(meta)

    return result


def _has_any_permission(actor, entity_name: str) -> bool:
    """Check if actor has read or write permission for this entity type."""
    roles = getattr(actor, "_cached_roles", [])
    for role in roles:
        for action in ("read", "write"):
            allowed = role.permissions.get(action, [])
            if "*" in allowed or entity_name in allowed:
                return True
    return False


def _get_field_metadata(cls, entity_name: str) -> list[dict]:
    """Derive field metadata from Pydantic model_fields."""
    fields = []
    for fname, finfo in cls.model_fields.items():
        if fname.startswith("_") or fname in ("id", "revision_id"):
            continue
        fields.append(
            {
                "name": fname,
                "type": _pydantic_type_to_string(finfo.annotation),
                "required": finfo.is_required(),
                "default": finfo.default if finfo.default is not None else None,
                "enum_values": _extract_enum_values(finfo.annotation),
                "description": finfo.description,
            }
        )
    return fields


def _pydantic_type_to_string(annotation) -> str:
    """Convert a Pydantic type annotation to a simple string."""
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        return "list"
    type_name = getattr(annotation, "__name__", str(annotation))
    type_map = {
        "str": "str",
        "int": "int",
        "float": "float",
        "bool": "bool",
        "ObjectId": "objectid",
        "datetime": "datetime",
        "date": "date",
        "Decimal": "decimal",
    }
    return type_map.get(type_name, "str")


def _extract_enum_values(annotation) -> list[str] | None:
    """Extract Literal values from a type annotation."""
    args = get_args(annotation)
    if args and all(isinstance(a, str) for a in args):
        return list(args)
    return None


def _check_permission(actor, entity_name: str, action: str) -> bool:
    """Check a specific permission without raising."""
    roles = getattr(actor, "_cached_roles", [])
    for role in roles:
        allowed = role.permissions.get(action, [])
        if "*" in allowed or entity_name in allowed:
            return True
    return False


# --- Per-entity detail metadata [G-33] ---


@meta_router.get("/entities/{entity_name}")
async def get_entity_detail_metadata(entity_name: str, actor=Depends(get_current_actor)):
    """Full metadata for a specific entity type — everything the UI needs
    to auto-generate list views, detail views, and forms."""
    cls = ENTITY_REGISTRY.get(entity_name)
    if not cls:
        raise HTTPException(404, f"Entity type '{entity_name}' not found")

    # Get field metadata — domain entities use EntityDefinition,
    # kernel entities derive from Pydantic model_fields
    fields = []
    is_kernel = getattr(cls, "_is_kernel_entity", False)

    if not is_kernel:
        from kernel.entity.definition import EntityDefinition

        defn = await EntityDefinition.find_one({"name": entity_name})
        if defn:
            for fname, fdef in defn.fields.items():
                fields.append({
                    "name": fname,
                    "type": fdef.type,
                    "required": fdef.required,
                    "default": fdef.default,
                    "enum_values": fdef.enum_values,
                    "description": fdef.description,
                    "is_state_field": fdef.is_state_field,
                    "is_relationship": fdef.is_relationship,
                    "relationship_target": fdef.relationship_target,
                    "indexed": fdef.indexed,
                    "unique": fdef.unique,
                })
        else:
            fields = _get_field_metadata(cls, entity_name)
    else:
        fields = _get_field_metadata(cls, entity_name)

    # State machine
    state_machine = getattr(cls, "_state_machine", None)

    # Capabilities
    capabilities = []
    for cap in getattr(cls, "_activated_capabilities", []):
        cap_name = cap.capability if hasattr(cap, "capability") else cap.get("capability", "")
        capabilities.append({
            "name": cap_name,
            "cli_command": (
                f"indemn {entity_name.lower()} "
                f"{cap_name.replace('_', '-')} <id> --auto"
            ),
        })

    # @exposed methods (kernel entities only)
    exposed_methods = []
    for attr_name in dir(cls):
        attr = getattr(cls, attr_name, None)
        if attr and getattr(attr, "_exposed", False):
            exposed_methods.append({
                "name": attr._exposed_name,
                "cli_command": (
                    f"indemn {entity_name.lower()} "
                    f"{attr._exposed_name.replace('_', '-')} <id>"
                ),
            })

    # Permissions for the current actor
    permissions = {
        "read": _check_permission(actor, entity_name, "read"),
        "write": _check_permission(actor, entity_name, "write"),
    }

    return {
        "name": entity_name,
        "collection": cls.Settings.name if hasattr(cls, "Settings") else entity_name.lower() + "s",
        "is_kernel_entity": is_kernel,
        "fields": fields,
        "state_machine": state_machine,
        "capabilities": capabilities,
        "exposed_methods": exposed_methods,
        "permissions": permissions,
    }
