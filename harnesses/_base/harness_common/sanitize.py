"""Sanitize free-form string values in dynamic_params before composing
the <deployment_context> SystemMessage.

Layer (c) of the §10.7 prompt-injection mitigation. Strips newlines
(prevents injection of pseudo-SystemMessage blocks like "\\n\\n[NEW
INSTRUCTION]..."), caps length (defends against context-flooding),
removes HTML-like tags (defensive — schemas should reject HTML at
validation time but the regex is a backstop).

Usage:
    from harness_common.sanitize import sanitize_dynamic_params
    safe = sanitize_dynamic_params(dynamic_params)
    # Use `safe` when composing the <deployment_context> SystemMessage.

Returns a NEW dict. Does NOT mutate the input.
"""

import re
from typing import Any

_MAX_STRING_LEN = 2000
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def sanitize_dynamic_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with string values sanitized.

    Rules:
    - Strings: replace newlines/CR with spaces, remove HTML-like tags,
      cap length at 2000 chars (append "...[truncated]" if over).
    - Non-strings (numbers, bools, None): passthrough.
    - Nested dicts: recurse.
    - Lists: recurse element-by-element.
    """
    return {k: _sanitize_value(v) for k, v in params.items()}


def _sanitize_value(v: Any) -> Any:
    if isinstance(v, str):
        v = v.replace("\n", " ").replace("\r", " ")
        v = _HTML_TAG_PATTERN.sub("", v)
        if len(v) > _MAX_STRING_LEN:
            v = v[:_MAX_STRING_LEN] + "...[truncated]"
        return v
    if isinstance(v, dict):
        return {k: _sanitize_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_sanitize_value(item) for item in v]
    return v
