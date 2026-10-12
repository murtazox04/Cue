"""Resend webhooks and Amazon SNS notifications for SES, end to end."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from cue_notify.api.app import create_app
from cue_notify.api.routers import inbound
from cue_notify.channels.resend import ResendChannel, ResendConfig
from cue_notify.channels.ses import SesChannel, SesConfig
from cue_notify.runtime import Runtime
from cue_notify.settings import Settings
from tests.helpers import FakeChannel, create_recipient, create_template, drain
from tests.unit.test_connectors import sns_signer

SECRET = "whsec_" + base64.b64encode(b"resend-test-secret").decode()
TOPIC = "arn:aws:sns:eu-west-1:123456789012:ses-feedback"
CERT_URL = "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-test.pem"
SUBSCRIBE_URL = "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"


@pytest.fixture
def provider_calls() -> list[httpx.Request]:
    return []


@pytest.fixture
def signer() -> tuple[bytes, Any]:
    inbound._SNS_CERTIFICATES.clear()  # pyright: ignore[reportPrivateUsage]
    return sns_signer()


@pytest.fixture
async def providers(
    settings: Settings,
    push: FakeChannel,
    provider_calls: list[httpx.Request],
    signer: tuple[bytes, Any],
) -> AsyncIterator[Runtime]:
    pem, _ = signer

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        if request.url.host == "api.resend.com":
            return httpx.Response(200, json={"id": "em-1"})
        if request.url.host.startswith("email."):
            return httpx.Response(200, json={"MessageId": "0100-1"})
        if str(request.url) == CERT_URL:
            return httpx.Response(200, content=pem)
        return httpx.Response(200, text="<ConfirmSubscriptionResponse/>")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    channels = {
        "push": push,
        "email": ResendChannel(
            "email",
            ResendConfig(api_key="re_1", from_address="hi@shop.test", webhook_secret=SECRET),
            http,
        ),
        "ses": SesChannel(
            "ses",
            SesConfig(
                region="eu-west-1",
                from_address="hi@shop.test",
                access_key_id="AKID",
                secret_access_key="secret",
                sns_topic_arns=[TOPIC],
            ),
            http,
        ),
    }
    rt = Runtime.create(settings, channels=channels, http=http)
    try:
        yield rt
    finally:
        await rt.aclose()


@pytest.fixture
async def server(providers: Runtime, admin_key: str) -> AsyncIterator[httpx.AsyncClient]:
    """The API of the runtime with the Resend and SES channels."""
    app = create_app(runtime=providers)
    app.state.runtime = providers
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cue.test",
        headers={"Authorization": f"Bearer {admin_key}"},
    ) as http:
        yield http


async def _send(server: httpx.AsyncClient, providers: Runtime, channel: str) -> dict[str, Any]:
    client = server
    await create_template(client)
    await create_recipient(
        client,
        addresses=[
            {"channel": "email", "value": "ann@example.test"},
            {"channel": "ses", "value": "ann@example.test"},
        ],
    )
    sent = await client.post(
        "/v1/messages",
        json={
            "recipient": "user-1",
            "template": "order-shipped",
            "channels": [channel],
            "data": {"order_id": "A-1"},
        },
    )
    assert sent.status_code == 202, sent.text
    await drain(providers)
    return (await client.get(f"/v1/messages/{sent.json()['id']}")).json()


def _resend_headers(body: bytes, *, secret: str = SECRET) -> dict[str, str]:
    msg_id, timestamp = "msg_1", str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{base64.b64encode(digest).decode()}",
        "content-type": "application/json",
    }


async def test_resend_bounces_disable_the_address(
    client: httpx.AsyncClient, providers: Runtime, server: httpx.AsyncClient
) -> None:
    message = await _send(server, providers, "email")
    assert (message["status"], message["provider_message_id"]) == ("sent", "em-1")
    event = {
        "type": "email.bounced",
        "data": {
            "email_id": "em-1",
            "to": ["ann@example.test"],
            "bounce": {"type": "Permanent", "message": "mailbox does not exist"},
        },
    }
    body = json.dumps(event).encode()
    forged = await server.post(
        "/v1/inbound/resend/email",
        content=body,
        headers=_resend_headers(body, secret="whsec_" + base64.b64encode(b"other").decode()),
    )
    assert forged.status_code == 403
    wrong_channel = await server.post(
        "/v1/inbound/resend/ses", content=body, headers=_resend_headers(body)
    )
    assert wrong_channel.status_code == 404
    ok = await server.post("/v1/inbound/resend/email", content=body, headers=_resend_headers(body))
    assert ok.status_code == 204

    bounced = (await client.get(f"/v1/messages/{message['id']}")).json()
    assert (bounced["status"], bounced["status_reason"]) == (
        "failed",
        "bounced: mailbox does not exist",
    )
    addresses = (await client.get("/v1/recipients/user-1")).json()["addresses"]
    by_channel = {a["channel"]: a["enabled"] for a in addresses}
    assert by_channel == {"email": False, "ses": True}  # only the channel that bounced


async def test_ses_notifications_through_sns(
    client: httpx.AsyncClient,
    providers: Runtime,
    server: httpx.AsyncClient,
    signer: tuple[bytes, Any],
    provider_calls: list[httpx.Request],
) -> None:
    _, sign = signer
    message = await _send(server, providers, "ses")
    assert message["provider_message_id"] == "0100-1"

    def sns(kind: str, **fields: Any) -> dict[str, Any]:
        return sign(
            {
                "Type": kind,
                "MessageId": "sns-1",
                "TopicArn": TOPIC,
                "Timestamp": "2026-10-12T00:00:00.000Z",
                "SigningCertURL": CERT_URL,
                **fields,
            }
        )

    confirm = sns(
        "SubscriptionConfirmation", Message="confirm", Token="t", SubscribeURL=SUBSCRIBE_URL
    )
    assert (await server.post("/v1/inbound/ses/ses", json=confirm)).status_code == 204
    assert str(provider_calls[-1].url) == SUBSCRIBE_URL

    complaint = {
        "notificationType": "Complaint",
        "mail": {"messageId": "0100-1"},
        "complaint": {"complainedRecipients": [{"emailAddress": "ann@example.test"}]},
    }
    notification = sns("Notification", Message=json.dumps(complaint))
    assert (await server.post("/v1/inbound/ses/ses", json=notification)).status_code == 204
    recipient = (await client.get("/v1/recipients/user-1")).json()
    assert "marketing" in recipient["preferences"]["unsubscribed_categories"]

    # Someone else's topic, a tampered message and a foreign certificate are refused.
    other_topic = sign({**notification, "TopicArn": "arn:aws:sns:eu-west-1:666:evil"})
    tampered = {**notification, "Message": json.dumps({**complaint, "x": 1})}
    foreign = sign({**notification, "SigningCertURL": "https://evil.test/cert.pem"})
    for body in (other_topic, tampered, foreign):
        assert (await server.post("/v1/inbound/ses/ses", json=body)).status_code == 403
