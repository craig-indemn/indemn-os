"""Consolidated kernel-pattern audit for the three AI-406 entities (Track 14b).

Tasks 1.1, 1.5, 1.6 each verify their own entity in isolation. This file
pins the canonical kernel-entity shape across ALL THREE collectively:
- `_is_kernel_entity = True` (so Beanie registers it)
- `Settings.name` (so the auto-CRUD collection routing works)
- `_state_field_name = "status"` (so state transitions use the right field)
- `_state_machine` non-empty (so transitions actually have a graph)
- All indexes lead with `org_id` (kernel multi-tenancy convention)
- Plus Track-13 specifics: Deployment's UNIQUE (org_id, name) index;
  BrandAssets's dark_mode_supported field.

Drift (e.g., one entity missing `_is_kernel_entity = True` so it's not
registered with Beanie) only surfaces as a runtime error in production
otherwise. This audit catches it cheaply at unit-test time.
"""

import pytest

from kernel_entities.brand_assets import BrandAssets
from kernel_entities.deployment import Deployment
from kernel_entities.surface_config import SurfaceConfig

KERNEL_ENTITIES = [
    (Deployment, "deployments"),
    (SurfaceConfig, "surface_configs"),
    (BrandAssets, "brand_assets"),
]


@pytest.mark.parametrize("cls,collection_name", KERNEL_ENTITIES)
def test_is_kernel_entity_marker(cls, collection_name):
    """Every AI-406 entity marks itself as kernel (so Beanie registers it)."""
    assert cls._is_kernel_entity is True, f"{cls.__name__} missing _is_kernel_entity = True"


@pytest.mark.parametrize("cls,collection_name", KERNEL_ENTITIES)
def test_settings_collection_name(cls, collection_name):
    """Settings.name matches the canonical plural collection name."""
    assert cls.Settings.name == collection_name, (
        f"{cls.__name__}.Settings.name = {cls.Settings.name!r}, expected {collection_name!r}"
    )


@pytest.mark.parametrize("cls,_collection_name", KERNEL_ENTITIES)
def test_state_field_name_is_status(cls, _collection_name):
    """All three entities declare _state_field_name = 'status'."""
    assert getattr(cls, "_state_field_name", None) == "status", (
        f"{cls.__name__} should declare _state_field_name = 'status'"
    )


@pytest.mark.parametrize("cls,_collection_name", KERNEL_ENTITIES)
def test_state_machine_non_empty(cls, _collection_name):
    """Each entity declares a non-empty state machine dict."""
    sm = getattr(cls, "_state_machine", None)
    assert sm, f"{cls.__name__} missing _state_machine"
    assert len(sm) > 0, f"{cls.__name__}._state_machine is empty"


@pytest.mark.parametrize("cls,_collection_name", KERNEL_ENTITIES)
def test_indexes_start_with_org_id(cls, _collection_name):
    """Every index's first key is `org_id` (kernel multi-tenancy convention)."""
    indexes = cls.Settings.indexes
    assert indexes, f"{cls.__name__}.Settings.indexes is empty"
    for idx in indexes:
        # Index may be IndexModel (has .document) or list of (field, direction) tuples
        if hasattr(idx, "document"):
            keys = list(idx.document.get("key", {}).keys())
        else:
            keys = [pair[0] for pair in idx]
        assert keys and keys[0] == "org_id", (
            f"{cls.__name__} index {keys!r} doesn't lead with org_id"
        )


def test_deployment_has_unique_org_id_name_index():
    """Track 13c — Deployment's (org_id, name) index is UNIQUE.

    Tuple-list form (Beanie's default) does NOT carry uniqueness — the
    explicit IndexModel(..., unique=True) form is required. If someone
    refactors to list-of-tuples, two Deployments with the same name
    would silently coexist instead of being rejected by the DB.
    """
    found_unique = False
    for idx in Deployment.Settings.indexes:
        if not hasattr(idx, "document"):
            continue
        doc = idx.document
        keys = list(doc.get("key", {}).keys())
        if keys == ["org_id", "name"] and doc.get("unique") is True:
            found_unique = True
            break
    assert found_unique, (
        "Deployment needs IndexModel([(org_id, ASC), (name, ASC)], unique=True) — "
        "the (org_id, name) uniqueness was missing or not using IndexModel form"
    )


def test_brand_assets_has_dark_mode_supported_field():
    """Track 13a — BrandAssets exposes the dark_mode_supported bool field per §6.4."""
    assert "dark_mode_supported" in BrandAssets.model_fields, (
        "BrandAssets needs dark_mode_supported field per §6.4 + Track 13a"
    )
    assert BrandAssets.model_fields["dark_mode_supported"].annotation is bool


def test_surface_config_validator_registered():
    """SurfaceConfig's per-vendor schema validator is wired (Task 1.8).

    Existence check via Pydantic's __pydantic_decorators__. The validator's
    actual behavior is exhaustively tested in test_surface_config_validation.py;
    this is the audit-level smoke check that it didn't get accidentally removed.
    """
    mvs = SurfaceConfig.__pydantic_decorators__.model_validators
    assert "_validate_config_against_vendor_schema" in mvs, (
        "SurfaceConfig must register _validate_config_against_vendor_schema (Task 1.8)"
    )
    assert mvs["_validate_config_against_vendor_schema"].info.mode == "after"


def test_deployment_validator_chain_in_order():
    """Deployment's four model_validators are present and run mode='after'.

    Order is enforced by Pydantic via declaration order; we can't assert the
    runtime order through __pydantic_decorators__ alone (dict iteration order
    happens to be declaration order in CPython 3.7+, but that's an
    implementation detail). Existence + mode is the auditable contract.
    """
    mvs = Deployment.__pydantic_decorators__.model_validators
    expected = {
        "_validate_parameter_schema",
        "_derive_acts_as_and_validate",
        "_validate_static_parameters",
        "_derive_validation_mode",
    }
    missing = expected - set(mvs.keys())
    assert not missing, f"Deployment missing validators: {missing}"
    for name in expected:
        assert mvs[name].info.mode == "after", (
            f"Deployment.{name} must be mode='after'"
        )
