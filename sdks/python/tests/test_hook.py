"""cue-hook: coding-agent hook events become Cue events."""

from __future__ import annotations

import json
from typing import Any

import httpx

from cue_client.hook import event_from_hook, report

ENV = {"CUE_URL": "http://cue.test", "CUE_API_KEY": "ck_test", "CUE_RECIPIENT": "dev-1"}


def test_openhands_stop_becomes_an_agent_event() -> None:
    payload = {"hook_event_name": "Stop", "session_id": "s-1", "stop_reason": "finished"}
    env = {**ENV, "OPENHANDS_SESSION_ID": "s-1", "OPENHANDS_PROJECT_DIR": "/repo"}
    assert event_from_hook(payload, env) == {
        "name": "agent.stop",
        "recipient": "dev-1",
        "data": {
            "session_id": "s-1",
            "stop_reason": "finished",
            "hook": "Stop",
            "agent": "OpenHands",
            "project": "/repo",
        },
    }


def test_claude_code_waiting_for_input_is_reported() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(202, json={"id": "e1", "status": "pending", "duplicate": False})

    payload = {
        "hook_event_name": "Notification",
        "session_id": "s-2",
        "message": "Claude is waiting for your input",
        "cwd": "/work/app",
    }
    report(payload, {**ENV, "CUE_AGENT": "Claude Code"}, transport=httpx.MockTransport(handler))
    assert seen[0]["name"] == "agent.notification"
    assert seen[0]["data"]["message"] == "Claude is waiting for your input"
    assert seen[0]["data"]["agent"] == "Claude Code"
    assert seen[0]["data"]["project"] == "/work/app"
