from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import select

from cue_notify.api.deps import Admin, PageDep, RuntimeDep, SessionDep, paginate
from cue_notify.api.schemas import BroadcastIn, BroadcastOut, Page, StartIn
from cue_notify.core.enums import BroadcastStatus
from cue_notify.db.models import Broadcast
from cue_notify.services import broadcasts as broadcast_service
from cue_notify.services import catalog
from cue_notify.services.errors import InvalidStateError

router = APIRouter(prefix="/broadcasts", tags=["broadcasts"])


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a draft broadcast")
async def create_broadcast(
    body: BroadcastIn, _: Admin, session: SessionDep, runtime: RuntimeDep
) -> BroadcastOut:
    await catalog.template(session, body.template)
    await catalog.category(session, body.category)
    broadcast = Broadcast(
        name=body.name,
        template_key=body.template,
        category_key=body.category,
        channels=catalog.channels(runtime, body.channels),
        audience=body.audience.model_dump(mode="json"),
        variables=body.variables,
        status=BroadcastStatus.DRAFT,
        stats={},
    )
    session.add(broadcast)
    await session.flush()
    return BroadcastOut.model_validate(broadcast)


@router.get("", summary="List broadcasts")
async def list_broadcasts(_: Admin, session: SessionDep, page: PageDep) -> Page[BroadcastOut]:
    items, cursor = await paginate(session, select(Broadcast), Broadcast.id, page)
    return Page(items=[BroadcastOut.model_validate(item) for item in items], next_cursor=cursor)


@router.get("/{broadcast_id}", summary="Get a broadcast and its progress")
async def get_broadcast(broadcast_id: UUID, _: Admin, session: SessionDep) -> BroadcastOut:
    return BroadcastOut.model_validate(await broadcast_service.get(session, broadcast_id))


@router.delete("/{broadcast_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a draft")
async def delete_broadcast(broadcast_id: UUID, _: Admin, session: SessionDep) -> None:
    broadcast = await broadcast_service.get(session, broadcast_id)
    if broadcast.status is not BroadcastStatus.DRAFT:
        raise InvalidStateError("only drafts can be deleted; cancel it instead")
    await session.delete(broadcast)


@router.post("/{broadcast_id}/start", summary="Start now or schedule")
async def start_broadcast(
    broadcast_id: UUID, body: StartIn, _: Admin, session: SessionDep, runtime: RuntimeDep
) -> BroadcastOut:
    broadcast = await broadcast_service.get(session, broadcast_id)
    return BroadcastOut.model_validate(
        await broadcast_service.start(session, runtime, broadcast, body.at)
    )


@router.post("/{broadcast_id}/pause", summary="Pause after the current batch")
async def pause_broadcast(broadcast_id: UUID, _: Admin, session: SessionDep) -> BroadcastOut:
    broadcast = await broadcast_service.get(session, broadcast_id)
    return BroadcastOut.model_validate(await broadcast_service.pause(broadcast))


@router.post("/{broadcast_id}/resume", summary="Resume where it stopped")
async def resume_broadcast(
    broadcast_id: UUID, _: Admin, session: SessionDep, runtime: RuntimeDep
) -> BroadcastOut:
    broadcast = await broadcast_service.get(session, broadcast_id)
    return BroadcastOut.model_validate(await broadcast_service.resume(session, runtime, broadcast))


@router.post("/{broadcast_id}/cancel", summary="Cancel and withdraw unsent messages")
async def cancel_broadcast(broadcast_id: UUID, _: Admin, session: SessionDep) -> BroadcastOut:
    broadcast = await broadcast_service.get(session, broadcast_id)
    return BroadcastOut.model_validate(await broadcast_service.cancel(session, broadcast))
