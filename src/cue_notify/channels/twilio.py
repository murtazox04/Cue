"""SMS through Twilio's Messages API. The address is an E.164 phone number."""

from __future__ import annotations

from typing import ClassVar, Self

import httpx
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator

from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    InvalidAddressError,
    Receipt,
    transport_error,
)

API = "https://api.twilio.com/2010-04-01"

# https://www.twilio.com/docs/api/errors — codes meaning "this number will never work".
DEAD_NUMBER_CODES = frozenset({21211, 21214, 21217, 21407, 21421, 21610, 21612, 21614})


class TwilioConfig(ChannelConfig):
    account_sid: str
    auth_token: SecretStr
    from_number: str | None = Field(default=None, description="Sender number in E.164.")
    messaging_service_sid: str | None = Field(default=None, description="Alternative to from.")
    status_callback: AnyHttpUrl | None = None
    include_title: bool = Field(default=False, description="Prefix the body with the title.")

    @model_validator(mode="after")
    def _sender(self) -> Self:
        if not (self.from_number or self.messaging_service_sid):
            raise ValueError("set either from_number or messaging_service_sid")
        return self


class TwilioChannel(Channel[TwilioConfig]):
    provider: ClassVar[str] = "twilio"
    config_model: ClassVar[type[ChannelConfig]] = TwilioConfig

    async def send(self, delivery: Delivery) -> Receipt:
        content = delivery.content
        body = content.body
        if self.config.include_title and content.title:
            body = f"{content.title}\n{body}"
        if content.url:
            body = f"{body}\n{content.url}"
        form = {"To": delivery.address, "Body": body}
        if self.config.messaging_service_sid:
            form["MessagingServiceSid"] = self.config.messaging_service_sid
        else:
            form["From"] = str(self.config.from_number)
        if self.config.status_callback:
            form["StatusCallback"] = str(self.config.status_callback)

        url = f"{API}/Accounts/{self.config.account_sid}/Messages.json"
        auth = (self.config.account_sid, self.config.auth_token.get_secret_value())
        try:
            response = await self.http.post(url, data=form, auth=auth)
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc

        if response.is_success:
            return Receipt(provider_message_id=response.json().get("sid"))
        error = response.json() if response.content else {}
        detail = f"twilio {error.get('code')}: {error.get('message', response.text[:300])}"
        if response.status_code == 429 or response.status_code >= 500:
            raise DeliveryError(detail, retryable=True)
        if error.get("code") in DEAD_NUMBER_CODES:
            raise InvalidAddressError(detail)
        raise DeliveryError(detail)
