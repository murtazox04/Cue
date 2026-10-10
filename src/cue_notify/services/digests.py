"""Digests: group a burst of events into one message (see :mod:`cue_notify.core.digest`).

Lifecycle of a digest:

1. The first event matching a digest rule opens a digest (per rule, recipient and
   grouping key) and schedules a ``flush_digest`` job for the end of the window.
2. Later events within the window are appended; reaching ``send_after`` moves the flush
   job to now.
3. The flush composes one message through the regular composer, so preferences, caps,
   cooldowns and quiet hours still apply, and the digest is marked sent.

Adding to a digest happens under the recipient row lock, so concurrent events for the
same person never open two digests for the same key.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify import queue
from cue_notify.core import templating
from cue_notify.core.digest import DigestPolicy
from cue_notify.core.enums import DigestStatus
from cue_notify.core.time import utcnow
from cue_notify.db.models import Category, Digest, Event, Recipient, Rule, Template
from cue_notify.runtime import Runtime
from cue_notify.services import composer
from cue_notify.services.errors import NotFoundError

logger = logging.getLogger(__name__)

FLUSH_JOB = "flush_digest"
DEFAULT_KEY = "default"


def _job_key(digest: Digest) -> str:
    return f"{FLUSH_JOB}:{digest.id}"


def _chronological(item: dict[str, Any]) -> tuple[datetime, str]:
    return datetime.fromisoformat(item["occurred_at"]), item["event_id"]


def grouping_key(policy: DigestPolicy, context: dict[str, Any]) -> str:
    """Render the grouping key for one event; a key that cannot render groups by rule."""
    if not policy.key:
        return DEFAULT_KEY
    try:
        rendered = templating.render(policy.key, context)
    except templating.RenderError as exc:
        logger.warning("digest key failed to render, using the default key: %s", exc)
        return DEFAULT_KEY
    return rendered[:255] or DEFAULT_KEY


async def add(
    session: AsyncSession,
    runtime: Runtime,
    rule: Rule,
    event: Event,
    recipient: Recipient,
    context: dict[str, Any],
) -> Digest:
    """Add ``event`` to the open digest for its key, opening one if needed."""
    policy = DigestPolicy.model_validate(rule.digest)
    await session.execute(
        select(Recipient.id).where(Recipient.id == recipient.id).with_for_update()
    )
    key = grouping_key(policy, context)
    item = {
        "event_id": str(event.id),
        "occurred_at": event.occurred_at.isoformat(),
        "data": event.data,
    }
    digest = await session.scalar(
        select(Digest).where(
            Digest.rule_id == rule.id,
            Digest.recipient_id == recipient.id,
            Digest.key == key,
            Digest.status == DigestStatus.OPEN,
        )
    )
    if digest is None:
        now = utcnow()
        digest = Digest(
            rule_id=rule.id,
            recipient_id=recipient.id,
            key=key,
            status=DigestStatus.OPEN,
            items=[item],
            count=1,
            closes_at=now + policy.window,
        )
        session.add(digest)
        await session.flush()
        await queue.enqueue(
            session,
            FLUSH_JOB,
            {"digest_id": str(digest.id)},
            run_at=digest.closes_at,
            key=_job_key(digest),
            max_attempts=runtime.settings.worker.max_attempts,
        )
        return digest

    digest.count += 1
    # Workers process events concurrently, so keep the group in the order things happened.
    items = sorted([*digest.items, item], key=_chronological)
    digest.items = items[-policy.max_items :]
    if policy.send_after is not None and digest.count >= policy.send_after:
        await queue.reschedule(session, _job_key(digest), utcnow())
    return digest


def template_data(digest: Digest) -> dict[str, Any]:
    """What a digest template sees as ``data``.

    The grouped events are ``data.events`` (not ``items``, which Jinja would resolve to
    the dict method).
    """
    events = [item["data"] for item in digest.items]
    return {
        "count": digest.count,
        "events": events,
        "first": events[0] if events else {},
        "last": events[-1] if events else {},
        "omitted": max(digest.count - len(events), 0),
        "key": digest.key,
    }


def _ai_context(policy: DigestPolicy, digest: Digest) -> dict[str, Any] | None:
    if not policy.summarize:
        return None
    updates = [
        {field: item["data"][field] for field in policy.summary_fields if field in item["data"]}
        for item in digest.items
    ]
    return {"grouped_updates": updates, "update_count": digest.count}


async def flush(ctx: queue.JobContext) -> None:
    """Job handler: turn an open digest into a message."""
    runtime = ctx.runtime
    digest_id = UUID(ctx.payload["digest_id"])
    async with runtime.transaction() as session:
        digest = await session.get(Digest, digest_id, with_for_update=True)
        if digest is None or digest.status is not DigestStatus.OPEN:
            return
        rule = await session.get(Rule, digest.rule_id)
        recipient = await session.get(Recipient, digest.recipient_id)
        if rule is None or recipient is None or not rule.enabled or rule.digest is None:
            digest.status = DigestStatus.CANCELLED
            return
        template = await session.scalar(select(Template).where(Template.key == rule.template_key))
        category = await session.get(Category, rule.category_key)
        if template is None or category is None:
            raise queue.PermanentJobError("digest rule's template or category was deleted")
        policy = DigestPolicy.model_validate(rule.digest)
        cooldown = timedelta(seconds=rule.cooldown_seconds) if rule.cooldown_seconds else None
        message, _ = await composer.compose(
            session,
            runtime,
            composer.Intent(
                recipient=recipient,
                template=template,
                category=category,
                channels=list(rule.channels),
                data=template_data(digest),
                rule=rule,
                cooldown=cooldown,
                dedup_key=f"digest:{digest.id}",
                personalize=policy.summarize,
                ai_context=_ai_context(policy, digest),
            ),
        )
        digest.status = DigestStatus.SENT
        digest.sent_at = utcnow()
        digest.message_id = message.id
    logger.info(
        "digest sent",
        extra={"digest_id": str(digest_id), "count": digest.count, "status": message.status},
    )


async def flush_dead(ctx: queue.JobContext, error: str) -> None:
    """Retries exhausted: close the digest so new events open a fresh one."""
    async with ctx.runtime.transaction() as session:
        digest = await session.get(Digest, UUID(ctx.payload["digest_id"]))
        if digest is not None and digest.status is DigestStatus.OPEN:
            digest.status = DigestStatus.CANCELLED
            logger.error("digest abandoned: %s", error, extra={"digest_id": str(digest.id)})


async def get(session: AsyncSession, digest_id: UUID) -> Digest:
    digest = await session.get(Digest, digest_id)
    if digest is None:
        raise NotFoundError("digest", digest_id)
    return digest


async def send_now(session: AsyncSession, digest: Digest) -> Digest:
    """Close the window early; the flush job runs on the worker's next poll."""
    if digest.status is DigestStatus.OPEN:
        await queue.reschedule(session, _job_key(digest), utcnow())
    return digest
