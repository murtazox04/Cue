"""The channel plugin contract.

A channel provider turns rendered :class:`~cue_notify.core.content.Content` into a call to
an external service. Providers are discovered through the ``cue.channels`` entry-point
group, so third-party packages can add channels without touching Cue::

    # pyproject.toml of your package
    [project.entry-points."cue.channels"]
    whatsapp = "my_package.whatsapp:WhatsAppChannel"

Errors are classified so the delivery engine can react correctly:

* :class:`InvalidAddressError` — the address is dead (unregistered token, blocked bot);
  it is disabled and the next address/channel is tried.
* :class:`DeliveryError` with ``retryable=True`` — transient (timeouts, 5xx, rate limits);
  the delivery job is retried with backoff.
* :class:`DeliveryError` with ``retryable=False`` — permanent for this message; the next
  channel is tried.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from cue_notify.core.content import Content


class ChannelConfig(BaseModel):
    """Base class for provider configuration models."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class Delivery:
    """Everything a provider needs to send one message to one address."""

    message_id: UUID
    address: str
    content: Content
    locale: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    """Cue identifiers (message id, tracking token) for client-side engagement tracking."""


@dataclass(frozen=True, slots=True)
class Receipt:
    provider_message_id: str | None = None


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class InvalidAddressError(DeliveryError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class Channel[ConfigT: ChannelConfig](ABC):
    """Base class for providers. Subclasses set ``provider`` and ``config_model``."""

    provider: ClassVar[str]
    config_model: ClassVar[type[ChannelConfig]]

    def __init__(self, name: str, config: ConfigT, http: httpx.AsyncClient) -> None:
        self.name = name
        self.config = config
        self.http = http

    @abstractmethod
    async def send(self, delivery: Delivery) -> Receipt: ...

    async def aclose(self) -> None:  # noqa: B027 — optional hook
        """Release provider resources. The shared HTTP client is closed by the runtime."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


def raise_for_http(
    response: httpx.Response, *, invalid_address: bool = False, include_body: bool = True
) -> None:
    """Map an HTTP response to the delivery error taxonomy.

    ``invalid_address`` marks the caller's knowledge that this 4xx means the address is dead.
    ``include_body=False`` keeps untrusted response bodies out of stored error messages.
    """
    if response.is_success:
        return
    detail = f"HTTP {response.status_code}"
    if include_body:
        detail += f": {response.text[:300]}"
    if response.status_code == 429 or response.status_code >= 500:
        retry_after = _retry_after(response)
        raise DeliveryError(detail, retryable=True, retry_after=retry_after)
    if invalid_address:
        raise InvalidAddressError(detail)
    raise DeliveryError(detail)


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        return None


def transport_error(exc: httpx.HTTPError) -> DeliveryError:
    """Network failures (timeouts, resets) are always worth retrying."""
    return DeliveryError(f"{type(exc).__name__}: {exc}", retryable=True)


def flatten_data(values: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in values.items() if value is not None}
