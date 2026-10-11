"""Translating between Dapr Agents' approval events and Cue approval requests."""

from __future__ import annotations

from cue_client.dapr import approval_request, approval_response

EVENT = {
    "approval_request_id": "req-1",
    "instance_id": "wf-9",
    "step_name": "DeleteOldData",
    "tool_call_id": "call-1",
    "tool_arguments": {"dataset": "sales-2023"},
    "instructions": "Confirm deletion of dataset: sales-2023",
    "timeout_seconds": 300,
    "requested_at": "2026-10-11T08:00:00Z",
}


def test_a_dapr_request_becomes_a_cue_approval() -> None:
    request = approval_request(EVENT, ["oncall"])
    assert request["title"] == "Allow the agent to run DeleteOldData?"
    assert request["body"].startswith("Confirm deletion of dataset: sales-2023")
    assert '"dataset": "sales-2023"' in request["body"]
    assert request["expires_in_seconds"] == 300
    assert request["metadata"] == {
        "approval_request_id": "req-1",
        "instance_id": "wf-9",
        "step_name": "DeleteOldData",
    }


def test_cue_decisions_become_dapr_responses() -> None:
    meta = {"approval_request_id": "req-1"}
    approved = {"status": "approved", "comment": None, "metadata": meta, "decided_at": "t"}
    assert approval_response(approved) == {
        "approval_request_id": "req-1",
        "approved": True,
        "reason": None,
        "decided_at": "t",
    }
    expired = approval_response({"status": "expired", "metadata": meta, "decided_at": "t"})
    assert (expired["approved"], expired["reason"]) == (False, "nobody decided before the deadline")
