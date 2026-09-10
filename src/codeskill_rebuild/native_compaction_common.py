"""Shared fail-closed primitives for native OpenClaw compaction adapters."""

from __future__ import annotations

import json
from typing import Any


class NativeCompactionEvidenceError(RuntimeError):
    """The native session record cannot safely justify an overlay relocation."""


def read_json_line(value: str) -> dict[str, Any]:
    """Parse one native JSON object without accepting arbitrary JSON values."""
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("entry is not an object")
    return parsed
