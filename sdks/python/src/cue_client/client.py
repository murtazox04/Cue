"""Synchronous and asynchronous clients for Cue's HTTP API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx

from cue_client import __version__

Importance = Literal["low", "normal", "high", "critical"]
JSON = dict[str, Any]

RETRY_STATUSES = frozenset({429, 502, 503, 504})


class CueError(Exception):
    """An error response from Cue (an RFC 9457 problem document)."""

    def __init__(self, status: int, title: str, detail: str, errors: list[Any] | None) -> None:
        super().__init__(f"{status} {title}: {detail}")
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors or []


def _raise_for_problem(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        problem: Mapping[str, Any] = response.json()
    except ValueError:
        problem = {}
    raise CueError(
        response.status_code,
        str(problem.get("title", response.reason_phrase)),
        str(problem.get("detail", response.text[:300])),
        problem.get("errors"),
    )


def _drop_none(values: Mapping[str, Any]) -> JSON:
    return {key: value for key, value in values.items() if value is not None}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _event(
    name: str,
    recipient: str | Mapping[str, Any],
    data: Mapping[str, Any] | None,
    idempotency_key: str | None,
    occurred_at: datetime | None,
) -> JSON:
    return _drop_none(
        {
            "name": name,
            "recipient": recipient,
            "data": dict(data or {}),
            "idempotency_key": idempotency_key,
            "occurred_at": _iso(occurred_at),
        }
    )


def _message(
    recipient: str,
    template: str,
    channels: list[str],
    data: Mapping[str, Any] | None,
    category: str,
    importance: Importance,
    expires_at: datetime | None,
    send_at: datetime | None,
    idempotency_key: str | None,
) -> JSON:
    return _drop_none(
        {
            "recipient": recipient,
            "template": template,
            "channels": channels,
            "data": dict(data or {}),
            "category": category,
            "importance": importance,
            "expires_at": _iso(expires_at),
            "send_at": _iso(send_at),
            "idempotency_key": idempotency_key,
        }
    )


def _profile(
    locale: str | None,
    timezone: str | None,
    attributes: Mapping[str, Any] | None,
    addresses: list[Mapping[str, str]] | None,
) -> JSON:
    return _drop_none(
        {
            "locale": locale,
            "timezone": timezone,
            "attributes": dict(attributes) if attributes is not None else None,
            "addresses": [dict(a) for a in addresses] if addresses is not None else None,
        }
    )


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "User-Agent": f"cue-client-python/{__version__}"}


class Cue:
    """Client for Cue's API.

    ``retries`` applies to rate limiting and gateway errors on requests that are safe to
    repeat: reads, profile updates, and sends that carry an idempotency key.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://127.0.0.1:8000",
        *,
        timeout: float = 10.0,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url, headers=_headers(api_key), timeout=timeout, transport=transport
        )
        self._retries = retries

    def __enter__(self) -> Cue:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, *, safe: bool, **kwargs: Any) -> Any:
        attempts = self._retries + 1 if safe else 1
        response = self._http.request(method, path, **kwargs)
        for attempt in range(1, attempts):
            if response.status_code not in RETRY_STATUSES:
                break
            time.sleep(_backoff(response, attempt))
            response = self._http.request(method, path, **kwargs)
        _raise_for_problem(response)
        return response.json() if response.content else None

    def send_event(
        self,
        name: str,
        recipient: str | Mapping[str, Any],
        data: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> JSON:
        """Report that something happened; Cue's rules decide what to send."""
        body = _event(name, recipient, data, idempotency_key, occurred_at)
        return self._request("POST", "/v1/events", json=body, safe=idempotency_key is not None)

    def send_message(
        self,
        recipient: str,
        template: str,
        channels: list[str],
        data: Mapping[str, Any] | None = None,
        *,
        category: str = "transactional",
        importance: Importance = "normal",
        expires_at: datetime | None = None,
        send_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> JSON:
        """Send a template directly. Check ``status``: it may be ``suppressed``."""
        body = _message(
            recipient,
            template,
            channels,
            data,
            category,
            importance,
            expires_at,
            send_at,
            idempotency_key,
        )
        return self._request("POST", "/v1/messages", json=body, safe=idempotency_key is not None)

    def upsert_recipient(
        self,
        external_id: str,
        *,
        locale: str | None = None,
        timezone: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        addresses: list[Mapping[str, str]] | None = None,
    ) -> JSON:
        body = _profile(locale, timezone, attributes, addresses)
        return self._request("PUT", f"/v1/recipients/{_segment(external_id)}", json=body, safe=True)

    def get_message(self, message_id: str) -> JSON:
        return self._request("GET", f"/v1/messages/{_segment(message_id)}", safe=True)

    def list_messages(self, **filters: str | int) -> JSON:
        """Filters: ``recipient``, ``status``, ``event_id``, ``agent``, ``limit``, ``cursor``."""
        return self._request("GET", "/v1/messages", params=filters, safe=True)


class AsyncCue:
    """The asynchronous twin of :class:`Cue`."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://127.0.0.1:8000",
        *,
        timeout: float = 10.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=_headers(api_key), timeout=timeout, transport=transport
        )
        self._retries = retries

    async def __aenter__(self) -> AsyncCue:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, safe: bool, **kwargs: Any) -> Any:
        attempts = self._retries + 1 if safe else 1
        response = await self._http.request(method, path, **kwargs)
        for attempt in range(1, attempts):
            if response.status_code not in RETRY_STATUSES:
                break
            await asyncio.sleep(_backoff(response, attempt))
            response = await self._http.request(method, path, **kwargs)
        _raise_for_problem(response)
        return response.json() if response.content else None

    async def send_event(
        self,
        name: str,
        recipient: str | Mapping[str, Any],
        data: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> JSON:
        body = _event(name, recipient, data, idempotency_key, occurred_at)
        return await self._request(
            "POST", "/v1/events", json=body, safe=idempotency_key is not None
        )

    async def send_message(
        self,
        recipient: str,
        template: str,
        channels: list[str],
        data: Mapping[str, Any] | None = None,
        *,
        category: str = "transactional",
        importance: Importance = "normal",
        expires_at: datetime | None = None,
        send_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> JSON:
        body = _message(
            recipient,
            template,
            channels,
            data,
            category,
            importance,
            expires_at,
            send_at,
            idempotency_key,
        )
        return await self._request(
            "POST", "/v1/messages", json=body, safe=idempotency_key is not None
        )

    async def upsert_recipient(
        self,
        external_id: str,
        *,
        locale: str | None = None,
        timezone: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        addresses: list[Mapping[str, str]] | None = None,
    ) -> JSON:
        body = _profile(locale, timezone, attributes, addresses)
        return await self._request(
            "PUT", f"/v1/recipients/{_segment(external_id)}", json=body, safe=True
        )

    async def get_message(self, message_id: str) -> JSON:
        return await self._request("GET", f"/v1/messages/{_segment(message_id)}", safe=True)

    async def list_messages(self, **filters: str | int) -> JSON:
        return await self._request("GET", "/v1/messages", params=filters, safe=True)


def _segment(value: str) -> str:
    """A path segment that cannot step into another endpoint."""
    return quote(value, safe="")


def _backoff(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 30.0)
    return min(0.5 * 2 ** (attempt - 1), 8.0)
