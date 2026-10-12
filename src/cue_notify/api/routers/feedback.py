from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from cue_notify.api.deps import RecipientsWriter, RuntimeDep, SessionDep
from cue_notify.core.enums import Scope
from cue_notify.services import agents, api_keys, feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackOut(BaseModel):
    message_id: UUID | None
    recipients: list[str] | None = Field(
        description="External ids of the recipients it concerned; null unless the key holds "
        "recipients:read."
    )
    recipient_count: int
    disabled: int = Field(description="Addresses this report disabled.")
    unsubscribed: bool = Field(
        description="A complaint stopped optional messages to an address that still got them."
    )
    duplicate: bool = Field(description="A report with this report_id was applied before.")


@router.post(
    "",
    summary="Report a delivery, bounce or complaint",
    description=(
        "Forward what your provider tells you after sending. A `bounce` disables the "
        "address; `soft_bounce`s in a row (`delivery.soft_bounce_limit`) do too; a "
        "`complaint` stops every optional message to the address, in present and future "
        "categories; `delivered` marks the message delivered. Name the message "
        "(`message_id`, or the `provider_message_id` with its `channel`), the address, or both."
    ),
)
async def report_feedback(
    body: feedback.FeedbackIn, key: RecipientsWriter, session: SessionDep, runtime: RuntimeDep
) -> FeedbackOut:
    agents.check_not_agent(key, "report delivery feedback")
    result = await feedback.apply(session, runtime, body, {"source": "api", "api_key": key.name})
    readable = api_keys.allows(key, Scope.RECIPIENTS_READ)
    return FeedbackOut(
        message_id=result.message_id,
        recipients=result.recipients if readable else None,
        recipient_count=len(result.recipients),
        disabled=result.disabled,
        unsubscribed=result.unsubscribed,
        duplicate=result.duplicate,
    )
