from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from cue_notify.api.deps import MessagesReader, MessagesWriter, PageDep, SessionDep, paginate
from cue_notify.api.schemas import DigestOut, Page
from cue_notify.core.enums import DigestStatus
from cue_notify.db.models import Digest
from cue_notify.services import digests as digest_service
from cue_notify.services import recipients as recipient_service

router = APIRouter(prefix="/digests", tags=["digests"])


@router.get("", summary="List digests")
async def list_digests(
    _: MessagesReader,
    session: SessionDep,
    page: PageDep,
    recipient: Annotated[str | None, Query(description="Filter by external id.")] = None,
    status: DigestStatus | None = None,
) -> Page[DigestOut]:
    stmt = select(Digest)
    if recipient is not None:
        found = await recipient_service.get(session, recipient)
        stmt = stmt.where(Digest.recipient_id == found.id)
    if status is not None:
        stmt = stmt.where(Digest.status == status)
    items, cursor = await paginate(session, stmt, Digest.id, page)
    return Page(items=[DigestOut.model_validate(item) for item in items], next_cursor=cursor)


@router.get("/{digest_id}", summary="Get a digest and its grouped events")
async def get_digest(digest_id: UUID, _: MessagesReader, session: SessionDep) -> DigestOut:
    return DigestOut.model_validate(await digest_service.get(session, digest_id))


@router.post("/{digest_id}/send", summary="Send an open digest now instead of at window end")
async def send_digest(digest_id: UUID, _: MessagesWriter, session: SessionDep) -> DigestOut:
    digest = await digest_service.get(session, digest_id)
    return DigestOut.model_validate(await digest_service.send_now(session, digest))
