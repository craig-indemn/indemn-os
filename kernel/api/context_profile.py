"""Response-serialization profiles for entity GET routes.

Per-field truncation policy lives here, NOT in harness code. The harness
asks for `?context_profile=llm` and trusts the kernel's response.

Why a profile layer:
- The hint on FieldDefinition (`content_size_hint: short|medium|long|rich`)
  describes the field's CONTENT NATURE. It does not encode byte counts.
- The CONSUMER chooses a profile that maps hint → byte cap. An LLM consumer
  uses `llm`; a UI preview might use `preview` (future); the default `raw`
  applies no caps and matches today's GET behavior.
- Adding a new consumer profile is a one-line addition here; no entity
  definition or harness change required.

Defaults:
- Profile = `raw` (no caps) — preserves existing API behavior for all
  callers that don't pass `?context_profile`.
- Unset hint under `llm` = `medium` (50K) — see Session 27 plan decision.

Kernel-entity branch:
- Kernel entities (Trace, Message, Actor, ...) have no FieldDefinition rows
  and therefore no `content_size_hint`. By design they are NOT capped under
  any profile — `serialize_for_profile` short-circuits when
  `_field_definitions` is empty. This preserves rich kernel-entity payloads
  (e.g. Trace.outputs, often 1MB+ of JSON) and keeps the architectural
  principle clean: policy lives on the entity definition, not in code.
"""

from typing import Optional

PROFILE_CAPS: dict[str, dict[Optional[str], Optional[int]]] = {
    # No truncation — matches today's API behavior for callers that don't
    # specify a profile. Default profile.
    "raw": {
        "short": None,
        "medium": None,
        "long": None,
        "rich": None,
        None: None,
    },
    # LLM context profile — used by harnesses injecting entity context into
    # an LLM system prompt. Caps chosen for modern context windows (1M+
    # tokens). Adjust here if a smaller-context model needs tighter caps;
    # entity definitions do not change.
    "llm": {
        "short": 5_000,
        "medium": 50_000,
        "long": 500_000,
        "rich": 1_000_000,
        None: 50_000,  # default for unset hint
    },
}

TRUNCATION_MARKER_TEMPLATE = (
    "\n\n[… truncated — {total} chars total. "
    "Refetch with ?context_profile=raw for full content.]"
)


def is_valid_profile(profile: str) -> bool:
    """Return True if `profile` is a known profile name."""
    return profile in PROFILE_CAPS


def cap_for(hint: Optional[str], profile: str) -> Optional[int]:
    """Return the byte cap for a given (hint, profile) pair.

    None means no truncation. Unknown profile defaults to `raw` (no cap).
    Unknown hint within a known profile uses the profile's `None` entry
    (typically the `medium` default).
    """
    if profile not in PROFILE_CAPS:
        return None
    profile_map = PROFILE_CAPS[profile]
    if hint in profile_map:
        return profile_map[hint]
    return profile_map.get(None)


def apply_cap(value: str, cap: Optional[int]) -> str:
    """Apply a byte cap to a string value, appending the truncation marker
    when truncation occurs. Returns `value` unchanged when:
      * `cap` is None (no policy)
      * `value` length is at or under `cap`

    The marker length is subtracted from the cap before slicing so the
    returned string is at most `cap` bytes total (marker included). This
    keeps the contract "the field never exceeds the cap" honest for
    callers that check byte budgets.
    """
    if cap is None:
        return value
    total = len(value)
    if total <= cap:
        return value
    marker = TRUNCATION_MARKER_TEMPLATE.format(total=total)
    # Reserve room for the marker so the returned string fits in `cap` bytes.
    # Minimum 1 byte of content even if cap is pathologically small.
    keep = max(cap - len(marker), 1)
    return value[:keep] + marker
