"""Kernel capabilities — import submodules to trigger self-registration."""

import kernel.capability.auto_classify  # noqa: F401
import kernel.capability.entity_resolve  # noqa: F401
import kernel.capability.fetch_new  # noqa: F401
import kernel.capability.stale_check  # noqa: F401

# Capabilities that operate on the entity collection (no specific entity_id) rather
# than on a single instance. The CLI/API renders these as `indemn <slug> <cap-name>
# --data '{...}'`, NOT as `indemn <slug> <cap-name> <id> --auto`.
# Keeping this central so the API route registrar (kernel/api/registration.py) and
# the auto-generated entity skill (kernel/skill/generator.py) stay in sync.
COLLECTION_LEVEL_CAPABILITIES: set[str] = {"fetch_new", "entity_resolve"}
