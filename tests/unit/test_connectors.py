"""Slack, Resend, Amazon SES (with SNS) and APNs connectors."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from cue_notify.channels import resend, ses
from cue_notify.channels.apns import ApnsChannel, ApnsConfig
from cue_notify.channels.base import Delivery, DeliveryError, InvalidAddressError
from cue_notify.channels.resend import ResendChannel, ResendConfig
from cue_notify.channels.ses import SesChannel, SesConfig
from cue_notify.channels.slack import SlackChannel, SlackConfig
from cue_notify.core.content import Action, Content
from cue_notify.core.enums import FeedbackType, Importance

Handler = Any


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _delivery(address: str, **content: Any) -> Delivery:
    return Delivery(
        message_id=uuid4(),
        address=address,
        content=Content(title="Order shipped", body="Order <A-1> & co is on its way.", **content),
        importance=Importance.HIGH,
        links={
            "preferences": "https://cue.test/preferences/tok",
            "unsubscribe": "https://cue.test/preferences/tok/unsubscribe?category=x",
        },
        metadata={"cue_message_id": "m-1"},
    )


# --------------------------------------------------------------------------- Slack


def _slack_error(error: str) -> httpx.Response:
    return httpx.Response(200, json={"ok": False, "error": error})


async def test_slack_posts_blocks_with_escaped_text_and_buttons() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "channel": "D1", "ts": "1.2"})

    actions = [Action(id="track", label="Track", url="https://shop.test/t")]
    async with _client(handler) as http:
        channel = SlackChannel("slack", SlackConfig(bot_token="xoxb-1"), http)
        receipt = await channel.send(_delivery("U123", url="https://shop.test/o", actions=actions))
    assert receipt.provider_message_id == "D1:1.2"
    request = seen[0]
    assert request.headers["authorization"] == "Bearer xoxb-1"
    body = json.loads(request.content)
    assert body["channel"] == "U123"
    kinds = [block["type"] for block in body["blocks"]]
    assert kinds == ["header", "section", "actions", "context"]
    assert body["blocks"][1]["text"]["text"] == "Order &lt;A-1&gt; &amp; co is on its way."
    assert [b["url"] for b in body["blocks"][2]["elements"]] == [
        "https://shop.test/o",
        "https://shop.test/t",
    ]


@pytest.mark.parametrize(
    ("response", "error", "retryable"),
    [
        (_slack_error("user_not_found"), InvalidAddressError, False),
        (_slack_error("invalid_auth"), DeliveryError, False),
        (_slack_error("internal_error"), DeliveryError, True),
        (httpx.Response(429, headers={"Retry-After": "7"}), DeliveryError, True),
    ],
)
async def test_slack_errors_are_classified(
    response: httpx.Response, error: type[Exception], retryable: bool
) -> None:
    async with _client(lambda _: response) as http:
        channel = SlackChannel("slack", SlackConfig(bot_token="xoxb-1"), http)
        with pytest.raises(error) as raised:
            await channel.send(_delivery("U123"))
    assert isinstance(raised.value, DeliveryError)
    assert raised.value.retryable is retryable
    if response.status_code == 429:
        assert raised.value.retry_after == 7


# --------------------------------------------------------------------------- Resend


async def test_resend_sends_once_with_unsubscribe_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "em-1"})

    config = ResendConfig(api_key="re_1", from_address="hi@shop.test", from_name="Shop")
    async with _client(handler) as http:
        delivery = _delivery("ann@example.test", html="<p>hi</p>")
        receipt = await ResendChannel("email", config, http).send(delivery)
    assert receipt.provider_message_id == "em-1"
    request = seen[0]
    assert request.headers["idempotency-key"] == str(delivery.message_id)
    body = json.loads(request.content)
    assert body["from"] == "Shop <hi@shop.test>"
    assert body["to"] == ["ann@example.test"]
    assert body["html"] == "<p>hi</p>"
    assert body["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert body["headers"]["Importance"] == "high"


async def test_resend_errors_and_addresses() -> None:
    responses = iter([httpx.Response(422, json={"message": "bad"}), httpx.Response(503)])
    config = ResendConfig(api_key="re_1", from_address="hi@shop.test")
    async with _client(lambda _: next(responses)) as http:
        channel = ResendChannel("email", config, http)
        with pytest.raises(InvalidAddressError):
            await channel.send(_delivery("a@x.test, b@y.test"))
        with pytest.raises(DeliveryError) as permanent:
            await channel.send(_delivery("ann@example.test"))
        assert not permanent.value.retryable
        with pytest.raises(DeliveryError) as transient:
            await channel.send(_delivery("ann@example.test"))
        assert transient.value.retryable


def test_resend_webhook_signature_matches_svix() -> None:
    # The example from Svix's documentation of manual verification.
    secret = "whsec_plJ3nmyCDGBKInavdOK15jsl"
    body = b'{"event_type":"ping","data":{"success":true}}'
    headers = {
        "svix-id": "msg_loFOjxBNrRLzqYUf",
        "svix-timestamp": "1731705121",
        "svix-signature": "v1,bad v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0=",
    }
    assert resend.verify_webhook(secret, headers, body, now=1731705121)
    assert not resend.verify_webhook(secret, headers, body + b" ", now=1731705121)
    assert not resend.verify_webhook(secret, headers, body, now=1731705121 + 301)
    assert not resend.verify_webhook("whsec_###", headers, body, now=1731705121)
    junk = {**headers, "svix-signature": "v1,é"}
    assert not resend.verify_webhook(secret, junk, body, now=1731705121)


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        ({"type": "email.delivered"}, FeedbackType.DELIVERED),
        ({"type": "email.bounced", "data": {"bounce": {"type": "Permanent"}}}, FeedbackType.BOUNCE),
        (
            {"type": "email.bounced", "data": {"bounce": {"type": "Temporary"}}},
            FeedbackType.SOFT_BOUNCE,
        ),
        ({"type": "email.delivery_delayed"}, FeedbackType.SOFT_BOUNCE),
        ({"type": "email.complained"}, FeedbackType.COMPLAINT),
        ({"type": "email.suppressed"}, FeedbackType.BOUNCE),
        ({"type": "email.opened"}, None),
    ],
)
def test_resend_events_map_to_feedback(event: dict[str, Any], kind: FeedbackType | None) -> None:
    event.setdefault("data", {}).update({"email_id": "em-1", "to": ["ann@example.test"]})
    report = resend.feedback(event)
    assert (report[0] if report else None) == kind
    if report:
        assert report[2:] == ("em-1", ["ann@example.test"])


# --------------------------------------------------------------------------- SES


def test_sigv4_matches_botocore() -> None:
    # Computed with botocore's SigV4Auth for the same request.
    headers = ses.sign_v4(
        "POST",
        "https://email.eu-west-1.amazonaws.com/v2/email/outbound-emails",
        b'{"FromEmailAddress":"a@b.test"}',
        region="eu-west-1",
        service="ses",
        credentials=("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "session-token"),
        now=datetime(2026, 10, 12, 4, 19, 57, tzinfo=UTC),
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261012/eu-west-1/ses/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token, "
        "Signature=bed1b348af9d22e8c364f58050fcb6b321139f738a8098b74df5e947d6a516aa"
    )
    assert headers["x-amz-security-token"] == "session-token"


def _ses_config(**options: Any) -> SesConfig:
    return SesConfig(
        region="eu-west-1",
        from_address="hi@shop.test",
        access_key_id="AKID",
        secret_access_key="secret",
        configuration_set="cue",
        **options,
    )


async def test_ses_sends_a_signed_simple_message() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"MessageId": "0100-1"})

    async with _client(handler) as http:
        receipt = await SesChannel("email", _ses_config(), http).send(_delivery("ann@example.test"))
    assert receipt.provider_message_id == "0100-1"
    request = seen[0]
    assert str(request.url) == "https://email.eu-west-1.amazonaws.com/v2/email/outbound-emails"
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    body = json.loads(request.content)
    assert body["Destination"] == {"ToAddresses": ["ann@example.test"]}
    assert body["ConfigurationSetName"] == "cue"
    simple = body["Content"]["Simple"]
    assert simple["Subject"]["Data"] == "Order shipped"
    assert {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"} in simple[
        "Headers"
    ]


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        (
            httpx.Response(
                400,
                headers={"x-amzn-ErrorType": "MessageRejected:http://internal"},
                json={"message": "Email address is not verified."},
            ),
            False,
        ),
        (httpx.Response(429, headers={"x-amzn-ErrorType": "TooManyRequestsException"}), True),
        (httpx.Response(400, json={"__type": "com.amazon#LimitExceededException"}), True),
    ],
)
async def test_ses_errors_are_classified(response: httpx.Response, retryable: bool) -> None:
    async with _client(lambda _: response) as http:
        with pytest.raises(DeliveryError) as raised:
            await SesChannel("email", _ses_config(), http).send(_delivery("ann@example.test"))
    assert raised.value.retryable is retryable
    assert not isinstance(raised.value, InvalidAddressError)


def test_ses_needs_both_keys_or_none(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="both"):
        SesConfig(region="eu-west-1", from_address="a@b.test", access_key_id="AKID")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    config = SesConfig(region="eu-west-1", from_address="a@b.test")
    with pytest.raises(DeliveryError):
        config.credentials()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    assert config.credentials() == ("AKENV", "s", None)


def sns_signer() -> tuple[bytes, Any]:
    """A self-signed certificate and a function that signs SNS messages with its key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )

    def sign(message: dict[str, Any], version: str = "2") -> dict[str, Any]:
        signed = {**message, "SignatureVersion": version}
        algorithm = hashes.SHA256() if version == "2" else hashes.SHA1()  # noqa: S303
        signature = key.sign(ses.string_to_sign(signed), padding.PKCS1v15(), algorithm)
        return {**signed, "Signature": base64.b64encode(signature).decode()}

    return certificate.public_bytes(serialization.Encoding.PEM), sign


