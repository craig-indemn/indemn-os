"""Shared test fixtures.

Integration tests use the Atlas dev cluster with a dedicated test database.
Collections are cleaned after each test (not dropDatabase — dev user lacks that permission).
"""

import os
import uuid

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from kernel.changes.collection import ChangeRecord
from kernel.context import current_actor_id, current_org_id
from kernel.entity.definition import EntityDefinition
from kernel.message.schema import Message, MessageLog
from kernel.rule.lookup import Lookup
from kernel.rule.schema import Rule, RuleGroup
from kernel.skill.schema import Skill
from kernel_entities import (
    Actor,
    Attention,
    Integration,
    Organization,
    Role,
    Runtime,
    Session,
)

ALL_MODELS = [
    Organization, Actor, Role, Integration, Attention, Runtime, Session,
    EntityDefinition, Skill, Rule, RuleGroup, Lookup,
    Message, MessageLog, ChangeRecord,
]


def _get_mongodb_uri() -> str:
    """Get MongoDB URI from environment or construct from Secrets Manager."""
    uri = os.environ.get("MONGODB_URI")
    if uri:
        return uri
    # Try AWS Secrets Manager — build public endpoint URI
    try:
        import boto3
        client = boto3.client("secretsmanager", region_name="us-east-1")
        response = client.get_secret_value(SecretId="indemn/dev/shared/mongodb-uri")
        private_uri = response["SecretString"]
        # Convert private link URI to public endpoint
        return private_uri.replace("-pl-0.", ".")
    except Exception:
        pytest.skip("No MongoDB URI available (set MONGODB_URI or configure AWS)")


@pytest_asyncio.fixture
async def db():
    """Provide a test database. Collections cleaned after each test."""
    uri = _get_mongodb_uri()
    test_db_name = "indemn_os_test"
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
    database = client[test_db_name]

    await init_beanie(database=database, document_models=ALL_MODELS)

    # Set module-level db references so get_database()/get_client() work
    import kernel.db as db_module
    db_module._client = client
    db_module._db = database

    yield database

    # Cleanup: delete all documents from known collections
    for model in ALL_MODELS:
        settings = getattr(model, "Settings", None)
        if settings and hasattr(settings, "name"):
            try:
                await database[settings.name].delete_many({})
            except Exception:
                pass

    # Clean up dynamic test collections
    try:
        coll_names = await database.list_collection_names()
        for name in coll_names:
            if name.startswith("test_"):
                await database[name].delete_many({})
    except Exception:
        pass

    client.close()


@pytest_asyncio.fixture
async def org_id(db):
    """Create a test organization and set context."""
    oid = ObjectId()
    org = Organization(
        id=oid, org_id=oid, name="Test Org", slug=f"test-{uuid.uuid4().hex[:6]}",
        status="active",
    )
    await org.insert()
    current_org_id.set(oid)
    yield oid
    current_org_id.set(None)


@pytest_asyncio.fixture
async def actor(db, org_id):
    """Create a test actor with admin role and set context."""
    role = Role(
        org_id=org_id, name="admin",
        permissions={"read": ["*"], "write": ["*"]},
    )
    await role.insert()

    act = Actor(
        org_id=org_id, name="Test Actor", email="test@example.com",
        type="human", status="active", role_ids=[role.id],
    )
    await act.insert()
    current_actor_id.set(str(act.id))
    act._cached_roles = [role]

    yield act
    current_actor_id.set(None)
