"""Pytest config — make the harness directory importable.

The harness lives at `harnesses/voice-deepagents/` (hyphens — not a valid
Python package name). Inside the Docker image it's COPYed to `/app/harness/`
and run via `python -m harness.main`. For local pytest we add the harness
directory to sys.path so `from llm_adapter import ...` resolves directly.
"""

import os
import sys

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HARNESS_DIR not in sys.path:
    sys.path.insert(0, HARNESS_DIR)