@pytest.mark.parametrize("version", ["1", "2"])
def test_sns_signatures_are_verified(version: str) -> None:
    pem, sign = sns_signer()
    message = sign(
        {
            "Type": "Notification",
            "MessageId": "m-1",
            "TopicArn": "arn:aws:sns:eu-west-1:1:ses",
            "Message": '{"eventType":"Bounce"}',
            "Timestamp": "2026-10-12T00:00:00.000Z",
        },
        version,
    )
    ses.verify_signature(message, pem)
    with pytest.raises(ses.SnsError):
        ses.verify_signature({**message, "Message": '{"eventType":"Delivery"}'}, pem)
    with pytest.raises(ses.SnsError):
        ses.verify_signature({**message, "SignatureVersion": "3"}, pem)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-abc.pem", True),
        ("https://sns.cn-north-1.amazonaws.com.cn/SimpleNotificationService-abc.pem", True),
        ("http://sns.eu-west-1.amazonaws.com/SimpleNotificationService-abc.pem", False),
        ("https://sns.eu-west-1.amazonaws.com.evil.test/cert.pem", False),
        ("https://evil.test/sns.eu-west-1.amazonaws.com/cert.pem", False),
        ("https://sns.eu-west-1.amazonaws.com:8443/cert.pem", False),
        ("https://sns.eu-west-1.amazonaws.com/cert.txt", False),
    ],
)
def test_only_amazon_sns_serves_certificates(url: str, ok: bool) -> None:
    assert ses.is_sns_url(url, suffix=".pem") is ok


