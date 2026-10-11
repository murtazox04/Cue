from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.api.schemas import DigestOut, EventOut, MessageOut
from cue_notify.core.enums import Scope
from cue_notify.db.models import ApiKey, Digest, Event, Message, Recipient
from cue_notify.services import api_keys, preferences, review_links
from cue_notify.settings import Settings

REDACTED = "[redacted]"
LEGACY_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32}(?![A-Za-z0-9_-])")
PREFERENCE_PATH = re.compile(r"/preferences/[A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class Redaction:
    tokens: frozenset[str] = frozenset()
    legacy_hashes: frozenset[str] = frozenset()
    preference_links: bool = False

    def text(self, value: str) -> str:
        for token in self.tokens:
            value = value.replace(token, REDACTED)
        if self.legacy_hashes:
            value = LEGACY_TOKEN.sub(self.legacy_token, value)
        if self.preference_links:
            value = PREFERENCE_PATH.sub(f"/preferences/{REDACTED}", value)
        return review_links.redact(value)

    def legacy_token(self, match: re.Match[str]) -> str:
        found = match.group(0)
        return REDACTED if preferences.token_hash(found) in self.legacy_hashes else found


def redact(value: Any, redaction: Redaction | None) -> Any:
    if redaction is None:
        return value
    if isinstance(value, str):
        return redaction.text(value)
    if isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        return {name: redact(item, redaction) for name, item in mapping.items()}
    if isinstance(value, list):
        items = cast("list[Any]", value)
        return [redact(item, redaction) for item in items]
    return value


async def redaction_for(
    session: AsyncSession, settings: Settings, key: ApiKey, recipient_ids: Iterable[UUID | None]
) -> Redaction | None:
    if api_keys.allows(key, Scope.ADMIN):
        return None
    if key.agent_key is None:
        return Redaction()
    ids = {recipient_id for recipient_id in recipient_ids if recipient_id is not None}
    rows = (
        (
            await session.execute(
                select(Recipient.preference_token_id, Recipient.preference_token_hash).where(
                    Recipient.id.in_(ids)
                )
            )
        ).all()
        if ids
        else []
    )
    tokens = {preferences.issued_token(settings, token_id) for token_id, _ in rows}
    return Redaction(
        tokens=frozenset(token for token in tokens if token),
        legacy_hashes=frozenset(digest for _, digest in rows if digest),
        preference_links=True,
    )


async def message_views(
    session: AsyncSession, settings: Settings, key: ApiKey, messages: Sequence[Message]
) -> list[MessageOut]:
    views = [MessageOut.model_validate(message) for message in messages]
    if not views:
        return views
    redaction = await redaction_for(
        session, settings, key, (message.recipient_id for message in messages)
    )
    return [view.model_copy(update={"content": redact(view.content, redaction)}) for view in views]


async def digest_views(
    session: AsyncSession, settings: Settings, key: ApiKey, digests: Sequence[Digest]
) -> list[DigestOut]:
    views = [DigestOut.model_validate(digest) for digest in digests]
    if not views:
        return views
    redaction = await redaction_for(
        session, settings, key, (digest.recipient_id for digest in digests)
    )
    return [view.model_copy(update={"items": redact(view.items, redaction)}) for view in views]


async def event_views(
    session: AsyncSession, settings: Settings, key: ApiKey, events: Sequence[Event]
) -> list[EventOut]:
    views = [EventOut.model_validate(event) for event in events]
    if not views:
        return views
    redaction = await redaction_for(
        session, settings, key, (event.recipient_id for event in events)
    )
    return [view.model_copy(update={"data": redact(view.data, redaction)}) for view in views]
