from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from cue_notify.api.deps import (
    EventsWriter,
    MessagesReader,
    PageDep,
    RuntimeDep,
    SessionDep,
    check_inline_profile,
    paginate,
)
from cue_notify.api.schemas import EventAccepted, EventOut, Page
from cue_notify.db.models import Event
from cue_notify.services import events as event_service
from cue_notify.services import recipients as recipient_service

router = APIRouter(prefix="/events", tags=["events"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest an event",
    responses={200: {"model": EventAccepted, "description": "Duplicate of an earlier event"}},
)
async def ingest_event(
    body: event_service.EventIn,
    key: EventsWriter,
    session: SessionDep,
    runtime: RuntimeDep,
    response: Response,
) -> EventAccepted:
    """Record something that happened to a recipient. Matching rules run asynchronously.

    Send an `idempotency_key` so retries never produce duplicate notifications;
    keys are unique per `source` (default: the API key's name). Adding addresses
    inline requires the `recipients:write` scope.
    """
    check_inline_profile(key, body.recipient)
    event, created = await event_service.ingest(
        session, runtime, body, source=body.source or key.name
    )
    if created:
        runtime.metrics.event_ingested(event.name)
    else:
        response.status_code = status.HTTP_200_OK
    return EventAccepted(id=event.id, status=event.status, duplicate=not created)


@router.get("", summary="List events")
async def list_events(
    _: MessagesReader,
    session: SessionDep,
    page: PageDep,
    recipient: Annotated[str | None, Query(description="Filter by external id.")] = None,
    name: str | None = None,
) -> Page[EventOut]:
    stmt = select(Event)
    if recipient is not None:
        found = await recipient_service.get(session, recipient)
        stmt = stmt.where(Event.recipient_id == found.id)
    if name is not None:
        stmt = stmt.where(Event.name == name)
    items, cursor = await paginate(session, stmt, Event.id, page)
    return Page(items=[EventOut.model_validate(item) for item in items], next_cursor=cursor)


@router.get("/{event_id}", summary="Get an event and its rule outcomes")
async def get_event(event_id: UUID, _: MessagesReader, session: SessionDep) -> EventOut:
    return EventOut.model_validate(await event_service.get(session, event_id))
