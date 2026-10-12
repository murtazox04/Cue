"""Approval requests: ask a person to decide on something an agent wants to do.

A held *message* is approved inside Cue. An approval request is about an action outside
it: deleting data, issuing a refund, publishing a draft. The agent (or your service)
asks; Cue tells each reviewer through your own rules and channels, with a one-time link
to decide from the notification; the decision is recorded and sent back:

- to ``approvals.callback_url`` as a signed ``POST`` (the same ``Cue-Signature`` scheme as
  the webhook connector, so the SDKs' ``verify_webhook`` checks it), and
- to anyone polling ``GET /v1/approvals/{id}``.

A request nobody decides on before ``expires_at`` becomes ``expired``: treat it as a no.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

import httpx
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify import queue
from cue_notify.channels.webhook import SIGNATURE_HEADER, sign
from cue_notify.core.enums import ApprovalStatus, Importance, Scope
from cue_notify.core.limits import bounded_data
from cue_notify.core.time import utcnow
from cue_notify.db.models import Agent, ApiKey, Approval, ReviewLink
from cue_notify.runtime import Runtime
from cue_notify.services import agents, api_keys, events, review_links
from cue_notify.services.errors import (
    ForbiddenError,
    GoneError,
    InvalidStateError,
    NotFoundError,
    TooManyRequestsError,
)

logger = logging.getLogger(__name__)

REQUEST_JOB = "request_approval"
EXPIRE_JOB = "expire_approval"
CALLBACK_JOB = "approval_callback"
PREVIEW_CHARS = 500


class ApprovalIn(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="What needs a decision.")
    body: str = Field(default="", max_length=4000, description="Details the reviewer sees.")
    reviewers: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        min_length=1, max_length=10, description="External ids of the people who may decide."
    )
    importance: Importance = Importance.HIGH
    expires_in_seconds: int = Field(
        default=3600, ge=60, le=7 * 86_400, description="Undecided requests expire after this."
    )
    metadata: Annotated[dict[str, Any], AfterValidator(bounded_data)] = Field(
        default_factory=dict,
        description="Returned with the decision, e.g. the id of the paused flow.",
    )


async def create(
    session: AsyncSession, runtime: Runtime, body: ApprovalIn, key: ApiKey
) -> Approval:
    agent = await agents.for_key(session, key)
    if agent is not None:
        # Otherwise an agent could name anyone as a "reviewer" and reach people outside
        # its budget through the reviewers' notification rule.
        outside = sorted(set(body.reviewers) - set(agent.reviewers))
        if outside:
            raise ForbiddenError(
                f"agent {agent.key} may only ask its reviewers; not allowed: {', '.join(outside)}"
            )
        # Approval requests notify reviewers outside the agent's attention budget.
        limit = runtime.settings.approvals.max_pending_per_agent
        pending = await session.scalar(
            select(func.count())
            .select_from(Approval)
            .where(Approval.agent_key == agent.key, Approval.status == ApprovalStatus.PENDING)
        )
        if (pending or 0) >= limit:
            raise TooManyRequestsError(
                f"agent {agent.key} already has {limit} pending approval requests"
            )
    importance = body.importance
    if agent is not None and importance.rank > agent.max_importance.rank:
        importance = agent.max_importance
    approval = Approval(
        title=body.title,
        body=body.body,
        reviewers=list(dict.fromkeys(body.reviewers)),
        importance=importance,
        agent_key=agent.key if agent else None,
        requested_by=key.name,
        meta=body.metadata,
        status=ApprovalStatus.PENDING,
        expires_at=utcnow() + timedelta(seconds=body.expires_in_seconds),
    )
    session.add(approval)
    await session.flush()
    payload = {"approval_id": str(approval.id)}
    attempts = runtime.settings.worker.max_attempts
    await queue.enqueue(
        session, REQUEST_JOB, payload, key=f"{REQUEST_JOB}:{approval.id}", max_attempts=attempts
    )
    await queue.enqueue(
        session,
        EXPIRE_JOB,
        payload,
        run_at=approval.expires_at,
        key=f"{EXPIRE_JOB}:{approval.id}",
        max_attempts=attempts,
    )
    return approval


async def get(session: AsyncSession, approval_id: UUID, key: ApiKey) -> Approval:
    """An approval request; agent keys only see their own."""
    approval = await session.get(Approval, approval_id)
    if approval is None or (key.agent_key is not None and approval.agent_key != key.agent_key):
        raise NotFoundError("approval", approval_id)
    return as_read(session, approval)


def lapsed(approval: Approval) -> bool:
    return approval.status is ApprovalStatus.PENDING and approval.expires_at <= utcnow()


def with_status(stmt: Any, status: ApprovalStatus) -> Any:
    now = utcnow()
    if status is ApprovalStatus.PENDING:
        return stmt.where(Approval.status == status, Approval.expires_at > now)
    if status is ApprovalStatus.EXPIRED:
        return stmt.where(
            or_(
                Approval.status == status,
                and_(Approval.status == ApprovalStatus.PENDING, Approval.expires_at <= now),
            )
        )
    return stmt.where(Approval.status == status)


def as_read(session: AsyncSession, approval: Approval) -> Approval:
    if lapsed(approval):
        session.expunge(approval)
        approval.status = ApprovalStatus.EXPIRED
    return approval


def visible_to(key: ApiKey) -> Any:
    stmt = select(Approval)
    if key.agent_key is not None:
        stmt = stmt.where(Approval.agent_key == key.agent_key)
    return stmt


async def _decide(
    session: AsyncSession,
    runtime: Runtime,
    approval: Approval,
    *,
    status: ApprovalStatus,
    by: str | None,
    comment: str | None,
) -> None:
    now = utcnow()
    approval.status = status
    approval.decided_by = by[:300] if by else None
    approval.comment = comment
    approval.decided_at = now
    await review_links.close(session, now, approval_id=approval.id)
    await queue.enqueue(
        session,
        CALLBACK_JOB,
        {"approval_id": str(approval.id)},
        key=f"{CALLBACK_JOB}:{approval.id}",
        max_attempts=runtime.settings.worker.max_attempts,
    )


def _check_pending(approval: Approval) -> None:
    if approval.status is not ApprovalStatus.PENDING:
        raise InvalidStateError(f"approval is {approval.status}, not pending")
    if approval.expires_at <= utcnow():
        raise InvalidStateError("approval has expired")


async def decide(
    session: AsyncSession,
    runtime: Runtime,
    approval_id: UUID,
    key: ApiKey,
    *,
    approve: bool,
    comment: str | None,
) -> Approval:
    """A decision through the API. People decide; agent keys never can."""
    agents.check_not_agent(key, "decide approval requests; a person must")
    if not api_keys.allows(key, Scope.APPROVALS_DECIDE):
        raise ForbiddenError("deciding approval requests requires the approvals:decide scope")
    approval = await session.get(Approval, approval_id, with_for_update=True)
    if approval is None:
        raise NotFoundError("approval", approval_id)
    _check_pending(approval)
    status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    await _decide(session, runtime, approval, status=status, by=key.name, comment=comment)
    return approval


async def by_review_link(session: AsyncSession, link: ReviewLink, *, lock: bool = True) -> Approval:
    approval = await session.get(Approval, link.approval_id, with_for_update=lock)
    if approval is None:
        raise NotFoundError("approval", link.approval_id)
    who = f" by {approval.decided_by}" if approval.decided_by else ""
    already = f"This request was already decided{who}; it is now {approval.status}."
    if approval.status is not ApprovalStatus.PENDING:
        raise GoneError(already)
    if lapsed(approval):
        raise GoneError("This request has expired.")
    review_links.check_open(link, utcnow(), already)
    return approval


async def decide_by_link(
    session: AsyncSession,
    runtime: Runtime,
    link: ReviewLink,
    *,
    approve: bool,
    comment: str | None,
) -> Approval:
    approval = await by_review_link(session, link)
    status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    await _decide(
        session, runtime, approval, status=status, by=f"link:{link.reviewer}", comment=comment
    )
    return approval


# --------------------------------------------------------------------------- jobs


async def request(ctx: queue.JobContext) -> None:
    """Job: report a ``cue.approval_requested`` event, with a one-time link, per reviewer."""
    runtime = ctx.runtime
    async with runtime.transaction() as session:
        approval = await session.get(Approval, UUID(ctx.payload["approval_id"]))
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            return
        agent = await session.get(Agent, approval.agent_key) if approval.agent_key else None
        data = {
            "kind": "action",
            "approval_id": str(approval.id),
            "agent": approval.agent_key,
            "agent_name": agent.name if agent else approval.requested_by,
            "title": approval.title,
            "body": approval.body[:PREVIEW_CHARS],
            "importance": approval.importance.value,
            "expires_at": approval.expires_at.isoformat(),
        }
        for reviewer in approval.reviewers:
            token = await review_links.create(
                session, reviewer, approval.expires_at, approval_id=approval.id
            )
            await events.ingest(
                session,
                runtime,
                events.EventIn(
                    name=agents.APPROVAL_EVENT,
                    recipient=reviewer,
                    data={**data, "review_url": review_links.url(runtime.settings, token)},
                    idempotency_key=f"{approval.id}:{reviewer}",
                ),
                source=events.SYSTEM_SOURCE,
            )


async def expire(ctx: queue.JobContext) -> None:
    """Job: an undecided request becomes ``expired`` at ``expires_at``."""
    runtime = ctx.runtime
    async with runtime.transaction() as session:
        approval = await session.get(
            Approval, UUID(ctx.payload["approval_id"]), with_for_update=True
        )
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            return
        if approval.expires_at > utcnow():
            raise queue.RetryLater(
                (approval.expires_at - utcnow()).total_seconds(), "waiting for expiry", planned=True
            )
        await _decide(
            session, runtime, approval, status=ApprovalStatus.EXPIRED, by=None, comment=None
        )


def callback_payload(approval: Approval) -> dict[str, Any]:
    return {
        "type": "approval.decided",
        "id": str(approval.id),
        "status": approval.status.value,
        "title": approval.title,
        "agent": approval.agent_key,
        "decided_by": approval.decided_by,
        "comment": approval.comment,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "metadata": approval.meta,
    }


async def callback(ctx: queue.JobContext) -> None:
    """Job: tell ``approvals.callback_url`` about a decision, signed like Cue's webhooks."""
    runtime = ctx.runtime
    settings = runtime.settings.approvals
    if settings.callback_url is None or settings.callback_secret is None:
        return
    async with runtime.session() as session:
        approval = await session.get(Approval, UUID(ctx.payload["approval_id"]))
    if approval is None:
        return
    body = json.dumps(callback_payload(approval), separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": f"approval:{approval.id}",
        SIGNATURE_HEADER: sign(body, settings.callback_secret.get_secret_value(), int(time.time())),
    }
    try:
        response = await runtime.http.post(
            str(settings.callback_url),
            content=body,
            headers=headers,
            timeout=settings.timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"approval callback failed: {exc}") from exc
    if response.status_code >= 500 or response.status_code in (408, 429):
        raise RuntimeError(f"approval callback returned {response.status_code}")
    if response.status_code >= 400:
        raise queue.PermanentJobError(f"approval callback returned {response.status_code}")


async def expire_dead(ctx: queue.JobContext, error: str) -> None:
    async with ctx.runtime.transaction() as session:
        approval = await session.get(
            Approval, UUID(ctx.payload["approval_id"]), with_for_update=True
        )
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            return
        await _decide(
            session, ctx.runtime, approval, status=ApprovalStatus.EXPIRED, by=None, comment=None
        )
    logger.error(
        "approval marked expired because its expiry job gave up: %s",
        error,
        extra={"approval_id": ctx.payload["approval_id"]},
    )


async def request_dead(ctx: queue.JobContext, error: str) -> None:
    logger.error(
        "reviewers were not told about an approval request: %s",
        error,
        extra={"approval_id": ctx.payload["approval_id"]},
    )


async def callback_dead(ctx: queue.JobContext, error: str) -> None:
    logger.error(
        "the approval callback was not delivered: %s",
        error,
        extra={"approval_id": ctx.payload["approval_id"]},
    )


async def review_dead(ctx: queue.JobContext, error: str) -> None:
    logger.error(
        "reviewers were not told about a held message: %s",
        error,
        extra={"message_id": ctx.payload["message_id"]},
    )
