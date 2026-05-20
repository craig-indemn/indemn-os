"""Unit tests for Deployment kernel entity (AI-404-1)."""

import pytest
from bson import ObjectId
from pydantic import ValidationError


class TestDeploymentEntity:
    def test_can_import(self):
        """Deployment class can be imported from kernel_entities."""
        from kernel_entities.deployment import Deployment

        assert Deployment is not None

    def test_required_fields(self):
        """Deployment requires name, associate_id, runtime_id, acts_as."""
        from kernel_entities.deployment import Deployment

        # Should construct with required fields
        deployment = Deployment(
            org_id=ObjectId(),
            name="Test Deployment",
            associate_id=ObjectId(),
            runtime_id=ObjectId(),
            acts_as="associate_self",
        )
        assert deployment.name == "Test Deployment"
        assert deployment.acts_as == "associate_self"
        assert deployment.status == "configured"  # default

    def test_missing_name_raises(self):
        """Missing name → ValidationError."""
        from kernel_entities.deployment import Deployment

        with pytest.raises(ValidationError):
            Deployment(
                org_id=ObjectId(),
                associate_id=ObjectId(),
                runtime_id=ObjectId(),
                acts_as="associate_self",
            )

    def test_acts_as_enum_validated(self):
        """acts_as only accepts session_actor or associate_self."""
        from kernel_entities.deployment import Deployment

        with pytest.raises(ValidationError):
            Deployment(
                org_id=ObjectId(),
                name="Bad",
                associate_id=ObjectId(),
                runtime_id=ObjectId(),
                acts_as="invalid_value",
            )

    def test_is_kernel_entity_marker(self):
        """Deployment is marked as a kernel entity."""
        from kernel_entities.deployment import Deployment

        assert Deployment._is_kernel_entity is True

    def test_settings_collection_name(self):
        """Beanie Settings.name is 'deployments'."""
        from kernel_entities.deployment import Deployment

        assert Deployment.Settings.name == "deployments"
