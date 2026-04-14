"""Skill content integrity — hash on creation, verify on load.

Skills are tamper-evident. A skill modified outside the normal update path
is rejected. Content hashes are computed on creation and verified on load.
"""

import hashlib


def compute_content_hash(content: str) -> str:
    """Compute SHA-256 hash of skill content."""
    return hashlib.sha256(content.encode()).hexdigest()


def verify_content_hash(content: str, expected_hash: str) -> bool:
    """Verify skill content against its expected hash."""
    return compute_content_hash(content) == expected_hash
