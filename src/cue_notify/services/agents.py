"""Agents: automated senders governed by an attention budget and, optionally, approval.

Many senders now are AI agents acting for a product: a support agent, a sales agent, a
coding agent reporting back. Each gets its own API key bound to an agent, and Cue applies
the agent's rules on top of everything else:

- which categories it may use, and the highest importance it may claim;
- how many messages it may send each person per period (its attention budget);
- when a person must approve a message first (a JSON Logic condition).

Approvals are recorded on the message (who reviewed it, when), and an agent can never
approve its own messages.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.enums import MessageStatus, SuppressionReason
from cue_notify.core.time import utcnow
from cue_notify.db.models import Agent, ApiKey, Message
from cue_notify.runtime import Runtime
from cue_notify.services import composer
from cue_notify.services.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
)


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
    if reviewer.agent_key is not None and reviewer.agent_key == message.agent_key:
        raise ForbiddenError("an agent cannot review its own messages")
    return message


async def approve(
    session: AsyncSession, runtime: Runtime, message_id: object, reviewer: ApiKey
) -> Message:
    """Release a held message for delivery. One that expired while waiting is dropped."""
    message = await _pending(session, message_id, reviewer)
    now = utcnow()
    message.reviewed_by = reviewer.name
    message.reviewed_at = now
    if message.expires_at is not None and message.expires_at <= now:
        message.status = MessageStatus.SUPPRESSED
        message.status_reason = SuppressionReason.EXPIRED
        return message
    message.status = MessageStatus.QUEUED
    message.status_reason = None
    message.send_at = max(message.send_at, now)
    await composer.enqueue_delivery(session, runtime, message)
    return message


async def reject(
    session: AsyncSession, message_id: object, reviewer: ApiKey, reason: str | None
) -> Message:
    message = await _pending(session, message_id, reviewer)
    message.status = MessageStatus.CANCELLED
    message.status_reason = f"rejected: {reason}"[:500] if reason else "rejected"
    message.reviewed_by = reviewer.name
    message.reviewed_at = utcnow()
    return message
