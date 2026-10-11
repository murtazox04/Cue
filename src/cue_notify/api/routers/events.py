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
    may_change_profile,
    paginate,
)
from cue_notify.api.schemas import EventAccepted, EventOut, Page
from cue_notify.api.views import event_views
from cue_notify.db.models import Event
from cue_notify.services import agents
from cue_notify.services import events as event_service
from cue_notify.services import recipients as recipient_service
from cue_notify.services.errors import ForbiddenError, NotFoundError

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
    keys are unique per `source` (default: the API key's name; always `agent:<key>` for
    agent keys). Adding addresses inline requires the `recipients:write` scope. Names
    starting with `cue.` and the source `cue` are reserved for Cue's own events.
    """
    check_inline_profile(key, body.recipient)
    if body.name.startswith("cue.") or body.source == event_service.SYSTEM_SOURCE:
        # Cue's own events (approval requests, for instance) carry review links;
        # a forged one would be a phishing message that looks like Cue's.
        raise ForbiddenError("event names starting with 'cue.' and source 'cue' are reserved")
    agent = await agents.for_key(session, key)
    event, created = await event_service.ingest(
        session,
        runtime,
        body,
        # An agent's events get a source of their own, so it cannot claim (and thereby
        # suppress) another producer's idempotency keys.
        source=f"agent:{agent.key}" if agent else body.source or key.name,
        agent_key=agent.key if agent else None,
        may_change_profile=may_change_profile(key),
    )
    if created:
        runtime.metrics.event_ingested(event.name)
    else:
        response.status_code = status.HTTP_200_OK
    return EventAccepted(id=event.id, status=event.status, duplicate=not created)


@router.get("", summary="List events")
async def list_events(
    key: MessagesReader,
    session: SessionDep,
    runtime: RuntimeDep,
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
    if key.agent_key is not None:  # agents see only what they reported
        stmt = stmt.where(Event.agent_key == key.agent_key)
    items, cursor = await paginate(session, stmt, Event.id, page)
    return Page(items=await event_views(session, runtime.settings, key, items), next_cursor=cursor)


@router.get("/{event_id}", summary="Get an event and its rule outcomes")
async def get_event(
    event_id: UUID, key: MessagesReader, session: SessionDep, runtime: RuntimeDep
) -> EventOut:
    event = await event_service.get(session, event_id)
    if key.agent_key is not None and event.agent_key != key.agent_key:
        raise NotFoundError("event", event_id)
    return (await event_views(session, runtime.settings, key, [event]))[0]
