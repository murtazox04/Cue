from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from cue_notify.channels.base import Delivery, DeliveryError, InvalidAddressError
from cue_notify.channels.fcm import FcmChannel, FcmConfig
from cue_notify.channels.registry import ChannelConfigError, ChannelRegistry
from cue_notify.channels.smtp import SmtpChannel, SmtpConfig
from cue_notify.channels.telegram import TelegramChannel, TelegramConfig, format_message
from cue_notify.channels.twilio import TwilioChannel, TwilioConfig
from cue_notify.channels.webhook import SIGNATURE_HEADER, WebhookChannel, WebhookConfig
from cue_notify.core.content import Content
from cue_notify.settings import ChannelSpec

type Handler = Any


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _delivery(address: str = "addr", **content: Any) -> Delivery:
    return Delivery(
        message_id=uuid4(),
        address=address,
        content=Content(**{"title": "Hi", "body": "Body", **content}),
        metadata={"cue_message_id": "m1"},
    )


# --------------------------------------------------------------------------- webhook


async def test_webhook_signs_payload() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, headers={"x-request-id": "r1"})

    config = WebhookConfig.model_validate({"url": "https://hooks.test/in", "secret": "s3cret"})
    async with _client(handler) as http:
        receipt = await WebhookChannel("hook", config, http).send(_delivery())
    assert receipt.provider_message_id == "r1"
    request = captured[0]
    timestamp, signature = (
        part.split("=", 1)[1] for part in request.headers[SIGNATURE_HEADER].split(",")
    )
    expected = hmac.new(
        b"s3cret", f"{timestamp}.".encode() + request.content, hashlib.sha256
    ).hexdigest()
    assert signature == expected
    assert json.loads(request.content)["content"]["body"] == "Body"


async def test_webhook_per_recipient_url_is_pinned_and_errors_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter([503, 410, 400])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(next(statuses), text="internal secrets")

    async def resolve(_: WebhookChannel, host: str, port: int) -> list[str]:
        assert (host, port) == ("hooks.test", 443)
        return ["93.184.216.34"]

    monkeypatch.setattr(WebhookChannel, "resolve", resolve)
    async with _client(handler) as http:
        channel = WebhookChannel("hook", WebhookConfig(), http)
        with pytest.raises(DeliveryError) as transient:
            await channel.send(_delivery("https://hooks.test/in"))
        assert transient.value.retryable
        assert "secrets" not in str(transient.value)  # untrusted bodies are not kept
        with pytest.raises(InvalidAddressError):
            await channel.send(_delivery("https://hooks.test/in"))
        with pytest.raises(DeliveryError) as permanent:
            await channel.send(_delivery("https://hooks.test/in"))
        assert not permanent.value.retryable

    request = seen[0]
    assert request.url.host == "93.184.216.34"  # connected to the address that was checked
    assert request.headers["host"] == "hooks.test"
    assert request.extensions["sni_hostname"] == "hooks.test"


@pytest.mark.parametrize(
    ("address", "resolved"),
    [
        ("https://169.254.169.254/latest/meta-data", None),
        ("https://[::1]/admin", None),
        ("https://internal.test/", ["10.0.0.7"]),
        ("https://mixed.test/", ["93.184.216.34", "127.0.0.1"]),
        ("http://plain.test/", ["93.184.216.34"]),
        ("ftp://files.test/", None),
        ("not a url", None),
    ],
)
async def test_webhook_rejects_unsafe_destinations(
    monkeypatch: pytest.MonkeyPatch, address: str, resolved: list[str] | None
) -> None:
    async def resolve(_: WebhookChannel, host: str, port: int) -> list[str]:
        return resolved or []

    monkeypatch.setattr(WebhookChannel, "resolve", resolve)

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("must not connect")

    async with _client(handler) as http:
        channel = WebhookChannel("hook", WebhookConfig(), http)
        with pytest.raises(InvalidAddressError):
            await channel.send(_delivery(address))


async def test_webhook_private_networks_can_be_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(_: WebhookChannel, host: str, port: int) -> list[str]:
        return ["10.0.0.7"]

    monkeypatch.setattr(WebhookChannel, "resolve", resolve)
    async with _client(lambda _: httpx.Response(204)) as http:
        config = WebhookConfig(allow_private_networks=True, allow_http=True)
        await WebhookChannel("hook", config, http).send(_delivery("http://intranet.test/hook"))


# --------------------------------------------------------------------------- twilio


async def test_twilio_sends_form_and_maps_dead_numbers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        if form["To"] == "+10000000000":
            return httpx.Response(400, json={"code": 21211, "message": "Invalid 'To'"})
        assert form["Body"] == "Hi\nBody\nhttps://x.test"
        assert form["MessagingServiceSid"] == "MG1"
        return httpx.Response(201, json={"sid": "SM123"})

    config = TwilioConfig(
        account_sid="AC1", auth_token="t", messaging_service_sid="MG1", include_title=True
    )
    async with _client(handler) as http:
        channel = TwilioChannel("sms", config, http)
        receipt = await channel.send(_delivery("+15550001", url="https://x.test"))
        assert receipt.provider_message_id == "SM123"
        with pytest.raises(InvalidAddressError, match="21211"):
            await channel.send(_delivery("+10000000000"))


def test_twilio_requires_a_sender() -> None:
    with pytest.raises(ValidationError, match="from_number or messaging_service_sid"):
        TwilioConfig(account_sid="AC1", auth_token="t")


# --------------------------------------------------------------------------- telegram


