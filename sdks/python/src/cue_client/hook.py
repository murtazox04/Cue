"""``cue-hook``: tell people what their coding agents are doing, through Cue.

OpenHands (``.openhands/hooks.json``) and Claude Code (``.claude/settings.json``) run hook
commands with the event as JSON on stdin. ``cue-hook`` reports it to Cue as an event,
``agent.stop``, ``agent.notification`` (Claude Code: waiting for input),
``agent.session_end`` and so on, and your rules decide whether and how to tell the
person. Use a digest to merge pings from many agents running in parallel, quiet hours to
keep the night quiet, and importance for the ones that matter::

    {"stop": [{"matcher": "*", "hooks": [{"command": "cue-hook", "async": true}]}]}

Configuration comes from the environment: ``CUE_URL``, ``CUE_API_KEY`` and
``CUE_RECIPIENT`` (whose agent this is), plus optional ``CUE_AGENT`` (a display name).
The hook never blocks the agent: it always exits 0 and reports failures on stderr.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

import httpx

from cue_client.client import Cue

FIELDS = ("session_id", "cwd", "message", "stop_reason", "reason", "tool_name")


def event_from_hook(payload: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """The Cue event for one hook invocation."""
    hook = str(payload.get("hook_event_name") or payload.get("event") or "hook")
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", hook).lower()
    data = {key: payload[key] for key in FIELDS if payload.get(key) is not None}
    data["hook"] = hook
    openhands = "OpenHands" if env.get("OPENHANDS_SESSION_ID") else None
    data["agent"] = env.get("CUE_AGENT") or openhands
    data["project"] = (
        env.get("OPENHANDS_PROJECT_DIR") or env.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
    )
    return {
        "name": f"{env.get('CUE_EVENT_PREFIX', 'agent')}.{name}",
        "recipient": env["CUE_RECIPIENT"],
        "data": {key: value for key, value in data.items() if value},
    }


def report(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    event = event_from_hook(payload, env)
    with Cue(
        env["CUE_API_KEY"],
        env.get("CUE_URL", "http://127.0.0.1:8000"),
        timeout=5.0,
        retries=0,
        transport=transport,
    ) as cue:
        return cue.send_event(event["name"], event["recipient"], event["data"])


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload: Any = json.loads(raw) if raw.strip() else {}
        hook: dict[str, Any] = payload if isinstance(payload, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        report(hook, os.environ)
    except Exception as exc:  # a notification must never stop the agent
        print(f"cue-hook: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
