"""What comes back after sending: replies, delivery statuses, bounces and complaints.

Replies from people: STOP and START keywords sent back to your text messages.

`POST /v1/inbound/sms` takes a reply from any provider, called by your backend. Twilio
can call `POST /v1/inbound/twilio` directly: point the number's (or messaging service's)
incoming-message webhook there. Twilio's request signature is checked against the
Twilio channel's auth token.

Delivery feedback from providers (see :mod:`cue_notify.services.feedback`): Twilio's
status callback, Resend's webhooks and Amazon SNS notifications for SES, each verified
with the provider's own signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Iterable
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from cue_notify.api.deps import RecipientsWriter, RuntimeDep, SessionDep
from cue_notify.api.pages import form_fields
from cue_notify.channels import resend, ses
from cue_notify.channels.twilio import delivery_feedback
from cue_notify.core.enums import FeedbackType
from cue_notify.services import agents, feedback, inbound
from cue_notify.services.errors import ForbiddenError, NotFoundError, UnprocessableError

router = APIRouter(prefix="/inbound", tags=["inbound"])
logger = logging.getLogger(__name__)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


class InboundSms(BaseModel):
    from_: str = Field(alias="from", min_length=2, max_length=32, examples=["+15551234567"])
    body: str = Field(max_length=1600, examples=["STOP"])
    message_id: str | None = Field(default=None, max_length=100)


class InboundOut(BaseModel):
    action: inbound.Action | None
    recipients: list[str]


@router.post("/sms", summary="Handle a reply to a text message")
async def inbound_sms(
    body: InboundSms, key: RecipientsWriter, session: SessionDep, runtime: RuntimeDep
) -> InboundOut:
    """STOP (and the other FCC keywords) mutes text messages and unsubscribes the sender's
    recipients from optional categories; START unmutes text messages. Other replies are
    ignored (`action: null`)."""
    agents.check_not_agent(key, "report replies on people's behalf")
    provenance: dict[str, Any] = {"method": "sms_reply", "api_key": key.name}
    if body.message_id:
        provenance["message_id"] = body.message_id
    result = await inbound.handle(session, runtime, body.from_, body.body, provenance)
    return InboundOut(action=result.action, recipients=result.recipients)


def twilio_signature(auth_token: str, url: str, params: dict[str, list[str]]) -> str:
    """Twilio's request signature: the URL, then every parameter (empty ones included)
    as name + value, sorted by name and then by value, HMAC-SHA1 with the auth token."""
    payload = url + "".join(
        key + value for key in sorted(params) for value in sorted(set(params[key]))
    )
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def twilio_channels(runtime: RuntimeDep, account_sid: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for name in runtime.channels:
        channel = runtime.channels.get(name)
        config: Any = channel.config if channel is not None else None
        if (
            channel is not None
            and channel.provider == "twilio"
            and hmac.compare_digest(str(config.account_sid).encode(), account_sid.encode())
        ):
            tokens[name] = str(config.auth_token.get_secret_value())
    if not tokens:
        raise NotFoundError("Twilio channel", "for this account")
    return tokens


async def twilio_form(request: Request, runtime: RuntimeDep) -> dict[str, list[str]]:
    if runtime.settings.api.public_url is None:
        raise UnprocessableError("set api.public_url so Twilio's signature can be checked")
    return await form_fields(request, keep_blank=True)


def verify_twilio(
    request: Request,
    runtime: RuntimeDep,
    path: str,
    params: dict[str, list[str]],
    tokens: Iterable[str],
) -> None:
    url = f"{str(runtime.settings.api.public_url).rstrip('/')}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    received = request.headers.get("X-Twilio-Signature", "").encode("latin-1")
    if not any(
        hmac.compare_digest(twilio_signature(token, url, params).encode(), received)
        for token in set(tokens)
    ):
        raise ForbiddenError("invalid Twilio signature")


def _twilio_token(runtime: RuntimeDep) -> str:
    for name in runtime.settings.compliance.sms_channels:
        channel = runtime.channels.get(name) if name in runtime.channels else None
        if channel is not None and channel.provider == "twilio":
            token: Any = channel.config.auth_token  # pyright: ignore[reportAttributeAccessIssue]
            return str(token.get_secret_value())
    raise NotFoundError("Twilio channel", "in compliance.sms_channels")


@router.post("/twilio", summary="Twilio incoming-message webhook", include_in_schema=False)
async def inbound_twilio(request: Request, session: SessionDep, runtime: RuntimeDep) -> Response:
    params = await twilio_form(request, runtime)
    verify_twilio(request, runtime, "/v1/inbound/twilio", params, [_twilio_token(runtime)])
    phone = (params.get("From") or [""])[0]
    text = (params.get("Body") or [""])[0]
    provenance = {"method": "sms_reply", "provider": "twilio"}
    if sid := (params.get("MessageSid") or [""])[0]:
        provenance["message_id"] = sid
    await inbound.handle(session, runtime, phone, text, provenance)
    return Response(EMPTY_TWIML, media_type="application/xml")


@router.post(
    "/twilio/status",
    summary="Twilio delivery status callback",
    description="Set the Twilio channel's `status_callback` to "
    "`<public_url>/v1/inbound/twilio/status`. Delivered texts are marked delivered; numbers "
    "that cannot receive texts are disabled.",
    include_in_schema=False,
)
async def twilio_status(request: Request, session: SessionDep, runtime: RuntimeDep) -> Response:
    params = await twilio_form(request, runtime)
    fields = {name: values[0] for name, values in params.items() if values}
    tokens = twilio_channels(runtime, fields.get("AccountSid", ""))
    verify_twilio(request, runtime, "/v1/inbound/twilio/status", params, tokens.values())
    report = delivery_feedback(fields)
    sid = fields.get("MessageSid")
    if report is None or not sid:
        return Response(status_code=204)
    channel = await feedback.locate_channel(session, list(tokens), sid)
    if channel is None:
        return Response(status_code=204)
    kind, reason = report
    await feedback.apply(
        session,
        runtime,
        feedback.FeedbackIn(
            type=kind,
            channel=channel,
            address=fields.get("To") or None,
            provider_message_id=sid,
            report_id=f"twilio:{sid}:{fields.get('MessageStatus', '')}",
            reason=reason,
        ),
        {"source": "twilio"},
        require_message=True,
    )
    return Response(status_code=204)


# --------------------------------------------------------------------------- e-mail providers

_SNS_CERTIFICATES: dict[str, bytes] = {}


def _provider_channel(runtime: RuntimeDep, name: str, provider: str) -> Any:
    channel = runtime.channels.get(name) if name in runtime.channels else None
    if channel is None or channel.provider != provider:
        raise NotFoundError(f"{provider} channel", name)
    return channel


async def _apply_reports(
    session: SessionDep,
    runtime: RuntimeDep,
    channel: str,
    reports: list[tuple[FeedbackType, str, str, str]],
    source: str,
) -> None:
    for kind, reason, provider_id, address in reports:
        await feedback.apply(
            session,
            runtime,
            feedback.FeedbackIn(
                type=kind,
                channel=channel,
                address=address[:2048] or None,
                provider_message_id=provider_id[:255] or None,
                reason=reason[:300],
            ),
            {"source": source},
        )


@router.post("/resend/{channel}", summary="Resend webhook", include_in_schema=False)
async def resend_webhook(
    channel: str, request: Request, session: SessionDep, runtime: RuntimeDep
) -> Response:
    """Point a Resend webhook here (`email.delivered`, `email.bounced`,
    `email.delivery_delayed`, `email.complained`, `email.suppressed`) and set the
    channel's `webhook_secret`."""
    config: Any = _provider_channel(runtime, channel, "resend").config
    if config.webhook_secret is None:
        raise UnprocessableError(f"set webhook_secret on channel {channel!r}")
    body = await request.body()
    headers = {name: request.headers.get(name, "") for name in resend.SIGNATURE_HEADERS}
    if not resend.verify_webhook(config.webhook_secret.get_secret_value(), headers, body):
        raise ForbiddenError("invalid Resend signature")
    try:
        event = json.loads(body)
    except ValueError as exc:
        raise UnprocessableError("the body is not JSON") from exc
    report = resend.feedback(event if isinstance(event, dict) else {})
    if report is not None:
        kind, reason, email_id, addresses = report
        reports = [(kind, reason, email_id, address) for address in addresses[:50]]
        await _apply_reports(session, runtime, channel, reports, "resend")
    return Response(status_code=204)


