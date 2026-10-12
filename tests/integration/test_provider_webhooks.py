from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
BROKEN_CERT_URL = "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-down.pem"
SUBSCRIBE_URL = "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"
REFUSED_SUBSCRIBE_URL = "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=x"


@pytest.fixture
def provider_calls() -> list[httpx.Request]:
    return []


@pytest.fixture
def signer() -> tuple[bytes, Any]:
    inbound.SNS_CERTIFICATES.clear()
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
        if str(request.url) == BROKEN_CERT_URL:
            raise httpx.ConnectError("unreachable")
        if str(request.url) == REFUSED_SUBSCRIBE_URL:
            return httpx.Response(500)
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
    app = create_app(runtime=providers)
    app.state.runtime = providers
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cue.test",
        headers={"Authorization": f"Bearer {admin_key}"},
    ) as http:
        yield http


async def send_one(server: httpx.AsyncClient, providers: Runtime, channel: str) -> dict[str, Any]:
    await create_template(server)
    await create_recipient(
        server,
        addresses=[
            {"channel": "email", "value": "ann@example.test"},
            {"channel": "ses", "value": "ann@example.test"},
        ],
    )
    sent = await server.post(
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
    return (await server.get(f"/v1/messages/{sent.json()['id']}")).json()


def resend_headers(body: bytes, *, secret: str = SECRET, msg_id: str = "msg_1") -> dict[str, str]:
    timestamp = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{base64.b64encode(digest).decode()}",
        "content-type": "application/json",
    }


async def by_channel(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    addresses = (await client.get("/v1/recipients/user-1")).json()["addresses"]
    return {a["channel"]: a for a in addresses}


async def test_resend_bounces_disable_the_address(
    client: httpx.AsyncClient, providers: Runtime, server: httpx.AsyncClient
) -> None:
    message = await send_one(server, providers, "email")
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
        headers=resend_headers(body, secret="whsec_" + base64.b64encode(b"other").decode()),
    )
    assert forged.status_code == 403
    wrong_channel = await server.post(
        "/v1/inbound/resend/ses", content=body, headers=resend_headers(body)
    )
    assert wrong_channel.status_code == 404
    ok = await server.post("/v1/inbound/resend/email", content=body, headers=resend_headers(body))
    assert ok.status_code == 204

    bounced = (await client.get(f"/v1/messages/{message['id']}")).json()
    assert (bounced["status"], bounced["status_reason"]) == (
        "failed",
        "bounced: mailbox does not exist",
    )
    addresses = await by_channel(client)
    assert {name: a["enabled"] for name, a in addresses.items()} == {"email": False, "ses": True}


async def test_resend_deliveries_are_applied_once_and_delays_ignored(
    client: httpx.AsyncClient, providers: Runtime, server: httpx.AsyncClient
) -> None:
    await send_one(server, providers, "email")

    async def post(event_type: str, msg_id: str, **data: Any) -> None:
        event = {"type": event_type, "data": {"to": ["ann@example.test"], **data}}
        body = json.dumps(event).encode()
        response = await server.post(
            "/v1/inbound/resend/email", content=body, headers=resend_headers(body, msg_id=msg_id)
        )
        assert response.status_code == 204

    temporary = {"email_id": "em-unknown", "bounce": {"type": "Temporary"}}
    for _ in range(3):
        await post("email.bounced", "msg_soft", **temporary)
    for index in range(3):
        await post("email.delivery_delayed", f"msg_delay_{index}", email_id="em-unknown")
    assert (await by_channel(client))["email"]["enabled"] is True
    await post("email.bounced", "msg_soft_2", **temporary)
    assert (await by_channel(client))["email"]["enabled"] is True
    await post("email.bounced", "msg_soft_3", **temporary)
    assert (await by_channel(client))["email"]["enabled"] is False


def sns_message(sign: Any, kind: str, **fields: Any) -> dict[str, Any]:
    return sign(
        {
            "Type": kind,
            "MessageId": "sns-1",
            "TopicArn": TOPIC,
            "Timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "SigningCertURL": CERT_URL,
            **fields,
        }
    )


async def test_ses_notifications_through_sns(
    client: httpx.AsyncClient,
    providers: Runtime,
    server: httpx.AsyncClient,
    signer: tuple[bytes, Any],
    provider_calls: list[httpx.Request],
) -> None:
    _, sign = signer
    message = await send_one(server, providers, "ses")
    assert message["provider_message_id"] == "0100-1"

    confirm = sns_message(
        sign, "SubscriptionConfirmation", Message="confirm", Token="t", SubscribeURL=SUBSCRIBE_URL
    )
    assert (await server.post("/v1/inbound/ses/ses", json=confirm)).status_code == 204
    assert str(provider_calls[-1].url) == SUBSCRIBE_URL

    complaint = {
        "notificationType": "Complaint",
        "mail": {"messageId": "0100-1"},
        "complaint": {"complainedRecipients": [{"emailAddress": "ann@example.test"}]},
    }
    notification = sns_message(sign, "Notification", Message=json.dumps(complaint))
    assert (await server.post("/v1/inbound/ses/ses", json=notification)).status_code == 204
    addresses = await by_channel(client)
    assert addresses["ses"]["complained_at"] is not None
    assert addresses["email"]["complained_at"] is None

    other_topic = sign({**notification, "TopicArn": "arn:aws:sns:eu-west-1:666:evil"})
    tampered = {**notification, "Message": json.dumps({**complaint, "x": 1})}
    foreign = sign({**notification, "SigningCertURL": "https://evil.test/cert.pem"})
    for body in (other_topic, tampered, foreign):
        assert (await server.post("/v1/inbound/ses/ses", json=body)).status_code == 403


async def test_sns_replays_are_ignored_or_refused(
    client: httpx.AsyncClient,
    providers: Runtime,
    server: httpx.AsyncClient,
    signer: tuple[bytes, Any],
) -> None:
    _, sign = signer
    await send_one(server, providers, "ses")
    soft = {
        "eventType": "Bounce",
        "mail": {"messageId": "0100-unknown"},
        "bounce": {
            "bounceType": "Transient",
            "bouncedRecipients": [{"emailAddress": "ann@example.test"}],
        },
    }
    notification = sns_message(sign, "Notification", Message=json.dumps(soft))
    for _ in range(4):
        assert (await server.post("/v1/inbound/ses/ses", json=notification)).status_code == 204
    assert (await by_channel(client))["ses"]["enabled"] is True

    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    stale = sign({**notification, "MessageId": "sns-2", "Timestamp": old})
    refused = await server.post("/v1/inbound/ses/ses", json=stale)
    assert refused.status_code == 403
    assert "hour" in refused.json()["detail"]
    raw = await server.post(
        "/v1/inbound/ses/ses", json=notification, headers={"x-amz-sns-rawdelivery": "true"}
    )
    assert raw.status_code == 403


async def test_sns_failures_on_amazon_side_ask_for_a_retry(
    providers: Runtime, server: httpx.AsyncClient, signer: tuple[bytes, Any]
) -> None:
    _, sign = signer
    unreachable = sign(
        {**sns_message(sign, "Notification", Message="{}"), "SigningCertURL": BROKEN_CERT_URL}
    )
    assert (await server.post("/v1/inbound/ses/ses", json=unreachable)).status_code == 503
    refused = sns_message(
        sign,
        "SubscriptionConfirmation",
        Message="confirm",
        Token="x",
        SubscribeURL=REFUSED_SUBSCRIBE_URL,
    )
    assert (await server.post("/v1/inbound/ses/ses", json=refused)).status_code == 503
