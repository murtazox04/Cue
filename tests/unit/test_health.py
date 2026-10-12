from __future__ import annotations

import pytest

from cue_notify.channels.twilio import delivery_feedback
from cue_notify.core.enums import FeedbackType
from cue_notify.services.health import ChannelHealth


def test_a_channel_opens_after_failures_in_a_row_and_closes_on_success() -> None:
    health = ChannelHealth(failures=3, cooldown=60)
    for _ in range(2):
        health.failed("push", now=0)
    assert health.available("push", now=0)
    health.succeeded("push")  # a success resets the count
    for _ in range(3):
        health.failed("push", now=0)
    assert not health.available("push", now=10)
    assert health.available("push", now=61)  # cooldown over: try again
    health.succeeded("push")
    assert health.snapshot() == {}


def test_zero_failures_turns_the_breaker_off() -> None:
    health = ChannelHealth(failures=0, cooldown=60)
    for _ in range(100):
        health.failed("push", now=0)
    assert health.available("push", now=0)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"MessageStatus": "delivered"}, FeedbackType.DELIVERED),
        ({"MessageStatus": "undelivered", "ErrorCode": "30005"}, FeedbackType.BOUNCE),
        ({"MessageStatus": "failed", "ErrorCode": "21610"}, FeedbackType.BOUNCE),
        ({"MessageStatus": "undelivered", "ErrorCode": "30003"}, FeedbackType.SOFT_BOUNCE),
        ({"MessageStatus": "failed", "ErrorCode": ""}, FeedbackType.SOFT_BOUNCE),
        ({"MessageStatus": "sent"}, None),
    ],
)
def test_twilio_statuses_map_to_feedback(
    params: dict[str, str], expected: FeedbackType | None
) -> None:
    report = delivery_feedback(params)
    assert (report[0] if report else None) == expected
