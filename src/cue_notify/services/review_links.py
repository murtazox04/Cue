"""One-time review links for held messages and approval requests.

Each reviewer gets their own unguessable token in ``review_url``. Links are looked up by
the token's SHA-256 hash; the token itself exists only in the reviewer's notification
(and the event that produced it), which the API shows to ``admin`` keys alone. The first
decision closes every link for the same target, and links expire with it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.db.models import ReviewLink
from cue_notify.services.errors import GoneError, NotFoundError
from cue_notify.settings import Settings

_IN_TEXT = re.compile(r"/review/[A-Za-z0-9_-]{20,}")


def redact(text: str) -> str:
    """``text`` with review tokens replaced, for keys that may not decide."""
    return _IN_TEXT.sub("/review/[redacted]", text)


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_data(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [redact_data(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create(
    session: AsyncSession,
    reviewer: str,
    expires_at: datetime,
    *,
    message_id: UUID | None = None,
    approval_id: UUID | None = None,
) -> str:
    """A one-time token letting ``reviewer`` decide on the target until ``expires_at``."""
    token = secrets.token_urlsafe(32)
    session.add(
        ReviewLink(
            token_hash=_hash(token),
            message_id=message_id,
            approval_id=approval_id,
            reviewer=reviewer,
            expires_at=expires_at,
        )
    )
    await session.flush()
    return token


def url(settings: Settings, token: str) -> str | None:
    if settings.api.public_url is None:
        return None
    return f"{str(settings.api.public_url).rstrip('/')}/review/{token}"


async def close(
    session: AsyncSession,
    now: datetime,
    *,
    message_id: UUID | None = None,
    approval_id: UUID | None = None,
) -> None:
    """Mark every open link for the target as used."""
    column = ReviewLink.message_id if message_id is not None else ReviewLink.approval_id
    target = message_id if message_id is not None else approval_id
    links = await session.scalars(
        select(ReviewLink).where(column == target, ReviewLink.used_at.is_(None))
    )
    for link in links:
        link.used_at = now


async def find(session: AsyncSession, token: str, *, lock: bool = True) -> ReviewLink:
    link = await session.get(ReviewLink, _hash(token), with_for_update=lock)
    if link is None:
        raise NotFoundError("review link", "…")
    return link


def check_open(link: ReviewLink, now: datetime, already: str) -> None:
    """Raise unless the link can still be used; ``already`` explains a decided target."""
    if link.used_at is not None:
        raise GoneError(already)
    if link.expires_at <= now:
        raise GoneError("This review link has expired.")
