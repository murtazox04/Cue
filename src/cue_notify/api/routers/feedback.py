"""Delivery feedback from providers: deliveries, bounces and spam complaints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from cue_notify.api.deps import RecipientsWriter, RuntimeDep, SessionDep
from cue_notify.services import agents, feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackOut(BaseModel):
    message_id: UUID | None
    recipients: list[str]
    disabled: int
    unsubscribed: bool


@router.post("", summary="Report a delivery, bounce or complaint")
async def report_feedback(
    body: feedback.FeedbackIn, key: RecipientsWriter, session: SessionDep, runtime: RuntimeDep
) -> FeedbackOut:
    """Forward what your provider tells you after sending. A `bounce` disables the
    address; `soft_bounce`s in a row (`delivery.soft_bounce_limit`) do too; a
    `complaint` unsubscribes the person from every optional category; `delivered` marks
    the message delivered. Name the message (`message_id`, or the `provider_message_id`
    with its `channel`), the address, or both."""
    agents.check_not_agent(key, "report delivery feedback")
    result = await feedback.apply(session, runtime, body, {"source": "api", "api_key": key.name})
    return FeedbackOut(
        message_id=result.message_id,
        recipients=result.recipients,
        disabled=result.disabled,
        unsubscribed=result.unsubscribed,
    )
