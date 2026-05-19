# SurfaceConfig per-vendor JSON Schemas

Each file in this directory is a JSON Schema (draft 2020-12) that validates the `config` field of a `SurfaceConfig` kernel entity when its `vendor` matches the file's stem.

**Naming convention:** `{vendor}.schema.json` (e.g., `prompt-kit.schema.json`, `livekit.schema.json`).

**Validation:** `kernel/entity/save.py::save_tracked()` looks up the matching schema by `SurfaceConfig.vendor` and validates `SurfaceConfig.config` against it. Validation failure → save rejected with a clear error message naming the path that failed.

**Adding a new vendor:**
1. Drop a new `{vendor}.schema.json` file in this directory
2. Restart the kernel services (they cache schemas at startup)
3. Create SurfaceConfigs with the new vendor — `config` will be validated against the new schema

**No Pydantic class change required** — this is the deliberate design choice. New vendors are JSON Schema files, not entity migrations.

See [`../../docs/architecture/deployments.md`](../../docs/architecture/deployments.md) § The Three Entities → SurfaceConfig for the full design.
