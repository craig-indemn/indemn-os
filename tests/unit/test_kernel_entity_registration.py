"""Verify the new AI-406 kernel entities are registered with Beanie at startup."""

from kernel.db import KERNEL_DOCUMENT_MODELS
from kernel_entities.brand_assets import BrandAssets
from kernel_entities.deployment import Deployment
from kernel_entities.surface_config import SurfaceConfig


def test_deployment_in_document_models():
    """Deployment must be in KERNEL_DOCUMENT_MODELS so init_beanie creates its collection."""
    assert Deployment in KERNEL_DOCUMENT_MODELS


def test_surface_config_in_document_models():
    """SurfaceConfig must be in KERNEL_DOCUMENT_MODELS so init_beanie creates its collection."""
    assert SurfaceConfig in KERNEL_DOCUMENT_MODELS


def test_brand_assets_in_document_models():
    """BrandAssets must be in KERNEL_DOCUMENT_MODELS so init_beanie creates its collection."""
    assert BrandAssets in KERNEL_DOCUMENT_MODELS


def test_no_duplicate_registrations():
    """Catch accidental double-listing during merges (the same class twice in the list
    would cause Beanie to raise at init_beanie time)."""
    assert len(KERNEL_DOCUMENT_MODELS) == len(set(KERNEL_DOCUMENT_MODELS))
