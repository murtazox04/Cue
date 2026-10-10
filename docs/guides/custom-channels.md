# Custom channels

A channel provider is a class with a config model and one `send` method. Package it and
register it under the `cue.channels` entry point; Cue discovers it at start-up.

```python
# my_cue_whatsapp/channel.py
from typing import ClassVar

import httpx
from pydantic import SecretStr

from cue_notify.channels import (
    Channel,
    ChannelConfig,
    Delivery,
    InvalidAddressError,
    Receipt,
    raise_for_http,
    transport_error,
)


class WhatsAppConfig(ChannelConfig):
    phone_number_id: str
    access_token: SecretStr


class WhatsAppChannel(Channel[WhatsAppConfig]):
    provider: ClassVar[str] = "whatsapp"
    config_model: ClassVar[type[ChannelConfig]] = WhatsAppConfig

    async def send(self, delivery: Delivery) -> Receipt:
        try:
            response = await self.http.post(
                f"https://graph.facebook.com/v21.0/{self.config.phone_number_id}/messages",
                headers={"Authorization": f"Bearer {self.config.access_token.get_secret_value()}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": delivery.address,
                    "type": "text",
                    "text": {"body": delivery.content.body},
                },
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc  # network trouble: retryable
        if response.status_code == 400 and "recipient" in response.text:
            raise InvalidAddressError(response.text)
        raise_for_http(response)  # 429/5xx → retryable, other errors → permanent
        return Receipt(provider_message_id=response.json()["messages"][0]["id"])
```

```toml
# your package's pyproject.toml
[project.entry-points."cue.channels"]
whatsapp = "my_cue_whatsapp.channel:WhatsAppChannel"
```

```toml
# cue.toml
[channels.whatsapp]
provider = "whatsapp"
phone_number_id = "1234567890"
```

## The contract

- `send` handles **one address**. Cue fans out over a recipient's addresses and
  channels, retries and records results.
- Use the shared `self.http` client (connection pooling, sane timeouts).
- Classify failures (`raise_for_http` and `transport_error` cover the common HTTP cases):
    - `InvalidAddressError` — the address will never work; it gets disabled.
    - `DeliveryError(retryable=True, retry_after=…)` — transient; the job is retried.
    - `DeliveryError(retryable=False)` — permanent for this message; the next channel is
      tried.
- Return a `Receipt` with the provider's message id when there is one.
- `delivery.metadata` carries `cue_message_id` and `cue_tracking_token`; include
  them in the payload if your channel can carry data to a client app.
- `delivery.links` carries `preferences` and `unsubscribe` URLs when configured; an
  e-mail connector should turn `unsubscribe` into `List-Unsubscribe` headers.
- Override `aclose()` if you hold resources other than the shared HTTP client.

Test providers with `httpx.MockTransport` — see `tests/unit/test_channels.py`.
