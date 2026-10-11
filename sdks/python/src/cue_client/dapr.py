"""Human approval for Dapr Agents, collected by Cue.

A ``DurableAgent`` hook that returns ``RequireApproval`` publishes an
``ApprovalRequiredEvent`` to your approval topic and waits for an
``ApprovalResponseEvent``. A small service subscribed to that topic hands the request to
Cue, which tells the approvers on their own channels with a one-time link; when they
decide, Cue calls your ``approvals.callback_url`` and the service publishes the answer::

    @app.post("/approval-requests")            # Dapr subscription to the request topic
    async def on_request(cloud_event: dict):
        await cue.request_approval(**approval_request(cloud_event["data"], ["oncall"]))

    @app.post("/cue/decisions")                # Cue's approvals.callback_url
    async def on_decision(request: Request):
        body = await request.body()
        verify_webhook(body, request.headers.get("Cue-Signature"), CALLBACK_SECRET)
        await publish("agent-approval-responses", approval_response(json.loads(body)))

Neither function imports Dapr; they translate the documented event shapes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def approval_request(
    event: Mapping[str, Any], reviewers: list[str], *, max_body_chars: int = 4000
) -> dict[str, Any]:
    """Keyword arguments for ``Cue.request_approval`` from an ``ApprovalRequiredEvent``."""
    step = str(event.get("step_name") or "a step")
    arguments = json.dumps(event.get("tool_arguments") or {}, indent=2, default=str)
    instructions = event.get("instructions")
    body = f"{instructions}\n\nArguments:\n{arguments}" if instructions else arguments
    return {
        "title": f"Allow the agent to run {step}?"[:200],
        "body": body[:max_body_chars],
        "reviewers": reviewers,
        # Dapr denies on its own timeout; expire no later, so nobody approves too late.
        "expires_in_seconds": max(60, int(event.get("timeout_seconds") or 3600)),
        "metadata": {
            "approval_request_id": event["approval_request_id"],
            "instance_id": event.get("instance_id"),
            "step_name": step,
        },
    }


def approval_response(decision: Mapping[str, Any]) -> dict[str, Any]:
    """An ``ApprovalResponseEvent`` payload from Cue's ``approval.decided`` callback."""
    status = str(decision["status"])
    reason = decision.get("comment") or (
        "nobody decided before the deadline" if status == "expired" else None
    )
    response: dict[str, Any] = {
        "approval_request_id": decision["metadata"]["approval_request_id"],
        "approved": status == "approved",
        "reason": reason,
    }
    if decision.get("decided_at"):
        response["decided_at"] = decision["decided_at"]
    return response
