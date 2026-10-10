"""Size limits on caller-supplied payloads."""

from __future__ import annotations

import json
from typing import Any

#: Event and message ``data`` is copied into messages, digests and webhooks; keep it small.
MAX_DATA_BYTES = 16_384


def bounded_data(value: dict[str, Any]) -> dict[str, Any]:
    """Pydantic validator body: refuse ``data`` larger than :data:`MAX_DATA_BYTES` as JSON."""
    size = len(json.dumps(value, separators=(",", ":"), default=str).encode())
    if size > MAX_DATA_BYTES:
        raise ValueError(f"data is {size} bytes; the limit is {MAX_DATA_BYTES}")
    return value
