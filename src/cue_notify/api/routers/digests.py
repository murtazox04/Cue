from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from cue_notify.api.deps import (
    MessagesReader,
    MessagesWriter,
    PageDep,
    RuntimeDep,
    SessionDep,
    paginate,
)
from cue_notify.api.schemas import DigestOut, Page
from cue_notify.api.views import digest_views
from cue_notify.core.enums import DigestStatus
from cue_notify.db.models import Digest
from cue_notify.services import agents
from cue_notify.services import digests as digest_service
from cue_notify.services import recipients as recipient_service

router = APIRouter(prefix="/digests", tags=["digests"])


@router.get("", summary="List digests")
async def list_digests(
    key: MessagesReader,
    session: SessionDep,
    runtime: RuntimeDep,
    page: PageDep,
    recipient: Annotated[str | None, Query(description="Filter by external id.")] = None,
    status: DigestStatus | None = None,
) -> Page[DigestOut]:
    agents.check_not_agent(key, "read digests")
    stmt = select(Digest)
    if recipient is not None:
        found = await recipient_service.get(session, recipient)
        stmt = stmt.where(Digest.recipient_id == found.id)
    if status is not None:
        stmt = stmt.where(Digest.status == status)
    items, cursor = await paginate(session, stmt, Digest.id, page)
    return Page(items=await digest_views(session, runtime.settings, key, items), next_cursor=cursor)


@router.get("/{digest_id}", summary="Get a digest and its grouped events")
async def get_digest(
    digest_id: UUID, key: MessagesReader, session: SessionDep, runtime: RuntimeDep
) -> DigestOut:
    agents.check_not_agent(key, "read digests")
    digest = await digest_service.get(session, digest_id)
    return (await digest_views(session, runtime.settings, key, [digest]))[0]


@router.post("/{digest_id}/send", summary="Send an open digest now instead of at window end")
async def send_digest(
    digest_id: UUID, key: MessagesWriter, session: SessionDep, runtime: RuntimeDep
) -> DigestOut:
    agents.check_not_agent(key, "send digests")
    digest = await digest_service.get(session, digest_id)
    sent = await digest_service.send_now(session, digest)
    return (await digest_views(session, runtime.settings, key, [sent]))[0]