@router.post("/ses/{channel}", summary="Amazon SNS notifications for SES", include_in_schema=False)
async def ses_notification(
    channel: str, request: Request, session: SessionDep, runtime: RuntimeDep
) -> Response:
    """Subscribe this URL (HTTPS) to the SNS topic that SES publishes bounces,
    complaints and deliveries to, and list the topic in the channel's `sns_topic_arns`.
    Subscriptions are confirmed automatically for listed topics."""
    ses_channel = _provider_channel(runtime, channel, "ses")
    config: Any = ses_channel.config
    try:
        message = json.loads(await request.body())
    except ValueError as exc:
        raise UnprocessableError("the body is not JSON") from exc
    if not isinstance(message, dict):
        raise UnprocessableError("the body is not an SNS message")
    sns: dict[str, Any] = message  # pyright: ignore[reportUnknownVariableType]
    # Anyone can make an SNS topic and point it here; only the configured ones count.
    if sns.get("TopicArn") not in config.sns_topic_arns:
        raise ForbiddenError("unknown SNS topic")
    certificate_url = str(sns.get("SigningCertURL", ""))
    if not ses.is_sns_url(certificate_url, suffix=".pem"):
        raise ForbiddenError("the signing certificate is not Amazon SNS's")
    try:
        ses.verify_signature(sns, await _sns_certificate(runtime, certificate_url))
    except ses.SnsError as exc:
        raise ForbiddenError(str(exc)) from exc

    kind = sns.get("Type")
    if kind == "SubscriptionConfirmation":
        subscribe_url = str(sns.get("SubscribeURL", ""))
        if not ses.is_sns_url(subscribe_url):
            raise ForbiddenError("the subscription URL is not Amazon SNS's")
        try:
            response = await runtime.http.get(subscribe_url)
        except httpx.HTTPError as exc:
            raise UnprocessableError(f"could not confirm the subscription: {exc}") from exc
        if not response.is_success:
            raise UnprocessableError(f"SNS refused the confirmation: HTTP {response.status_code}")
        # The configured name, not the request path: nothing from the request is logged.
        logger.info("confirmed SNS subscription", extra={"channel": ses_channel.name})
    elif kind == "Notification":
        try:
            notification = json.loads(str(sns.get("Message", "")))
        except ValueError:
            notification = None
        if isinstance(notification, dict):
            reports = ses.feedback(notification)  # pyright: ignore[reportUnknownArgumentType]
            await _apply_reports(session, runtime, channel, reports[:50], "ses")
    return Response(status_code=204)


async def _sns_certificate(runtime: RuntimeDep, url: str) -> bytes:
    cached = _SNS_CERTIFICATES.get(url)
    if cached is not None:
        return cached
    try:
        response = await runtime.http.get(url)
    except httpx.HTTPError as exc:
        raise ForbiddenError(f"could not fetch the SNS certificate: {exc}") from exc
    if not response.is_success:
        raise ForbiddenError(f"could not fetch the SNS certificate: HTTP {response.status_code}")
    if len(_SNS_CERTIFICATES) >= 32:
        _SNS_CERTIFICATES.clear()
    _SNS_CERTIFICATES[url] = response.content
    return response.content
