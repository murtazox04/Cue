"""Identifier generation.

Primary keys are UUIDv7 (RFC 9562): globally unique like UUIDv4 but ordered by creation
time, which keeps B-tree indexes compact and makes ``id`` usable as a pagination cursor.
"""

from __future__ import annotations

import os
import secrets
import time
from uuid import UUID

_RAND_A_MASK = (1 << 12) - 1
_RAND_B_MASK = (1 << 62) - 1


def uuid7() -> UUID:
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10))
    value = (
        (unix_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | ((rand >> 62) & _RAND_A_MASK) << 64
        | 0b10 << 62
        | (rand & _RAND_B_MASK)
    )
    return UUID(int=value)


def token(nbytes: int = 24) -> str:
    """URL-safe random token for secrets that are shown or embedded (tracking tokens)."""
    return secrets.token_urlsafe(nbytes)


def preference_token_id() -> str:
    return secrets.token_hex(12)
