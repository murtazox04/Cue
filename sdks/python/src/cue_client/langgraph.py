"""Human approval for LangGraph, collected by Cue.

A node that needs a person calls ``interrupt(...)``; the graph pauses on its checkpointer.
Hand the pending interrupt to Cue, which asks the reviewers on their own channels with a
one-time link, and resume the thread when Cue reports the decision::

    def delete_accounts(state):
        decision = interrupt({"title": "Delete 1,204 inactive accounts?",
                              "body": "No login since 2024."})
        if not decision["approved"]:
            return {"result": "kept"}
        ...

    result = graph.invoke(inputs, config)
    for pending in result.get("__interrupt__", []):
        cue.request_approval(**approval_request(pending, thread_id, ["lead"]))

    # approvals.callback_url handler, after verify_webhook(...):
    thread_id, command = resume(json.loads(body))
    graph.invoke(command, {"configurable": {"thread_id": thread_id}})

The node receives ``{"approved": bool, "status": ..., "comment": ..., "decided_by": ...}``.
langgraph is imported only by :func:`resume`.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from typing import Any


def approval_request(
    pending: Any,
    thread_id: str,
    reviewers: list[str],
    *,
    expires_in_seconds: int = 86_400,
    max_body_chars: int = 4000,
) -> dict[str, Any]:
    """Keyword arguments for ``Cue.request_approval`` from a LangGraph ``Interrupt``.

    The interrupt's value may be a string (the question) or a mapping with ``title``
    (or ``question``) and ``body``; anything else is shown as JSON.
    """
    value: Any = pending.value
    if isinstance(value, Mapping):
        fields: Mapping[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        title = str(fields.get("title") or fields.get("question") or "Approve this step?")
        body = fields.get("body")
        if body is None:
            rest = {k: v for k, v in fields.items() if k not in ("title", "question")}
            body = json.dumps(rest, indent=2, default=str) if rest else ""
    else:
        title, body = str(value), ""
    return {
        "title": title[:200],
        "body": str(body)[:max_body_chars],
        "reviewers": reviewers,
        "expires_in_seconds": expires_in_seconds,
        "metadata": {"thread_id": thread_id, "interrupt_id": pending.id},
    }


def resume_value(decision: Mapping[str, Any]) -> dict[str, Any]:
    """What the interrupted node receives from ``interrupt(...)``."""
    return {
        "approved": decision["status"] == "approved",
        "status": decision["status"],
        "comment": decision.get("comment"),
        "decided_by": decision.get("decided_by"),
    }


def resume(decision: Mapping[str, Any]) -> tuple[str, Any]:
    """``(thread_id, Command)`` to resume the graph with Cue's decision."""
    command: Any = importlib.import_module("langgraph.types").Command
    meta = decision["metadata"]
    resume_map = {meta["interrupt_id"]: resume_value(decision)}
    return str(meta["thread_id"]), command(resume=resume_map)
