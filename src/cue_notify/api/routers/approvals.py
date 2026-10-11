from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from cue_notify.api.deps import (
    MessagesReader,
    MessagesWriter,
    PageDep,
    RuntimeDep,
    SessionDep,
    paginate,
)
from cue_notify.api.schemas import ApprovalOut, Page, ReviewIn
from cue_notify.core.enums import ApprovalStatus
from cue_notify.db.models import Approval
from cue_notify.services import approvals

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.post("", status_code=status.HTTP_201_CREATED, summary="Ask a person to decide")
async def create_approval(
    body: approvals.ApprovalIn, key: MessagesWriter, session: SessionDep, runtime: RuntimeDep
) -> ApprovalOut:
    """Ask `reviewers` to approve or reject something outside Cue (a refund, a deletion,
    publishing a draft). Each reviewer is told through your rules for the
    `cue.approval_requested` event, with a one-time `review_url`. The decision arrives
    at `approvals.callback_url` and here; undecided requests expire."""
    return ApprovalOut.model_validate(await approvals.create(session, runtime, body, key))


@router.get("", summary="List approval requests")
async def list_approvals(
    key: MessagesReader,
    session: SessionDep,
    page: PageDep,
    status_: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> Page[ApprovalOut]:
    stmt = approvals.visible_to(key)
    if status_ is not None:
        stmt = stmt.where(Approval.status == status_)
    items, cursor = await paginate(session, stmt, Approval.id, page)
    return Page(items=[ApprovalOut.model_validate(item) for item in items], next_cursor=cursor)


@router.get("/{approval_id}", summary="Get an approval request and its decision")
async def get_approval(approval_id: UUID, key: MessagesReader, session: SessionDep) -> ApprovalOut:
    return ApprovalOut.model_validate(await approvals.get(session, approval_id, key))


@router.post("/{approval_id}/approve", summary="Approve a request")
async def approve(
    approval_id: UUID,
    key: MessagesWriter,
    session: SessionDep,
    runtime: RuntimeDep,
    body: ReviewIn | None = None,
) -> ApprovalOut:
    approval = await approvals.decide(
        session, runtime, approval_id, key, approve=True, comment=body.reason if body else None
    )
    return ApprovalOut.model_validate(approval)


@router.post("/{approval_id}/reject", summary="Reject a request")
async def reject(
    approval_id: UUID,
    key: MessagesWriter,
    session: SessionDep,
    runtime: RuntimeDep,
    body: ReviewIn | None = None,
) -> ApprovalOut:
    approval = await approvals.decide(
        session, runtime, approval_id, key, approve=False, comment=body.reason if body else None
    )
    return ApprovalOut.model_validate(approval)
