"""CRUD and reference integrity for templates, rules, categories and recipients."""

from __future__ import annotations

import httpx

from tests.helpers import create_recipient, create_rule, create_template


async def test_rule_crud(client: httpx.AsyncClient) -> None:
    await create_template(client)
    rule = await create_rule(client)
    assert rule["template"] == "order-shipped"
    assert rule["channels"] == ["push", "sms"]

    updated = await client.patch(
        "/v1/rules/notify-shipped", json={"enabled": False, "channels": ["sms"], "priority": 3}
    )
    assert updated.status_code == 200
    assert (updated.json()["enabled"], updated.json()["channels"]) == (False, ["sms"])

    assert (
        await client.patch("/v1/rules/notify-shipped", json={"template": "nope"})
    ).status_code == 404
    assert (
        await client.patch("/v1/rules/notify-shipped", json={"channels": ["fax"]})
    ).status_code == 422
    assert [r["key"] for r in (await client.get("/v1/rules")).json()] == ["notify-shipped"]
    assert (
        await client.post("/v1/rules", json={**rule, "key": "notify-shipped"})
    ).status_code == 409
    assert (await client.delete("/v1/rules/notify-shipped")).status_code == 204
    assert (await client.get("/v1/rules/notify-shipped")).status_code == 404


async def test_referenced_template_and_category_cannot_be_deleted(
    client: httpx.AsyncClient,
) -> None:
    await create_template(client)
    await client.post("/v1/categories", json={"key": "alerts"})
    await create_rule(client, category="alerts")
    assert (await client.delete("/v1/templates/order-shipped")).status_code == 409
    assert (await client.delete("/v1/categories/alerts")).status_code == 409
    await client.delete("/v1/rules/notify-shipped")
    assert (await client.delete("/v1/templates/order-shipped")).status_code == 204
    assert (await client.delete("/v1/categories/alerts")).status_code == 204


async def test_category_crud(client: httpx.AsyncClient) -> None:
    keys = [c["key"] for c in (await client.get("/v1/categories")).json()]
    assert keys == ["marketing", "transactional"]
    marketing = (await client.get("/v1/categories/marketing")).json()
    assert marketing["frequency_caps"] == [
        {"period": "P1D", "limit": 2},
        {"period": "P7D", "limit": 5},
    ]
    patched = await client.patch(
        "/v1/categories/marketing", json={"frequency_caps": [{"period": 3600, "limit": 1}]}
    )
    assert patched.json()["frequency_caps"] == [{"period": "PT1H", "limit": 1}]
    assert (await client.post("/v1/categories", json={"key": "marketing"})).status_code == 409
    assert (await client.post("/v1/categories", json={"key": "Bad Key"})).status_code == 422


async def test_recipient_addresses_and_listing(client: httpx.AsyncClient) -> None:
    await create_recipient(
        client, "a", addresses=[{"channel": "webhook", "value": "https://x.test/a/b"}]
    )
    await create_recipient(client, "b", locale="ru", attributes={"plan": "pro"})
    merged = await client.put("/v1/recipients/b", json={"attributes": {"seats": 3}})
    assert merged.json()["attributes"] == {"plan": "pro", "seats": 3}
    assert merged.json()["locale"] == "ru"  # omitted fields are untouched

    removed = await client.delete("/v1/recipients/a/addresses/webhook/https://x.test/a/b")
    assert removed.status_code == 204
    assert (await client.get("/v1/recipients/a")).json()["addresses"] == []
    missing = await client.delete("/v1/recipients/a/addresses/webhook/https://x.test/a/b")
    assert missing.status_code == 404

    page = (await client.get("/v1/recipients", params={"limit": 1})).json()
    assert len(page["items"]) == 1
    assert page["next_cursor"] is not None


async def test_events_listing_filters(client: httpx.AsyncClient) -> None:
    for name in ("signup", "login", "login"):
        await client.post("/v1/events", json={"name": name, "recipient": "u1"})
    logins = (await client.get("/v1/events", params={"name": "login", "recipient": "u1"})).json()
    assert len(logins["items"]) == 2
    assert all(e["source"] == "tests" for e in logins["items"])
    assert (await client.get("/v1/events", params={"recipient": "ghost"})).status_code == 404
    bad = await client.post("/v1/events", json={"name": "has spaces", "recipient": "u1"})
    assert bad.status_code == 422
    assert bad.json()["errors"][0]["loc"] == ["body", "name"]
