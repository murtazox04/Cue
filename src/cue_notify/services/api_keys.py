"""API key issuance and verification.

Keys look like ``ck_<prefix>_<secret>``. Only the prefix (for lookup) and a SHA-256 hash
of the whole key are stored; the plaintext is shown exactly once, at creation. Keys carry
~190 bits of entropy, so a fast hash is appropriate (no password-stretching needed).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.enums import Scope
from cue_notify.core.time import utcnow
from cue_notify.db.models import ApiKey
from cue_notify.services.errors import UnprocessableError

KEY_PREFIX = "ck"
TOUCH_INTERVAL = timedelta(minutes=1)
AGENT_FORBIDDEN_SCOPES = frozenset(
    {Scope.ADMIN, Scope.RECIPIENTS_READ, Scope.RECIPIENTS_WRITE, Scope.APPROVALS_DECIDE}
)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create(
    session: AsyncSession,
    name: str,
    scopes: Iterable[Scope],
    expires_at: datetime | None = None,
    *,
    agent_key: str | None = None,
) -> tuple[ApiKey, str]:
    """Create a key. Returns the row and the plaintext key (never retrievable again).

    Keys bound to an agent cannot be ``admin``: an agent could then loosen its own limits.
    """
    granted = {Scope(scope) for scope in scopes}
    refused = sorted(granted & AGENT_FORBIDDEN_SCOPES) if agent_key is not None else []
    if refused:
        raise UnprocessableError(
            f"keys bound to an agent cannot have the {', '.join(refused)} scope"
            f"{'s' if len(refused) > 1 else ''}"
        )
    prefix = secrets.token_hex(6)
    raw = f"{KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(24)}"
    key = ApiKey(
        name=name,
        prefix=prefix,
        secret_hash=_hash(raw),
        scopes=sorted(scope.value for scope in granted),
        expires_at=expires_at,
        agent_key=agent_key,
    )
    session.add(key)
    await session.flush()
    return key, raw


async def authenticate(session: AsyncSession, raw: str) -> ApiKey | None:
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return None
    key = await session.scalar(select(ApiKey).where(ApiKey.prefix == parts[1]))
    if key is None or not hmac.compare_digest(key.secret_hash, _hash(raw)):
        return None
    now = utcnow()
    if key.revoked_at is not None or (key.expires_at is not None and key.expires_at <= now):
        return None
    if key.last_used_at is None or now - key.last_used_at > TOUCH_INTERVAL:
        key.last_used_at = now  # throttled so hot keys do not write on every request
    return key


def effective_scopes(key: ApiKey) -> set[str]:
    held = set(key.scopes)
    if key.agent_key is not None:
        held -= {scope.value for scope in AGENT_FORBIDDEN_SCOPES}
    return held


def allows(key: ApiKey, scope: Scope) -> bool:
    held = effective_scopes(key)
    return Scope.ADMIN.value in held or scope.value in held
