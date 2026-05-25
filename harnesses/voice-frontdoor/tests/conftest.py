"""Pytest config — make the voice-frontdoor harness package importable.

The `harness/` subdir is the Python package (locally and in Docker — the
Dockerfile copies it to /app/harness/). We add the voice-frontdoor dir
to sys.path so `from harness.app import app` resolves.
"""

import sys
from pathlib import Path

VOICE_FRONTDOOR_DIR = Path(__file__).resolve().parents[1]
HARNESSES_BASE_DIR = VOICE_FRONTDOOR_DIR.parent / "_base"

if str(VOICE_FRONTDOOR_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_FRONTDOOR_DIR))
if str(HARNESSES_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESSES_BASE_DIR))
