from __future__ import annotations

import smtplib

import httpx
import pytest

from cue_notify.channels.base import Delivery, DeliveryError, InvalidAddressError
from cue_notify.channels.smtp import SmtpChannel, SmtpConfig
from cue_notify.channels.twilio import delivery_feedback
from cue_notify.core.content import Content
from cue_notify.core.enums import FeedbackType
from cue_notify.core.ids import uuid7
from cue_notify.services.health import ChannelHealth


def test_a_channel_opens_after_failures_in_a_row_and_closes_on_success() -> None:
    health = ChannelHealth(failures=3, cooldown=60)
    for _ in range(2):
        health.failed("push", now=0)
    assert health.available("push", now=0)
    health.succeeded("push")
    for _ in range(3):
        health.failed("push", now=0)
    assert not health.available("push", now=10)
    assert health.available("push", now=61)
    health.succeeded("push")
    assert health.states == {}


def test_after_the_cooldown_one_message_probes_the_channel() -> None:
    health = ChannelHealth(failures=1, cooldown=60)
    health.failed("push", now=0)
    assert health.available("push", now=61)
    assert not health.available("push", now=62)
    health.failed("push", now=63)
    assert not health.available("push", now=100)
    assert health.available("push", now=124)
    assert not health.available("push", now=125)
    assert health.available("push", now=185)
    health.succeeded("push")
    assert health.available("push", now=186)
    assert health.available("push", now=186)


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


@pytest.mark.parametrize(
    ("error", "retryable", "deferred"),
    [
        (smtplib.SMTPRecipientsRefused({"a@x.test": (450, b"greylisted")}), True, True),
        (smtplib.SMTPRecipientsRefused({"a@x.test": (452, b"mailbox full")}), True, True),
        (smtplib.SMTPRecipientsRefused({"a@x.test": (421, b"closing")}), True, False),
        (smtplib.SMTPDataError(451, b"try again later"), True, True),
        (smtplib.SMTPDataError(421, b"closing"), True, False),
        (smtplib.SMTPSenderRefused(451, b"slow down", "noreply@x.test"), True, False),
        (smtplib.SMTPDataError(554, b"rejected"), False, False),
    ],
)
async def test_smtp_temporary_replies_are_retried(
    monkeypatch: pytest.MonkeyPatch, error: Exception, retryable: bool, deferred: bool
) -> None:
    async with httpx.AsyncClient() as http:
        channel = SmtpChannel("email", SmtpConfig(host="smtp.test", from_address="n@x.test"), http)

        def refuse(email: object) -> None:
            raise error

        monkeypatch.setattr(channel, "_send_blocking", refuse)
        delivery = Delivery(uuid7(), "a@x.test", Content(title="Hi", body="Hello"))
        with pytest.raises(DeliveryError) as raised:
            await channel.send(delivery)
    assert not isinstance(raised.value, InvalidAddressError)
    assert (raised.value.retryable, raised.value.deferred) == (retryable, deferred)


async def test_smtp_permanent_recipient_refusal_is_a_dead_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with httpx.AsyncClient() as http:
        channel = SmtpChannel("email", SmtpConfig(host="smtp.test", from_address="n@x.test"), http)

        def refuse(email: object) -> None:
            raise smtplib.SMTPRecipientsRefused({"a@x.test": (550, b"no such user")})

        monkeypatch.setattr(channel, "_send_blocking", refuse)
        delivery = Delivery(uuid7(), "a@x.test", Content(title="Hi", body="Hello"))
        with pytest.raises(InvalidAddressError):
            await channel.send(delivery)
