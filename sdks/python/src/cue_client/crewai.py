"""Human feedback for CrewAI Flows, collected by Cue.

``@human_feedback`` blocks on console input by default. :class:`CueFeedbackProvider`
instead asks reviewers through Cue (their channels, quiet hours and a one-time link to
decide from the notification) and pauses the flow. When Cue reports the decision to your
``approvals.callback_url``, resume the flow with :func:`resume_args`::

    provider = CueFeedbackProvider(Cue(API_KEY, CUE_URL), reviewers=["lead"])

    class Publish(Flow):
        @start()
        @human_feedback(message="Publish this post?", emit=["approved", "rejected"],
                        llm="gpt-4o-mini", provider=provider)
        def draft(self): ...

    # In your webhook handler, after verify_webhook(...):
    flow_id, feedback = resume_args(json.loads(body))
    Publish.from_pending(flow_id).resume(feedback)

This module needs ``crewai`` (1.8 or later) only when a flow actually asks for feedback.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from cue_client.client import Cue, Importance

OUTCOMES = {
    "approved": "Approved.",
    "rejected": "Rejected.",
    "expired": "Rejected: nobody decided before the deadline.",
}


class CueFeedbackProvider:
    """A CrewAI ``HumanFeedbackProvider`` that asks ``reviewers`` through Cue."""

    def __init__(
        self,
        client: Cue,
        reviewers: list[str],
        *,
        importance: Importance = "high",
        expires_in_seconds: int = 86_400,
        max_output_chars: int = 4000,
    ) -> None:
        self.client = client
        self.reviewers = reviewers
        self.importance: Importance = importance
        self.expires_in_seconds = expires_in_seconds
        self.max_output_chars = max_output_chars

    def request_feedback(self, context: Any, flow: Any) -> str:
        """Ask the reviewers and pause the flow (CrewAI persists it) until they decide.

        ``context`` is CrewAI's ``PendingFeedbackContext``; crewai is imported only here,
        so the client works without it.
        """
        pending: Any = importlib.import_module("crewai.flow").HumanFeedbackPending
        approval = self.client.request_approval(
            title=context.message[:200],
            body=str(context.method_output)[: self.max_output_chars],
            reviewers=self.reviewers,
            importance=self.importance,
            expires_in_seconds=self.expires_in_seconds,
            metadata={
                "flow_id": context.flow_id,
                "flow_class": context.flow_class,
                "method": context.method_name,
            },
        )
        raise pending(context=context, callback_info={"cue_approval_id": approval["id"]})


def feedback_text(decision: Mapping[str, Any]) -> str:
    """The reviewer's decision as feedback text, e.g. ``"Rejected. Not this week."``."""
    text = OUTCOMES[str(decision["status"])]
    comment = decision.get("comment")
    return f"{text} {comment}" if comment else text


def resume_args(decision: Mapping[str, Any]) -> tuple[str, str]:
    """``(flow_id, feedback)`` from Cue's ``approval.decided`` callback or an approval."""
    return str(decision["metadata"]["flow_id"]), feedback_text(decision)
