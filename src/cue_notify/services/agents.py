"""Agents: automated senders governed by an attention budget and, optionally, approval.

Many senders now are AI agents acting for a product: a support agent, a sales agent, a
coding agent reporting back. Each gets its own API key bound to an agent, and Cue applies
the agent's rules on top of everything else:

- which categories it may use, and the highest importance it may claim;
- how many messages it may send each person per period (its attention budget);
- when a person must approve a message first (a JSON Logic condition).

Approvals are recorded on the message (who reviewed it, when). Agent keys can never
review messages, hold the ``admin`` scope (which would let them loosen their own limits),
or change people's preferences.

Reviewers can also decide from the notification itself: each one gets a one-time link
(``review_url``) that expires with the message. Only a hash of its token is stored.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify import queue
from cue_notify.core.content import DEFAULT_VARIANT
from cue_notify.core.enums import MessageStatus, SuppressionReason
from cue_notify.core.time import utcnow
from cue_notify.db.models import Agent, ApiKey, Message, Recipient, ReviewLink
from cue_notify.runtime import Runtime
from cue_notify.services import composer, events
from cue_notify.services.errors import (
    ConflictError,
    ForbiddenError,
    GoneError,
    InvalidStateError,
    NotFoundError,
)
from cue_notify.settings import Settings


async def get(session: AsyncSession, key: str) -> Agent:
    agent = await session.get(Agent, key)
    if agent is None:
        raise NotFoundError("agent", key)
    return agent


async def for_key(session: AsyncSession, api_key: ApiKey) -> Agent | None:
    """The agent an API key sends for, refusing keys of a disabled agent."""
    if api_key.agent_key is None:
        return None
    agent = await get(session, api_key.agent_key)
    if not agent.enabled:
        raise ForbiddenError(f"agent {agent.key} is disabled")
    return agent


def check_not_agent(api_key: ApiKey, action: str) -> None:
    if api_key.agent_key is not None:
        raise ForbiddenError(f"agent keys cannot {action}")


def check_category(agent: Agent, category: str) -> None:
    if agent.categories is not None and category not in agent.categories:
        raise ForbiddenError(f"agent {agent.key} may not send {category} messages")


async def delete(session: AsyncSession, agent: Agent) -> None:
    bound = await session.scalar(select(exists().where(ApiKey.agent_key == agent.key)))
    if bound:
        raise ConflictError(
            f"agent {agent.key} still has API keys; delete or disable the agent's keys first"
        )
    await session.delete(agent)


async def _pending(session: AsyncSession, message_id: object, reviewer: ApiKey) -> Message:
    message = await session.get(Message, message_id, with_for_update=True)
    if message is None:
        raise NotFoundError("message", message_id)
    if message.status is not MessageStatus.PENDING_APPROVAL:
        raise InvalidStateError(f"message is {message.status}, not waiting for approval")
    if reviewer.agent_key is not None:
        # Not even another agent's: two agents could otherwise approve each other.
        raise ForbiddenError("agent keys cannot review messages; a person must")
    return message


async def approve(
    session: AsyncSession, runtime: Runtime, message_id: object, reviewer: ApiKey
) -> Message:
    """Release a held message for delivery. One that expired while waiting is dropped."""
    message = await _pending(session, message_id, reviewer)
    await _release(session, runtime, message, reviewer.name)
    return message


async def _release(
    session: AsyncSession, runtime: Runtime, message: Message, reviewer: str
) -> None:
    now = utcnow()
    message.reviewed_by = reviewer[:100]
    message.reviewed_at = now
    await _close_links(session, message, now)
    if message.expires_at is not None and message.expires_at <= now:
        message.status = MessageStatus.SUPPRESSED
        message.status_reason = SuppressionReason.EXPIRED
        return
    message.status = MessageStatus.QUEUED
    message.status_reason = None
    message.send_at = max(message.send_at, now)
    await composer.enqueue_delivery(session, runtime, message)


def _cancel(message: Message, reviewer: str, reason: str | None) -> None:
    message.status = MessageStatus.CANCELLED
    message.status_reason = f"rejected: {reason}"[:500] if reason else "rejected"
    message.reviewed_by = reviewer[:100]
    message.reviewed_at = utcnow()


APPROVAL_EVENT = "cue.approval_requested"
PREVIEW_CHARS = 500


async def request_review(ctx: queue.JobContext) -> None:
    """Job handler: report a ``cue.approval_requested`` event for each reviewer.

    Your rules decide how reviewers hear about it, through the same templates, channels
    and preferences as everything else.
    """
    runtime = ctx.runtime
    async with runtime.transaction() as session:
        message = await session.get(Message, UUID(ctx.payload["message_id"]))
        if message is None or message.status is not MessageStatus.PENDING_APPROVAL:
            return
        agent = await session.get(Agent, message.agent_key) if message.agent_key else None
        recipient = await session.get(Recipient, message.recipient_id)
        if agent is None or recipient is None:
            return
        content = message.content.get(DEFAULT_VARIANT, {})
        data = {
            "message_id": str(message.id),
            "agent": agent.key,
            "agent_name": agent.name,
            "recipient": recipient.external_id,
            "category": message.category_key,
            "importance": message.importance.value,
            "title": content.get("title"),
            "body": str(content.get("body", ""))[:PREVIEW_CHARS],
            "expires_at": message.expires_at.isoformat() if message.expires_at else None,
        }
        for reviewer in agent.reviewers:
            token = await create_review_link(session, message, reviewer)
            await events.ingest(
                session,
                runtime,
                events.EventIn(
                    name=APPROVAL_EVENT,
                    recipient=reviewer,
                    data={**data, "review_url": review_url(runtime.settings, token)},
                    idempotency_key=f"{message.id}:{reviewer}",
                ),
                source="cue",
            )


async def reject(
    session: AsyncSession, message_id: object, reviewer: ApiKey, reason: str | None
) -> Message:
    message = await _pending(session, message_id, reviewer)
    _cancel(message, reviewer.name, reason)
    await _close_links(session, message, utcnow())
    return message


# --------------------------------------------------------------------------- review links

REVIEW_LINK_TTL = timedelta(days=1)
"""How long a link lives when the message itself has no expiry."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_review_link(session: AsyncSession, message: Message, reviewer: str) -> str:
    """A one-time token letting ``reviewer`` decide on ``message`` until it expires."""
    token = secrets.token_urlsafe(32)
    expires_at = message.expires_at or utcnow() + REVIEW_LINK_TTL
    session.add(
        ReviewLink(
            token_hash=_hash(token), message_id=message.id, reviewer=reviewer, expires_at=expires_at
        )
    )
    await session.flush()
    return token


