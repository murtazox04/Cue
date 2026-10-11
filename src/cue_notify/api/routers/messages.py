from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select, update

from cue_notify.api.deps import (
    EventsWriter,
    MessagesReader,
    MessagesWriter,
    PageDep,
    RuntimeDep,
    SessionDep,
    check_inline_profile,
    paginate,
)
from cue_notify.api.schemas import MessageOut, Page, ReviewIn, SendIn
from cue_notify.core.enums import MessageStatus
from cue_notify.core.time import ensure_utc
from cue_notify.db.models import ApiKey, Message, Recipient
from cue_notify.services import agents, catalog, composer, engagement
from cue_notify.services import recipients as recipient_service
from cue_notify.services.errors import ForbiddenError, InvalidStateError, NotFoundError

router = APIRouter(prefix="/messages", tags=["messages"])
tracking = APIRouter(prefix="/track", tags=["messages"])


def _redact(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {k: _redact(v, secrets) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


async def _views(session: SessionDep, key: ApiKey, messages: Sequence[Message]) -> list[MessageOut]:
    """Messages as ``key`` may see them. Rendered content can contain preference and
    unsubscribe links; agent keys get them redacted, since the token would let them
    change people's preferences."""
    views = [MessageOut.model_validate(message) for message in messages]
    if key.agent_key is None or not views:
        return views
    tokens = set(
        (
            await session.scalars(
                select(Recipient.preference_token).where(
                    Recipient.id.in_({message.recipient_id for message in messages})
                )
            )
        ).all()
    )
    return [view.model_copy(update={"content": _redact(view.content, tokens)}) for view in views]


async def _get(session: SessionDep, message_id: UUID) -> Message:
    message = await session.get(Message, message_id)
    if message is None:
        raise NotFoundError("message", message_id)
    return message


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a message directly",
    responses={200: {"model": MessageOut, "description": "Duplicate idempotency key"}},
)
async def send_message(
    body: SendIn, key: MessagesWriter, session: SessionDep, runtime: RuntimeDep, response: Response
) -> MessageOut:
    """Send a template to one recipient without an event rule — OTPs, receipts, alerts.

    The message still passes through preferences, caps and quiet hours of its category,
    and, for keys bound to an agent, that agent's budget and approval policy. Check
    `status`: it may be `suppressed` (see `status_reason`) or `pending_approval`.
    """
    check_inline_profile(key, body.recipient)
    agent = await agents.for_key(session, key)
    if agent is not None:
        agents.check_category(agent, body.category)
    if isinstance(body.recipient, recipient_service.RecipientIn):
        recipient = await recipient_service.upsert(session, body.recipient)
    else:
        recipient = await recipient_service.ensure(session, body.recipient)
    intent = composer.Intent(
        recipient=recipient,
        template=await catalog.template(session, body.template),
        category=await catalog.category(session, body.category),
        channels=catalog.channels(runtime, body.channels),
        data=body.data,
        send_at=ensure_utc(body.send_at) if body.send_at else None,
        importance=body.importance,
        agent=agent,
        expires_at=ensure_utc(body.expires_at) if body.expires_at else None,
        dedup_key=f"api:{key.id}:{body.idempotency_key}" if body.idempotency_key else None,
    )
    message, created = await composer.compose(session, runtime, intent)
    if not created:
        response.status_code = status.HTTP_200_OK
    return (await _views(session, key, [message]))[0]


@router.get("", summary="List messages")
async def list_messages(
    key: MessagesReader,
    session: SessionDep,
    page: PageDep,
    recipient: Annotated[str | None, Query(description="Filter by external id.")] = None,
    status_: Annotated[MessageStatus | None, Query(alias="status")] = None,
    event_id: UUID | None = None,
    broadcast_id: UUID | None = None,
    agent: str | None = None,
) -> Page[MessageOut]:
    stmt = select(Message)
    if recipient is not None:
        found = await recipient_service.get(session, recipient)
        stmt = stmt.where(Message.recipient_id == found.id)
    if status_ is not None:
        stmt = stmt.where(Message.status == status_)
    if event_id is not None:
        stmt = stmt.where(Message.event_id == event_id)
    if broadcast_id is not None:
        stmt = stmt.where(Message.broadcast_id == broadcast_id)
    if agent is not None:
        stmt = stmt.where(Message.agent_key == agent)
    if key.agent_key is not None:  # agents see only what they sent
        stmt = stmt.where(Message.agent_key == key.agent_key)
    items, cursor = await paginate(session, stmt, Message.id, page)
    return Page(items=await _views(session, key, items), next_cursor=cursor)


@router.get("/{message_id}", summary="Get a message")
async def get_message(message_id: UUID, key: MessagesReader, session: SessionDep) -> MessageOut:
    message = await _get(session, message_id)
    if key.agent_key is not None and message.agent_key != key.agent_key:
        raise NotFoundError("message", message_id)
    return (await _views(session, key, [message]))[0]


@router.post("/{message_id}/approve", summary="Approve a message held for review")
async def approve_message(
    message_id: UUID, key: MessagesWriter, session: SessionDep, runtime: RuntimeDep
) -> MessageOut:
    """Releases it for delivery (policy is checked again at send time). A message that
    expired while waiting comes back `suppressed` with reason `expired`. Agent keys cannot
    review messages."""
    return MessageOut.model_validate(await agents.approve(session, runtime, message_id, key))


@router.post("/{message_id}/reject", summary="Reject a message held for review")
async def reject_message(
    message_id: UUID, body: ReviewIn, key: MessagesWriter, session: SessionDep
) -> MessageOut:
    return MessageOut.model_validate(await agents.reject(session, message_id, key, body.reason))


@router.post("/{message_id}/cancel", summary="Cancel a queued message")
async def cancel_message(message_id: UUID, key: MessagesWriter, session: SessionDep) -> MessageOut:
    """Agent keys can only cancel the agent's own messages."""
    message = await _get(session, message_id)
    if key.agent_key is not None and message.agent_key != key.agent_key:
        raise ForbiddenError("agent keys can only cancel their own messages")
    # Conditional update: a worker that already claimed the message wins the race.
    cancelled = await session.scalar(
        update(Message)
        .where(Message.id == message_id, Message.status == MessageStatus.QUEUED)
        .values(status=MessageStatus.CANCELLED, status_reason="cancelled via API")
        .returning(Message.id)
    )
    await session.refresh(message)
    if cancelled is None:
        raise InvalidStateError(
            f"message is {message.status}; only queued messages can be cancelled"
        )
    return (await _views(session, key, [message]))[0]


@router.post("/{message_id}/engagement", summary="Report delivery or engagement")
async def report_engagement(
    message_id: UUID, body: engagement.EngagementIn, key: EventsWriter, session: SessionDep
) -> MessageOut:
    # Engagement steers fatigue; it comes from your apps and backend, not from agents.
    agents.check_not_agent(key, "report engagement")
    return MessageOut.model_validate(engagement.record(await _get(session, message_id), body))


@tracking.post(
    "/{tracking_token}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Report engagement from a client app (no API key)",
)
async def track(tracking_token: str, body: engagement.EngagementIn, session: SessionDep) -> None:
    """Apps receive `cue_tracking_token` in the message payload and post it back here
    when the notification is shown, opened or acted upon. The token only grants this."""
    message = await session.scalar(select(Message).where(Message.tracking_token == tracking_token))
    if message is None:
        raise NotFoundError("message", "for this tracking token")
    engagement.record(message, body)
