"""Voice-frontdoor wrapper around `harness_common.jwt_auth` (AI-408 extraction).

The full HS256 dual-mode + purpose-claim impl moved to
`harnesses/_base/harness_common/jwt_auth.py` so the chat runtime can share
it. This wrapper pins the voice-frontdoor audience constant and re-exports
the symbols the rest of the package + the existing test fixtures reach for
(`verify_jwt`, `_get_public_key`, `JWT_AUDIENCE`, `JWT_ISSUER`,
`JWT_LEEWAY_SECONDS`).

The wrapper shape matters for back-compat:
- `harness.sessions` calls `jwt_auth.verify_jwt(token)` (no audience arg) —
  the wrapper supplies the pinned audience so call sites don't change.
- `tests/conftest.py`'s autouse `_stub_jwt_public_key` fixture monkeypatches
  `harness.jwt_auth._get_public_key` — re-exporting from harness_common
  keeps that patch effective (it replaces the bound module attribute on
  THIS module, which the wrapper then uses transitively because
  `_verify_shared` resolves `_get_public_key` at call time through the
  shared module — see test note below).

**Test fixture note:** the conftest patches `harness.jwt_auth._get_public_key`
locally. After this extraction, the shared module's `verify_jwt` uses
`harness_common.jwt_auth._get_public_key` at call time. To keep the
existing fixture working without rewriting every voice-frontdoor test, the
conftest's autouse fixture has been updated to also patch the shared
symbol — see `tests/conftest.py` `_stub_jwt_public_key`.
"""

from harness_common.jwt_auth import (
    JWT_ISSUER,  # re-exported for any test that imports it
    JWT_LEEWAY_SECONDS,  # re-exported for any test that imports it
    _get_public_key,  # re-exported so tests patching harness.jwt_auth._get_public_key keep working
)
from harness_common.jwt_auth import (
    verify_jwt as _verify_shared,
)

# Audience constant — pinned per surface. A JWT minted for chat
# (audience="runtime-chat") MUST NOT validate against the voice frontdoor
# (RS256 path). The HS256 path doesn't check aud per OS reality.
JWT_AUDIENCE = "runtime-voice-frontdoor"


def verify_jwt(token: str) -> dict:
    """Voice-frontdoor entry point — wraps shared `verify_jwt` with the
    voice-frontdoor audience pinned. Same exceptions as the shared impl.
    """
    return _verify_shared(token, audience=JWT_AUDIENCE)


__all__ = [
    "JWT_AUDIENCE",
    "JWT_ISSUER",
    "JWT_LEEWAY_SECONDS",
    "_get_public_key",
    "verify_jwt",
]
