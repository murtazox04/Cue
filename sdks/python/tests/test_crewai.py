"""Approvals in the client, and the CrewAI provider (with a stand-in for crewai)."""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from cue_client import Cue
from cue_client.crewai import CueFeedbackProvider, feedback_text, resume_args


@dataclass
class Context:
    flow_id: str = "flow-1"
    flow_class: str = "app.flows.PublishFlow"
    method_name: str = "draft"
    method_output: Any = "Draft post"
    message: str = "Publish this post?"


class Pending(Exception):  # noqa: N818 — mirrors crewai's name
    def __init__(self, context: Any, callback_info: dict[str, Any] | None = None) -> None:
        super().__init__("pending")
        self.context = context
        self.callback_info = callback_info


@pytest.fixture
def crewai(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = types.ModuleType("crewai.flow")
    flow.HumanFeedbackPending = Pending  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "crewai", types.ModuleType("crewai"))
    monkeypatch.setitem(sys.modules, "crewai.flow", flow)


def test_the_provider_asks_through_cue_and_pauses_the_flow(crewai: None) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/approvals"
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "a-1", "status": "pending"})

    cue = Cue("ck_test", "http://cue.test", transport=httpx.MockTransport(handler))
    provider = CueFeedbackProvider(cue, ["lead"], expires_in_seconds=600)
    with pytest.raises(Pending) as paused:
        provider.request_feedback(Context(), flow=None)
    assert paused.value.callback_info == {"cue_approval_id": "a-1"}
    assert sent == [
        {
            "title": "Publish this post?",
            "body": "Draft post",
            "reviewers": ["lead"],
            "importance": "high",
            "expires_in_seconds": 600,
            "metadata": {
                "flow_id": "flow-1",
                "flow_class": "app.flows.PublishFlow",
                "method": "draft",
            },
        }
    ]


def test_decisions_become_feedback_for_resume() -> None:
    decision = {"status": "rejected", "comment": "Wait for the docs", "metadata": {"flow_id": "f"}}
    assert resume_args(decision) == ("f", "Rejected. Wait for the docs")
    assert feedback_text({"status": "approved", "comment": None}) == "Approved."
    assert feedback_text({"status": "expired"}).startswith("Rejected")


def test_requesting_an_approval_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cue_client.client.time.sleep", lambda _: None)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(503)

    cue = Cue("ck_test", "http://cue.test", transport=httpx.MockTransport(handler))
    with pytest.raises(Exception, match="503"):
        cue.request_approval("Refund?", ["lead"])
    assert calls == ["POST"]
