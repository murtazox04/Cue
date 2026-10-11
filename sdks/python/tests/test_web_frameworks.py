"""Webhook verification helpers for FastAPI and Django."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest

SECRET = "whsec"
BODY = json.dumps({"id": "m1", "content": {"body": "hi"}}).encode()


def _sign(body: bytes, secret: str = SECRET) -> str:
    t = int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def test_fastapi_dependency_checks_the_signature() -> None:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from cue_client.fastapi import CueWebhook

    app = FastAPI()
    hook = CueWebhook(SECRET)

    @app.post("/hook")
    async def receive(payload: dict[str, Any] = Depends(hook)) -> dict[str, Any]:
        return {"got": payload["id"]}

    client = TestClient(app)
    ok = client.post("/hook", content=BODY, headers={"Cue-Signature": _sign(BODY)})
    assert ok.json() == {"got": "m1"}
    bad = client.post("/hook", content=BODY, headers={"Cue-Signature": _sign(BODY, "other")})
    assert bad.status_code == 401
    assert client.post("/hook", content=BODY).status_code == 401


@pytest.fixture(scope="module")
def django_settings() -> None:
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(DEBUG=False, SECRET_KEY="test", ALLOWED_HOSTS=["*"])
        django.setup()


def test_django_decorator_checks_the_signature(django_settings: None) -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory

    from cue_client.django import cue_webhook

    seen: list[dict[str, Any]] = []

    @cue_webhook(secret=SECRET)
    def view(request: Any, payload: dict[str, Any]) -> HttpResponse:
        seen.append(payload)
        return HttpResponse(status=204)

    factory = RequestFactory()
    signed = factory.post(
        "/hook", data=BODY, content_type="application/json", HTTP_CUE_SIGNATURE=_sign(BODY)
    )
    assert view(signed).status_code == 204
    assert seen == [json.loads(BODY)]
    forged = factory.post(
        "/hook", data=BODY, content_type="application/json", HTTP_CUE_SIGNATURE="t=1,v1=00"
    )
    assert view(forged).status_code == 401
    assert view(factory.get("/hook")).status_code == 405
