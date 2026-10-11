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
from cue_notify.services import agents, inbound
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
    payload = url + "".join(key + value for key in sorted(params) for value in params[key])
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _twilio_token(runtime: RuntimeDep) -> str:
    for name in runtime.settings.compliance.sms_channels:
        channel = runtime.channels.get(name) if name in runtime.channels else None
        if channel is not None and channel.provider == "twilio":
            token: Any = channel.config.auth_token  # pyright: ignore[reportAttributeAccessIssue]
            return str(token.get_secret_value())
    raise NotFoundError("Twilio channel", "in compliance.sms_channels")


@router.post("/twilio", summary="Twilio incoming-message webhook", include_in_schema=False)
async def inbound_twilio(request: Request, session: SessionDep, runtime: RuntimeDep) -> Response:
    public_url = runtime.settings.api.public_url
    if public_url is None:
        raise UnprocessableError("set api.public_url so Twilio's signature can be checked")
    params = await form_fields(request)
    url = f"{str(public_url).rstrip('/')}/v1/inbound/twilio"
    expected = twilio_signature(_twilio_token(runtime), url, params)
    if not hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")):
        raise ForbiddenError("invalid Twilio signature")
    phone = (params.get("From") or [""])[0]
    text = (params.get("Body") or [""])[0]
    provenance = {"method": "sms_reply", "provider": "twilio"}
    if sid := (params.get("MessageSid") or [""])[0]:
        provenance["message_id"] = sid
    await inbound.handle(session, runtime, phone, text, provenance)
    return Response(EMPTY_TWIML, media_type="application/xml")
