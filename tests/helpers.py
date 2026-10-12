"""Test doubles and builders shared by the test suite."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx
from pydantic import SecretStr
from typer.testing import CliRunner

from cue_notify.ai import Copy, GenerationError, PersonalizationRequest, PersonalizationResult
from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    InvalidAddressError,
    Receipt,
)
from cue_notify.cli import app
from cue_notify.jobs import create_worker
from cue_notify.runtime import Runtime

TEST_SECRET_KEY = SecretStr("cue-test-secret-key-0123456789abcdef")

# --------------------------------------------------------------------------- fake channel


class FakeConfig(ChannelConfig):
    pass


@dataclass
class FakeChannel(Channel[FakeConfig]):
    """Records deliveries; ``failures[address]`` makes sends to that address raise."""

    provider: ClassVar[str] = "fake"
    config_model: ClassVar[type[ChannelConfig]] = FakeConfig

    name: str = "fake"
    sent: list[Delivery] = field(default_factory=list)
    failures: dict[str, Exception] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.config = FakeConfig()

    async def send(self, delivery: Delivery) -> Receipt:
        failure = self.failures.get(delivery.address)
        if failure is not None:
            raise failure
        self.sent.append(delivery)
        return Receipt(provider_message_id=f"{self.name}-{len(self.sent)}")

    def fail(self, address: str, *, retryable: bool = False, invalid: bool = False) -> None:
        self.failures[address] = (
            InvalidAddressError("gone") if invalid else DeliveryError("boom", retryable=retryable)
        )


async def drain(runtime: Runtime) -> int:
    """Run every job that is due now."""
    return await create_worker(runtime, name="test").run_until_idle()


async def run_cli(runner: CliRunner, *args: str) -> str:
    result = await asyncio.to_thread(runner.invoke, app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


# --------------------------------------------------------------------------- fake AI


@dataclass
class FakeGenerator:
    fail: bool = False
    requests: list[PersonalizationRequest] = field(default_factory=list)

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def personalize(self, request: PersonalizationRequest) -> PersonalizationResult:
        self.requests.append(request)
        if self.fail:
            raise GenerationError("provider down")
        name = request.context.get("first_name", "there")
        copy = Copy(title=request.baseline.title, body=f"{name}, {request.baseline.body}")
        return PersonalizationResult(copy=copy, model=self.model_name, tokens=120)

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- builders


ORDER_TEMPLATE: dict[str, Any] = {
    "key": "order-shipped",
    "default_locale": "en",
    "locales": {
        "en": {
            "title": "Order shipped",
            "body": "Order {{ data.order_id }} is on its way.",
            "url": "https://shop.test/orders/{{ data.order_id }}",
            "channels": {"sms": {"body": "Order {{ data.order_id }} shipped."}},
        },
        "uz": {
            "title": "Buyurtma yo'lda",
            "body": "{{ data.order_id }} buyurtmangiz yo'lga chiqdi.",
        },
    },
}


async def create_template(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post("/v1/templates", json={**ORDER_TEMPLATE, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


async def create_rule(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "key": "notify-shipped",
        "name": "Notify when an order ships",
        "event": "order.shipped",
        "template": "order-shipped",
        "category": "transactional",
        "channels": ["push", "sms"],
        **overrides,
    }
    response = await client.post("/v1/rules", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def create_recipient(
    client: httpx.AsyncClient,
    external_id: str = "user-1",
    addresses: list[dict[str, str]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    body = {
        "addresses": addresses
        if addresses is not None
        else [{"channel": "push", "value": "token-1"}, {"channel": "sms", "value": "+15550001"}],
        **fields,
    }
    response = await client.put(f"/v1/recipients/{external_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def send_event(
    client: httpx.AsyncClient, name: str = "order.shipped", recipient: Any = "user-1", **fields: Any
) -> httpx.Response:
    body = {"name": name, "recipient": recipient, "data": {"order_id": "A-1"}, **fields}
    return await client.post("/v1/events", json=body)
