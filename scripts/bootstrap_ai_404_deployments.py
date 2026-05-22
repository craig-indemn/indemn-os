"""Bootstrap data for AI-404 (voice-driven proposal generation).

Creates in dev OS `_platform` org:
- 1 Sales Assistant Actor (type=associate)
- 1 BrandAssets ("Indemn Brand")
- 2 SurfaceConfigs (chat prompt-kit + voice livekit), each referencing the BrandAssets
- 2 Deployments (Sales-Web + Sales-Voice), each pointing at the right Runtime + SurfaceConfig

Idempotent — looks up records by (org_id, name) before creating. Re-runs are
safe and report existing IDs.

Uses Beanie directly (not the `indemn` CLI) because the deployed API doesn't
yet have AI-406's new entity routes — the CLI's dynamic CRUD generation
won't expose `deployment/surface-config/brand-assets` subcommands until the
API is redeployed with `KERNEL_DOCUMENT_MODELS` containing the new entities.
Once that deploy lands, the same bootstrap could equivalently be done via
the shell script in the playbook (kept commented at the bottom of this
file for post-deploy reference).

Saves via `save_tracked(actor_id="system:bootstrap:ai-404", ...)` so the
audit trail (changes collection) properly records the bootstrap operations.
"""

import asyncio
import os
import sys
from pathlib import Path

import boto3
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

# Make the kernel imports work when run from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.db import KERNEL_DOCUMENT_MODELS  # noqa: E402
from kernel_entities.actor import Actor  # noqa: E402
from kernel_entities.brand_assets import BrandAssets  # noqa: E402
from kernel_entities.deployment import Deployment  # noqa: E402
from kernel_entities.organization import Organization  # noqa: E402
from kernel_entities.runtime import Runtime  # noqa: E402
from kernel_entities.surface_config import SurfaceConfig  # noqa: E402

ACTOR_ID = "system:bootstrap:ai-404"

PARAM_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["actor_id"],
    "properties": {
        # Dynamic — passed at session start
        "actor_id": {"type": "string", "pattern": "^[0-9a-f]{24}$"},
        "current_route": {"type": "string"},
        # Static — baked in via STATIC_PARAMS (validated at save_tracked)
        "role": {"type": "string"},
        "tenant": {"type": "string"},
    },
    "additionalProperties": False,
}

STATIC_PARAMS = {"role": "sales", "tenant": "indemn-internal"}

ALLOWED_ORIGINS = ["https://sales.indemn.ai", "http://localhost:5173"]


def get_mongodb_uri() -> str:
    """Pull MongoDB URI from env or AWS Secrets; rewrite -pl-0 → public host."""
    uri = os.environ.get("MONGODB_URI")
    if uri:
        return uri
    secrets = boto3.client("secretsmanager", region_name="us-east-1")
    response = secrets.get_secret_value(SecretId="indemn/dev/shared/mongodb-uri")
    private_uri = response["SecretString"]
    return private_uri.replace("-pl-0.", ".")


async def _find_by_name(cls, org_id, name):
    """Look up an entity by (org_id, name)."""
    return await cls.find_one({"org_id": org_id, "name": name})


