from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from cue_notify.api.deps import (
    EventsWriter,
    MessagesReader,
    MessagesWriter,
    PageDep,
    RuntimeDep,
    SessionDep,
    paginate,
)
from cue_notify.api.schemas import MessageOut, Page, SendIn
from cue_notify.core.enums import MessageStatus
from cue_notify.core.time import ensure_utc
from cue_notify.db.models import Message
from cue_notify.services import catalog, composer, engagement
from cue_notify.services import recipients as recipient_service
from cue_notify.services.errors import InvalidStateError, NotFoundError

router = APIRouter(prefix="/messages", tags=["messages"])
tracking = APIRouter(prefix="/track", tags=["messages"])


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

    The message still passes through preferences, caps and quiet hours of its category.
    Check `status`: it may be `suppressed` with a `status_reason`.
    """
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
        dedup_key=f"api:{key.id}:{body.idempotency_key}" if body.idempotency_key else None,
    )
    message, created = await composer.compose(session, runtime, intent)
    if not created:
        response.status_code = status.HTTP_200_OK
    return MessageOut.model_validate(message)


@router.get("", summary="List messages")
async def list_messages(
    _: MessagesReader,
    session: SessionDep,
    page: PageDep,
    recipient: Annotated[str | None, Query(description="Filter by external id.")] = None,
    status_: Annotated[MessageStatus | None, Query(alias="status")] = None,
    event_id: UUID | None = None,
    broadcast_id: UUID | None = None,
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
    items, cursor = await paginate(session, stmt, Message.id, page)
    return Page(items=[MessageOut.model_validate(item) for item in items], next_cursor=cursor)


@router.get("/{message_id}", summary="Get a message")
async def get_message(message_id: UUID, _: MessagesReader, session: SessionDep) -> MessageOut:
    return MessageOut.model_validate(await _get(session, message_id))


@router.post("/{message_id}/cancel", summary="Cancel a queued message")
async def cancel_message(message_id: UUID, _: MessagesWriter, session: SessionDep) -> MessageOut:
    message = await _get(session, message_id)
    if message.status is not MessageStatus.QUEUED:
        raise InvalidStateError(
            f"message is {message.status}; only queued messages can be cancelled"
        )
    message.status = MessageStatus.CANCELLED
    message.status_reason = "cancelled via API"
    return MessageOut.model_validate(message)


@router.post("/{message_id}/engagement", summary="Report delivery or engagement")
async def report_engagement(
    message_id: UUID, body: engagement.EngagementIn, _: EventsWriter, session: SessionDep
) -> MessageOut:
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
