"""Replies from people: STOP and START keywords sent back to your text messages.

`POST /v1/inbound/sms` takes a reply from any provider, called by your backend. Twilio
can call `POST /v1/inbound/twilio` directly: point the number's (or messaging service's)
incoming-message webhook there. Twilio's request signature is checked against the
Twilio channel's auth token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from cue_notify.api.deps import RecipientsWriter, RuntimeDep, SessionDep
from cue_notify.api.pages import form_fields
from cue_notify.channels.twilio import delivery_feedback
from cue_notify.services import agents, feedback, inbound
from cue_notify.services.errors import ForbiddenError, NotFoundError, UnprocessableError

router = APIRouter(prefix="/inbound", tags=["inbound"])

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


def _twilio_channel(runtime: RuntimeDep, account_sid: str) -> tuple[str, str]:
    """(channel name, auth token) of the Twilio channel for ``account_sid``."""
    for name in runtime.channels:
        channel = runtime.channels.get(name)
        config: Any = channel.config if channel is not None else None
        if (
            channel is not None
            and channel.provider == "twilio"
            and hmac.compare_digest(str(config.account_sid).encode(), account_sid.encode())
        ):
            return name, str(config.auth_token.get_secret_value())
    raise NotFoundError("Twilio channel", "for this account")


async def _signed_twilio_form(
    request: Request, runtime: RuntimeDep, path: str, token: str | None
) -> dict[str, list[str]]:
    public_url = runtime.settings.api.public_url
    if public_url is None:
        raise UnprocessableError("set api.public_url so Twilio's signature can be checked")
    params = await form_fields(request, keep_blank=True)  # Twilio signs empty fields too
    if token is None:
        token = _twilio_channel(runtime, (params.get("AccountSid") or [""])[0])[1]
    url = f"{str(public_url).rstrip('/')}{path}"
    expected = twilio_signature(token, url, params).encode()
    # Bytes: compare_digest refuses non-ASCII str, which would turn a junk header into 500.
    received = request.headers.get("X-Twilio-Signature", "").encode("latin-1")
    if not hmac.compare_digest(expected, received):
        raise ForbiddenError("invalid Twilio signature")
    return params


def _twilio_token(runtime: RuntimeDep) -> str:
    for name in runtime.settings.compliance.sms_channels:
        channel = runtime.channels.get(name) if name in runtime.channels else None
        if channel is not None and channel.provider == "twilio":
            token: Any = channel.config.auth_token  # pyright: ignore[reportAttributeAccessIssue]
            return str(token.get_secret_value())
    raise NotFoundError("Twilio channel", "in compliance.sms_channels")


@router.post("/twilio", summary="Twilio incoming-message webhook", include_in_schema=False)
async def inbound_twilio(request: Request, session: SessionDep, runtime: RuntimeDep) -> Response:
    params = await _signed_twilio_form(
        request, runtime, "/v1/inbound/twilio", _twilio_token(runtime)
    )
    phone = (params.get("From") or [""])[0]
    text = (params.get("Body") or [""])[0]
    provenance = {"method": "sms_reply", "provider": "twilio"}
    if sid := (params.get("MessageSid") or [""])[0]:
        provenance["message_id"] = sid
    await inbound.handle(session, runtime, phone, text, provenance)
    return Response(EMPTY_TWIML, media_type="application/xml")


@router.post("/twilio/status", summary="Twilio delivery status callback", include_in_schema=False)
async def twilio_status(request: Request, session: SessionDep, runtime: RuntimeDep) -> Response:
    """Set the Twilio channel's `status_callback` to `<public_url>/v1/inbound/twilio/status`.
    Delivered texts are marked delivered; numbers that cannot receive texts are disabled."""
    params = await _signed_twilio_form(request, runtime, "/v1/inbound/twilio/status", None)
    fields = {name: values[0] for name, values in params.items() if values}
    report = delivery_feedback(fields)
    if report is not None and fields.get("MessageSid"):
        kind, reason = report
        channel, _ = _twilio_channel(runtime, fields.get("AccountSid", ""))
        await feedback.apply(
            session,
            runtime,
            feedback.FeedbackIn(
                type=kind,
                channel=channel,
                address=fields.get("To") or None,
                provider_message_id=fields["MessageSid"],
                reason=reason,
            ),
            {"source": "twilio"},
        )
    return Response(status_code=204)