async def test_telegram_escapes_html_and_detects_blocked_bots() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["chat_id"] == "blocked":
            return httpx.Response(
                403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
            )
        if payload["chat_id"] == "busy":
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 7},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with _client(handler) as http:
        channel = TelegramChannel("tg", TelegramConfig(bot_token="123:abc"), http)
        assert (await channel.send(_delivery("1"))).provider_message_id == "42"
        with pytest.raises(InvalidAddressError):
            await channel.send(_delivery("blocked"))
        with pytest.raises(DeliveryError) as busy:
            await channel.send(_delivery("busy"))
        assert busy.value.retryable
        assert busy.value.retry_after == 7
    assert format_message("<Hi>", "a & b", None) == "<b>&lt;Hi&gt;</b>\n\na &amp; b"


# --------------------------------------------------------------------------- fcm


@pytest.fixture(scope="module")
def service_account() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "project_id": "demo",
            "client_email": "svc@demo.iam.gserviceaccount.com",
            "private_key": pem,
            "token_uri": "https://oauth.test/token",
        }
    )


async def test_fcm_authenticates_builds_message_and_classifies(service_account: str) -> None:
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.host == "oauth.test":
            token_requests += 1
            return httpx.Response(200, json={"access_token": "ya29", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer ya29"
        message = json.loads(request.content)["message"]
        if message["token"] == "dead":
            return httpx.Response(
                404,
                json={
                    "error": {
                        "status": "NOT_FOUND",
                        "message": "x",
                        "details": [{"errorCode": "UNREGISTERED"}],
                    }
                },
            )
        assert message["notification"] == {
            "title": "Hi",
            "body": "Body",
            "image": "https://i.test/a.png",
        }
        assert message["data"] == {"cue_message_id": "m1", "url": "app://home"}
        assert message["android"]["priority"] == "HIGH"
        return httpx.Response(200, json={"name": "projects/demo/messages/1"})

    config = FcmConfig(credentials_json=service_account)
    async with _client(handler) as http:
        channel = FcmChannel("push", config, http)
        receipt = await channel.send(
            _delivery("tok", url="app://home", image_url="https://i.test/a.png")
        )
        assert receipt.provider_message_id == "projects/demo/messages/1"
        with pytest.raises(InvalidAddressError):
            await channel.send(_delivery("dead"))
    assert token_requests == 1  # cached between sends


async def test_fcm_thread_groups_notifications_on_both_platforms(service_account: str) -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.test":
            return httpx.Response(200, json={"access_token": "ya29", "expires_in": 3600})
        payloads.append(json.loads(request.content)["message"])
        return httpx.Response(200, json={"name": "projects/demo/messages/1"})

    async with _client(handler) as http:
        channel = FcmChannel("push", FcmConfig(credentials_json=service_account), http)
        await channel.send(_delivery("tok", thread="post-1"))
        await channel.send(_delivery("tok"))
    grouped, plain = payloads
    assert grouped["data"]["thread"] == "post-1"
    assert grouped["apns"]["payload"]["aps"]["thread-id"] == "post-1"
    assert "thread" not in plain["data"]
    assert "thread-id" not in plain["apns"]["payload"]["aps"]


def test_fcm_needs_exactly_one_credential_source() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        FcmConfig()


# --------------------------------------------------------------------------- smtp


def test_smtp_builds_multipart_email() -> None:
    config = SmtpConfig(host="smtp.test", from_address="noreply@x.test", from_name="Shop")
    channel = SmtpChannel("email", config, httpx.AsyncClient())
    email = channel.build(_delivery("ann@x.test", html="<p>Body</p>", url="https://x.test"))
    assert email["Subject"] == "Hi"
    assert email["From"] == "Shop <noreply@x.test>"
    assert email.get_content_type() == "multipart/alternative"
    text_part, html_part = email.iter_parts()
    assert "https://x.test" in text_part.get_content()
    assert html_part.get_content().strip() == "<p>Body</p>"
    assert email["References"] is None


def test_smtp_adds_one_click_unsubscribe_headers() -> None:
    config = SmtpConfig(host="smtp.test", from_address="noreply@x.test")
    channel = SmtpChannel("email", config, httpx.AsyncClient())
    url = "https://n.test/preferences/t/unsubscribe?category=marketing"
    delivery = Delivery(
        message_id=uuid4(),
        address="ann@x.test",
        content=Content(title="Hi", body="Body"),
        links={"preferences": "https://n.test/preferences/t", "unsubscribe": url},
    )
    email = channel.build(delivery)
    assert email["List-Unsubscribe"] == f"<{url}>"
    assert email["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert channel.build(_delivery("ann@x.test"))["List-Unsubscribe"] is None


def test_smtp_thread_becomes_a_stable_references_header() -> None:
    config = SmtpConfig(host="smtp.test", from_address="noreply@x.test")
    channel = SmtpChannel("email", config, httpx.AsyncClient())
    first = channel.build(_delivery("ann@x.test", thread="post 1/ü"))
    second = channel.build(_delivery("ann@x.test", thread="post 1/ü"))
    other = channel.build(_delivery("ann@x.test", thread="post-2"))
    assert first["References"] == second["References"] != other["References"]
    assert first["References"].endswith("@thread.cue>")


# --------------------------------------------------------------------------- registry


async def test_registry_builds_configured_channels_and_reports_errors() -> None:
    async with httpx.AsyncClient() as http:
        registry = ChannelRegistry.from_settings(
            {
                "log": ChannelSpec(provider="console"),
                "off": ChannelSpec(provider="console", enabled=False),
            },
            http,
        )
        assert list(registry) == ["log"]
        with pytest.raises(ChannelConfigError, match="unknown provider 'carrier-pigeon'"):
            ChannelRegistry.from_settings({"x": ChannelSpec(provider="carrier-pigeon")}, http)
        with pytest.raises(ChannelConfigError, match="bot_token"):
            ChannelRegistry.from_settings({"tg": ChannelSpec(provider="telegram")}, http)