def review_url(settings: Settings, token: str) -> str | None:
    if settings.api.public_url is None:
        return None
    return f"{str(settings.api.public_url).rstrip('/')}/review/{token}"


async def _close_links(session: AsyncSession, message: Message, now: datetime) -> None:
    links = await session.scalars(
        select(ReviewLink).where(ReviewLink.message_id == message.id, ReviewLink.used_at.is_(None))
    )
    for link in links:
        link.used_at = now


async def by_review_token(
    session: AsyncSession, token: str, *, lock: bool = True
) -> tuple[ReviewLink, Message]:
    """The link and its message, or why the link can no longer be used."""
    link = await session.get(ReviewLink, _hash(token), with_for_update=lock)
    if link is None:
        raise NotFoundError("review link", "…")
    message = await session.get(Message, link.message_id, with_for_update=lock)
    if message is None:
        raise NotFoundError("message", link.message_id)
    if link.used_at is not None or message.status is not MessageStatus.PENDING_APPROVAL:
        who = f" by {message.reviewed_by}" if message.reviewed_by else ""
        raise GoneError(f"This message was already reviewed{who}; it is now {message.status}.")
    if link.expires_at <= utcnow():
        raise GoneError("This review link has expired.")
    return link, message


async def decide_by_link(
    session: AsyncSession, runtime: Runtime, token: str, *, approve: bool, reason: str | None
) -> Message:
    link, message = await by_review_token(session, token)
    reviewer = f"link:{link.reviewer}"
    if approve:
        await _release(session, runtime, message, reviewer)
    else:
        _cancel(message, reviewer, reason)
        await _close_links(session, message, utcnow())
    return message
