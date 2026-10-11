"""Tool approval for Pydantic AI agents, collected by Cue.

Tools registered with ``requires_approval=True`` end a run with ``DeferredToolRequests``.
Ask the reviewers through Cue (their own channels, a one-time link to decide from the
notification), keep the run's messages, and continue when the decisions arrive::

    agent = Agent(model, output_type=[str, DeferredToolRequests])

    @agent.tool_plain(requires_approval=True)
    def refund(order_id: str) -> str: ...

    result = agent.run_sync("Refund order 9")
    if isinstance(result.output, DeferredToolRequests):
        save(conversation_id, result.all_messages())
        for request in approval_requests(result.output, conversation_id, ["lead"]):
            cue.request_approval(**request)

    # Once every pending call is decided (approvals.callback_url or get_approval):
    agent.run_sync(message_history=load(conversation_id),
                   deferred_tool_results=deferred_results(decisions))

A rejected call reaches the model as a denial with the reviewer's comment, so it can
explain or try something else. pydantic-ai is imported only by the result helpers.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


def approval_requests(
    requests: Any,
    conversation_id: str,
    reviewers: list[str],
    *,
    expires_in_seconds: int = 3600,
    max_body_chars: int = 4000,
) -> list[dict[str, Any]]:
    """Keyword arguments for ``Cue.request_approval``, one per call awaiting approval."""
    asks: list[dict[str, Any]] = []
    for call in requests.approvals:
        args = call.args if isinstance(call.args, str) else json.dumps(call.args or {}, indent=2)
        asks.append(
            {
                "title": f"Allow the agent to call {call.tool_name}?"[:200],
                "body": f"Arguments:\n{args}"[:max_body_chars],
                "reviewers": reviewers,
                "expires_in_seconds": expires_in_seconds,
                "metadata": {
                    "conversation_id": conversation_id,
                    "tool_call_id": call.tool_call_id,
                    "tool_name": call.tool_name,
                },
            }
        )
    return asks


def approval_result(decision: Mapping[str, Any]) -> Any:
    """``True`` for an approved call, else a ``ToolDenied`` carrying the reason."""
    if decision["status"] == "approved":
        return True
    denied: Any = importlib.import_module("pydantic_ai").ToolDenied
    reason = decision.get("comment") or (
        "Nobody approved this in time." if decision["status"] == "expired" else "Rejected."
    )
    return denied(f"A person rejected this tool call: {reason}")


def deferred_results(decisions: Iterable[Mapping[str, Any]]) -> Any:
    """``DeferredToolResults`` for the next run, from Cue's decisions."""
    results: Any = importlib.import_module("pydantic_ai").DeferredToolResults
    return results(approvals={d["metadata"]["tool_call_id"]: approval_result(d) for d in decisions})
