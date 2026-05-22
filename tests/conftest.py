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
from kernel_entities.brand_assets import BrandAssets
from kernel_entities.deployment import Deployment
from kernel_entities.surface_config import SurfaceConfig

ALL_MODELS = [
    Organization,
    Actor,
    Role,
    Integration,
    Attention,
    Runtime,
    Session,
    Deployment,  # AI-406
    SurfaceConfig,  # AI-406
    BrandAssets,  # AI-406
    EntityDefinition,
    Skill,
    Rule,
    RuleGroup,
    Lookup,
    Message,
    MessageLog,
    ChangeRecord,
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
        id=oid,
        org_id=oid,
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:6]}",
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
        org_id=org_id,
        name="admin",
        permissions={"read": ["*"], "write": ["*"]},
    )
    await role.insert()

    act = Actor(
        org_id=org_id,
        name="Test Actor",
        email="test@example.com",
        type="human",
        status="active",
        role_ids=[role.id],
    )
    await act.insert()
    current_actor_id.set(str(act.id))
    act._cached_roles = [role]

    yield act
    current_actor_id.set(None)


# --- AI-406 integration fixtures (Task 1.10.5) -------------------------------
# Used by Task 1.11's /public endpoint tests and any Phase 2+ test that
# exercises Deployment-bound flows. All factory fixtures use yield+delete so
# dev MongoDB doesn't accumulate test pollution between runs.

from httpx import ASGITransport, AsyncClient  # noqa: E402


@pytest_asyncio.fixture
async def client(db):
    """Async httpx client against the FastAPI app for integration tests.

    kernel.api.app exports a `create_app()` factory (not a module-level
    `app`); the playbook spec assumed module-level export. Calling the
    factory once per test is fine (each test gets a fresh router state).
    """
    from kernel.api.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def sample_runtime(db, org_id):
    """Factory: Runtime in `active` status. Cleaned up on teardown."""
    runtime = Runtime(
        org_id=org_id,
        name="test-chat-runtime",
        kind="realtime_chat",
        framework="deepagents",
        framework_version="0.1",
        transport="websocket",
        transport_config={"endpoint_url": "wss://test-runtime.local/ws/chat"},
        status="active",
    )
    await runtime.insert()
    yield runtime
    await runtime.delete()


@pytest_asyncio.fixture
async def sample_associate(db, org_id):
    """Factory: Actor of type=associate (distinct from the generic `actor`
    fixture). Used by sample_deployment + paused_deployment so the
    Deployment.associate_id binds to a real associate."""
    actor = Actor(
        org_id=org_id,
        name="Test Sales Assistant",
        type="associate",
        mode="reasoning",
    )
    await actor.insert()
    yield actor
    await actor.delete()


@pytest_asyncio.fixture
async def sample_deployment(db, org_id, sample_runtime, sample_associate):
    """Deployment in `active` status with acts_as=session_actor + parameter_schema
    requiring actor_id. Exercises the session_actor JWT-extraction path + the
    /public endpoint's full surface-safe payload."""
    d = Deployment(
        org_id=org_id,
        name="Sample Deployment",
        associate_id=sample_associate.id,
        runtime_id=sample_runtime.id,
        parameter_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["actor_id"],
            "properties": {"actor_id": {"type": "string"}},
        },
        acts_as="session_actor",
        allowed_origins=["https://test.example.com"],
        status="active",
    )
    await d.insert()
    yield d
    await d.delete()


@pytest_asyncio.fixture
async def paused_deployment(db, org_id, sample_runtime, sample_associate):
    """Deployment in `paused` status with acts_as=associate_self. Used for the
    not-active-rejection test on /public + any tests exercising the paused
    state-machine path."""
    d = Deployment(
        org_id=org_id,
        name="Paused Deployment",
        associate_id=sample_associate.id,
        runtime_id=sample_runtime.id,
        acts_as="associate_self",
        status="paused",
    )
    await d.insert()
    yield d
    await d.delete()