def test_ses_events_map_to_feedback() -> None:
    mail = {"mail": {"messageId": "0100-1"}}
    bounce = {
        **mail,
        "eventType": "Bounce",
        "bounce": {
            "bounceType": "Permanent",
            "bouncedRecipients": [{"emailAddress": "a@x.test", "diagnosticCode": "550 5.1.1"}],
        },
    }
    assert ses.feedback(bounce) == [(FeedbackType.BOUNCE, "550 5.1.1", "0100-1", "a@x.test")]
    transient = {**bounce, "bounce": {**bounce["bounce"], "bounceType": "Transient"}}
    assert ses.feedback(transient)[0][0] is FeedbackType.SOFT_BOUNCE
    complaint = {
        **mail,
        "notificationType": "Complaint",
        "complaint": {"complainedRecipients": [{"emailAddress": "a@x.test"}]},
    }
    assert ses.feedback(complaint)[0][0] is FeedbackType.COMPLAINT
    delivery = {**mail, "eventType": "Delivery", "delivery": {"recipients": ["a@x.test"]}}
    assert ses.feedback(delivery) == [(FeedbackType.DELIVERED, "delivered", "0100-1", "a@x.test")]
    assert ses.feedback({**mail, "eventType": "Open"}) == []


# --------------------------------------------------------------------------- APNs

