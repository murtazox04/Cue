from __future__ import annotations

import httpx

from cue_notify.core.enums import Scope
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys


async def _key(runtime: Runtime, *scopes: Scope) -> str:
    async with runtime.transaction() as session:
        _, raw = await api_keys.create(session, "scoped", scopes)
    return raw


async def test_missing_and_malformed_keys(client: httpx.AsyncClient) -> None:
    for header in (
        {"Authorization": ""},
        {"Authorization": "Bearer nope"},
        {"Authorization": "Bearer ck_x_y"},
    ):
        response = await client.get("/v1/messages", headers=header)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["title"] == "Unauthorized"


async def test_scopes_are_enforced(client: httpx.AsyncClient, runtime: Runtime) -> None:
    ingest_only = await _key(runtime, Scope.EVENTS_WRITE)
    headers = {"Authorization": f"Bearer {ingest_only}"}
    assert (await client.get("/v1/messages", headers=headers)).status_code == 403
    response = await client.post(
        "/v1/events", headers=headers, json={"name": "signup", "recipient": "u1"}
    )
    assert response.status_code == 202


async def test_revoked_key_stops_working(client: httpx.AsyncClient, runtime: Runtime) -> None:
    created = await client.post("/v1/api-keys", json={"name": "ci", "scopes": ["messages:read"]})
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith(f"ck_{body['prefix']}_")
    headers = {"Authorization": f"Bearer {body['key']}"}
    assert (await client.get("/v1/messages", headers=headers)).status_code == 200
    await client.post(f"/v1/api-keys/{body['id']}/revoke")
    assert (await client.get("/v1/messages", headers=headers)).status_code == 401
    listed = (await client.get("/v1/api-keys")).json()
    assert all("key" not in item for item in listed)


async def test_health_endpoints_are_public(client: httpx.AsyncClient) -> None:
    anonymous = {"Authorization": ""}
    assert (await client.get("/healthz", headers=anonymous)).json()["status"] == "ok"
    ready = (await client.get("/readyz", headers=anonymous)).json()
    assert ready == {"status": "ready", "channels": ["push", "sms"]}


async def test_openapi_documents_security(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert "ApiKey" in schema["components"]["securitySchemes"]
    assert "/v1/events" in schema["paths"]