async def main():
    print("=== AI-404 bootstrap ===")
    uri = get_mongodb_uri()
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
    db = client["indemn_os"]
    await init_beanie(database=db, document_models=KERNEL_DOCUMENT_MODELS)

    # Set context for kernel.db getters (some downstream code reads these)
    import kernel.db as db_module
    db_module._client = client
    db_module._db = db

    # 1) Resolve _platform org
    platform_org = await Organization.find_one(Organization.slug == "_platform")
    if not platform_org:
        print("ERROR: _platform org not found in dev")
        sys.exit(1)
    org_id = platform_org.id
    print(f"  org: _platform = {org_id}")

    # 2) Resolve Runtime IDs (must already exist in dev)
    chat_runtime = await _find_by_name(Runtime, org_id, "chat-deepagents-dev")
    voice_runtime = await _find_by_name(Runtime, org_id, "voice-deepagents-dev")
    if not chat_runtime:
        print("ERROR: chat-deepagents-dev runtime not found")
        sys.exit(1)
    if not voice_runtime:
        print("ERROR: voice-deepagents-dev runtime not found")
        sys.exit(1)
    print(f"  runtime chat-deepagents-dev = {chat_runtime.id}")
    print(f"  runtime voice-deepagents-dev = {voice_runtime.id}")

    # 3) BrandAssets — "Indemn Brand"
    brand = await _find_by_name(BrandAssets, org_id, "Indemn Brand")
    if not brand:
        brand = BrandAssets(
            org_id=org_id,
            name="Indemn Brand",
            logo_url="https://cdn.indemn.ai/logo.svg",
            favicon_url="https://cdn.indemn.ai/favicon.ico",
            primary_color="#1a3a8f",
            secondary_color="#4a5d8a",
            accent_color="#f59e0b",
            font_family_heading="Inter",
            font_family_body="Inter",
        )
        await brand.save_tracked(actor_id=ACTOR_ID, method="bootstrap")
        print(f"  brand_assets: CREATED Indemn Brand = {brand.id}")
    else:
        print(f"  brand_assets: EXISTS Indemn Brand = {brand.id}")

    # 4) Sales Assistant Actor (transitions to active so Deployments can bind it)
    sales_actor = await _find_by_name(Actor, org_id, "Sales Assistant")
    if not sales_actor:
        sales_actor = Actor(
            org_id=org_id,
            name="Sales Assistant",
            type="associate",
            mode="reasoning",
            skills=["proposal-generation-voice"],  # skill content created in Phase 4C
        )
        await sales_actor.save_tracked(actor_id=ACTOR_ID, method="bootstrap")
        print(f"  actor: CREATED Sales Assistant = {sales_actor.id}")
    else:
        print(f"  actor: EXISTS Sales Assistant = {sales_actor.id} (status={sales_actor.status})")
    if sales_actor.status == "provisioned":
        sales_actor.transition_to("active")
        await sales_actor.save_tracked(actor_id=ACTOR_ID, method="transition")
        print(f"  actor: TRANSITIONED Sales Assistant provisioned → active")

    # 5) SurfaceConfigs
    chat_sc = await _find_by_name(SurfaceConfig, org_id, "Indemn Sales — prompt-kit chat")
    if not chat_sc:
        chat_sc = SurfaceConfig(
            org_id=org_id,
            name="Indemn Sales — prompt-kit chat",
            channel_kind="chat",
            vendor="prompt-kit",
            brand_assets_id=brand.id,
            config={
                "widget_position": "bottom-right",
                "primary_color_ref": "brand.primary",
                "show_header": True,
                "header_text": "Sales Assistant",
                "input_placeholder": "Tell me about the customer you're building a proposal for…",
                "show_voice_toggle": True,
                "open_on_load": False,
            },
        )
        await chat_sc.save_tracked(actor_id=ACTOR_ID, method="bootstrap")
        # Activate
        chat_sc.transition_to("active")
        await chat_sc.save_tracked(actor_id=ACTOR_ID, method="transition")
        print(f"  surface_config: CREATED chat prompt-kit = {chat_sc.id}")
    else:
        print(f"  surface_config: EXISTS chat prompt-kit = {chat_sc.id}")

    voice_sc = await _find_by_name(SurfaceConfig, org_id, "Indemn Sales — livekit voice")
    if not voice_sc:
        voice_sc = SurfaceConfig(
            org_id=org_id,
            name="Indemn Sales — livekit voice",
            channel_kind="voice",
            vendor="livekit",
            brand_assets_id=brand.id,
            config={
                "widget_style": "floating-orb",
                "show_transcription": True,
                "show_waveform": True,
                "primary_color_ref": "brand.primary",
                "stt_provider": "deepgram",
                "stt_model": "nova-3",
                "tts_provider": "cartesia",
                "tts_model": "sonic-3",
                "tts_voice_id": "6ccbfb76-1fc6-48f7-b71d-91ac6298247b",
                "vad": "silero",
                "interrupt_enabled": True,
            },
        )
        await voice_sc.save_tracked(actor_id=ACTOR_ID, method="bootstrap")
        voice_sc.transition_to("active")
        await voice_sc.save_tracked(actor_id=ACTOR_ID, method="transition")
        print(f"  surface_config: CREATED voice livekit = {voice_sc.id}")
    else:
        print(f"  surface_config: EXISTS voice livekit = {voice_sc.id}")

    # 6) Deployments
    sales_web = await _find_by_name(Deployment, org_id, "Sales-Web")
    if not sales_web:
        sales_web = Deployment(
            org_id=org_id,
            name="Sales-Web",
            associate_id=sales_actor.id,
            runtime_id=chat_runtime.id,
            surface_config_id=chat_sc.id,
            parameter_schema=PARAM_SCHEMA,
            static_parameters=STATIC_PARAMS,
            greeting="Hi! What proposal can I help you build?",
            acts_as="session_actor",
            allowed_origins=ALLOWED_ORIGINS,
        )
        await sales_web.save_tracked(actor_id=ACTOR_ID, method="bootstrap")
        sales_web.transition_to("active")
        await sales_web.save_tracked(actor_id=ACTOR_ID, method="transition")
        print(f"  deployment: CREATED Sales-Web = {sales_web.id}")
    else:
        print(f"  deployment: EXISTS Sales-Web = {sales_web.id}")

    sales_voice = await _find_by_name(Deployment, org_id, "Sales-Voice")
    if not sales_voice:
        sales_voice = Deployment(
            org_id=org_id,
            name="Sales-Voice",
            associate_id=sales_actor.id,
            runtime_id=voice_runtime.id,
            surface_config_id=voice_sc.id,
            parameter_schema=PARAM_SCHEMA,
            static_parameters=STATIC_PARAMS,
            greeting="Hi, this is your proposal assistant. Who are we writing for?",
            acts_as="session_actor",
            allowed_origins=ALLOWED_ORIGINS,
        )
        await sales_voice.save_tracked(actor_id=ACTOR_ID, method="bootstrap")
        sales_voice.transition_to("active")
        await sales_voice.save_tracked(actor_id=ACTOR_ID, method="transition")
        print(f"  deployment: CREATED Sales-Voice = {sales_voice.id}")
    else:
        print(f"  deployment: EXISTS Sales-Voice = {sales_voice.id}")

    print()
    print("=== Bootstrap complete ===")
    print(f"  BrandAssets:        {brand.id}")
    print(f"  Sales Actor:        {sales_actor.id}")
    print(f"  Chat SurfaceConfig: {chat_sc.id}")
    print(f"  Voice SurfaceConfig:{voice_sc.id}")
    print(f"  Sales-Web Deployment:  {sales_web.id}")
    print(f"  Sales-Voice Deployment:{sales_voice.id}")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