TOKEN = "a" * 64


def _apns_key() -> tuple[str, Any]:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


class _TestApns(ApnsChannel):
    handler: Handler = None

    def make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _apns(handler: Handler, pem: str, **options: Any) -> ApnsChannel:
    _TestApns.handler = staticmethod(handler)  # type: ignore[assignment]
    config = ApnsConfig(
        team_id="TEAM123456", key_id="KEY1234567", key=pem, bundle_id="test.shop", **options
    )
    return _TestApns("ios", config, httpx.AsyncClient())


async def test_apns_sends_an_alert_with_a_signed_token() -> None:
    pem, public = _apns_key()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"apns-id": "A-1"})

    channel = _apns(handler, pem, category="ORDER")
    delivery = _delivery(TOKEN, thread="order-1", url="https://shop.test/o")
    receipt = await channel.send(delivery)
    await channel.aclose()
    assert receipt.provider_message_id == "A-1"
    request = seen[0]
    assert str(request.url) == f"https://api.push.apple.com/3/device/{TOKEN}"
    assert request.headers["apns-topic"] == "test.shop"
    assert request.headers["apns-priority"] == "10"
    token = request.headers["authorization"].removeprefix("bearer ")
    claims = jwt.decode(token, public, algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456"
    assert jwt.get_unverified_header(token)["kid"] == "KEY1234567"
    body = json.loads(request.content)
    assert body["aps"]["alert"] == {"title": "Order shipped", "body": delivery.content.body}
    assert body["aps"]["interruption-level"] == "time-sensitive"
    assert body["aps"]["thread-id"] == "order-1"
    assert body["aps"]["category"] == "ORDER"
    assert body["url"] == "https://shop.test/o"
    assert body["cue_message_id"] == "m-1"


async def test_apns_errors_are_classified_and_tokens_refreshed() -> None:
    pem, _ = _apns_key()
    responses = iter(
        [
            httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
            httpx.Response(200, headers={"apns-id": "A-2"}),
            httpx.Response(410, json={"reason": "Unregistered"}),
            httpx.Response(400, json={"reason": "BadDeviceToken"}),
            httpx.Response(429, json={"reason": "TooManyRequests"}),
            httpx.Response(400, json={"reason": "PayloadTooLarge"}),
        ]
    )
    channel = _apns(lambda _: next(responses), pem)
    assert (await channel.send(_delivery(TOKEN))).provider_message_id == "A-2"
    for error, retryable in (
        (InvalidAddressError, False),
        (InvalidAddressError, False),
        (DeliveryError, True),
        (DeliveryError, False),
    ):
        with pytest.raises(error) as raised:
            await channel.send(_delivery(TOKEN))
        assert isinstance(raised.value, DeliveryError)
        assert raised.value.retryable is retryable
    with pytest.raises(InvalidAddressError):
        await channel.send(_delivery("../../2/admin"))
    await channel.aclose()


def test_apns_needs_exactly_one_key() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ApnsConfig(team_id="TEAM123456", key_id="KEY1234567", bundle_id="test.shop")
