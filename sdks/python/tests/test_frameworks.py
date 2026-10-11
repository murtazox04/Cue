"""LangGraph and Pydantic AI bridges, with stand-ins for the frameworks."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from cue_client.langgraph import approval_request, resume, resume_value
from cue_client.pydantic_ai import approval_requests, deferred_results


@dataclass
class Interrupt:
    value: Any
    id: str = "int-1"


@dataclass
class Call:
    tool_name: str
    args: Any
    tool_call_id: str


@dataclass
class Requests:
    approvals: list[Call] = field(default_factory=list)


@dataclass
class Command:
    resume: Any


@dataclass
class Denied:
    message: str


@dataclass
class Results:
    approvals: dict[str, Any]


@pytest.fixture
def frameworks(monkeypatch: pytest.MonkeyPatch) -> None:
    lg = types.ModuleType("langgraph.types")
    lg.Command = Command  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(sys.modules, "langgraph.types", lg)
    pai = types.ModuleType("pydantic_ai")
    pai.ToolDenied = Denied  # type: ignore[attr-defined]
    pai.DeferredToolResults = Results  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pydantic_ai", pai)


def test_a_langgraph_interrupt_becomes_an_approval_and_resumes(frameworks: None) -> None:
    ask = approval_request(Interrupt({"title": "Delete it?", "body": "For good."}), "t-1", ["a"])
    assert (ask["title"], ask["body"]) == ("Delete it?", "For good.")
    assert ask["metadata"] == {"thread_id": "t-1", "interrupt_id": "int-1"}
    assert approval_request(Interrupt("Ship it?"), "t-1", ["a"])["title"] == "Ship it?"
    other = approval_request(Interrupt({"question": "Pay?", "amount": 40}), "t", ["a"])
    assert (other["title"], '"amount": 40' in other["body"]) == ("Pay?", True)

    decision = {
        "status": "rejected",
        "comment": "no",
        "decided_by": "link:a",
        "metadata": ask["metadata"],
    }
    thread_id, command = resume(decision)
    assert thread_id == "t-1"
    assert command.resume == {"int-1": resume_value(decision)}
    assert resume_value(decision)["approved"] is False


def test_pydantic_ai_calls_become_approvals_and_results(frameworks: None) -> None:
    requests = Requests([Call("refund", {"order_id": "9"}, "call-1")])
    [ask] = approval_requests(requests, "conv-1", ["lead"])
    assert ask["title"] == "Allow the agent to call refund?"
    assert '"order_id": "9"' in ask["body"]
    assert ask["metadata"] == {
        "conversation_id": "conv-1",
        "tool_call_id": "call-1",
        "tool_name": "refund",
    }
    meta = ask["metadata"]
    results = deferred_results(
        [
            {"status": "approved", "metadata": meta},
            {"status": "expired", "metadata": {**meta, "tool_call_id": "call-2"}},
        ]
    )
    assert results.approvals["call-1"] is True
    assert "Nobody approved this in time." in results.approvals["call-2"].message
