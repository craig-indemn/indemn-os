"""Entity metadata endpoint — returns everything the UI and CLI need to auto-generate."""

from typing import get_args

from fastapi import APIRouter, Depends

from kernel.auth.middleware import get_current_actor
from kernel.db import ENTITY_REGISTRY
from kernel.entity.definition import EntityDefinition

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
