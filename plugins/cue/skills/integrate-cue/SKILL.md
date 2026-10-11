---
name: integrate-cue
description: Add Cue to an application so it decides whether, what, when and where to notify people (push, e-mail, SMS, Telegram, webhooks), with preferences, unsubscribe links, digests, quiet hours and limits for AI agents. Use when the user wants to add or improve notifications, a notification preference page or one-click unsubscribe, batching/digests, or a safe way for an AI agent to notify people.
---

# Integrate Cue

Cue sits between the app and people. The app reports **events** ("order.shipped");
Cue's rules, templates and policy decide what, if anything, to send, and hand finished
messages to the delivery the app already has (a signed webhook) or to a bundled
connector (FCM, Twilio, SMTP, Telegram). Cue does not replace the app's sender.

Docs: https://murtazox04.github.io/Cue · LLM index: https://murtazox04.github.io/Cue/llms.txt

## 1. Run Cue

Local development (SQLite, embedded worker, console delivery):

```bash
export CUE_WORKER__EMBEDDED=true CUE_CHANNELS__PUSH__PROVIDER=console
uvx cue-notify serve --migrate                 # API on http://127.0.0.1:8000, docs at /docs
uvx cue-notify keys create backend -s admin    # prints the key once: ck_…
```

Production: PostgreSQL (`CUE_DATABASE__URL=postgresql+asyncpg://…`), `cuectl serve` and a
separate `cuectl worker`; the repository's `compose.yaml` and Docker image show the setup.
Put secrets in environment variables, structure in `cue.toml`.

## 2. Connect delivery

Prefer pointing a route at the app's existing sender:

```toml
[channels.email]
provider = "webhook"
url = "https://app.example.com/internal/cue/email"   # secret via CUE_CHANNELS__EMAIL__SECRET
```

The app receives `{"id", "channel", "address", "locale", "importance", "expires_at",
"content": {"title", "body", "url", "html", "thread", "actions", "data"}, "metadata",
"links": {"preferences", "unsubscribe"}}`. Verify the `Cue-Signature` header
(`t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">`) and reject old timestamps. Use the
`Idempotency-Key` header (the message id) to avoid double sends. If e-mail is sent by the
app, set `List-Unsubscribe: <links.unsubscribe>` and
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`.

## 3. Model the notifications

All with the admin key, `Authorization: Bearer ck_…`:

1. **Categories** carry policy. `transactional` (no caps, no opt-out) and `marketing`
   exist. Add others, e.g. `{"key": "social", "frequency_caps": [{"period": "P1D",
   "limit": 3}], "fatigue": {"after": 5, "pause": "P7D"}}` → `POST /v1/categories`.
2. **Templates** are localised Jinja2: `POST /v1/templates` with `{"key", "locales":
   {"en": {"title", "body", "url", "thread", "actions"}}}`. Variables: `data`,
   `recipient`, `event`, `category`, `importance`, `links`.
3. **Rules** map events to templates: `POST /v1/rules` with `{"key", "name", "event",
   "template", "category", "channels": ["push", "email"], "condition"?, "importance"?,
   "expires_after_seconds"?, "cooldown_seconds"?, "digest"?}`. Use `digest` (`{"window":
   "PT10M", "key": "{{ event.data.post_id }}"}`) for bursty events like comments, or
   `{"deliver_at": "08:00"}` for a daily summary in each person's time zone.

Check a rule before relying on it: `POST /v1/rules/evaluate` with an example event.

## 4. Call Cue from the app

Prefer the official clients when the app is Python (`pip install cue-notify-client`) or
TypeScript/JavaScript (`npm install cue-client`); they retry safely and verify webhook
signatures (`verify_webhook` / `verifyWebhook`). The raw HTTP calls look like this:

Keep profiles in sync and emit events where things happen. Always send an
`idempotency_key` derived from the business action.

```python
import httpx

cue = httpx.Client(base_url=CUE_URL, headers={"Authorization": f"Bearer {CUE_API_KEY}"})
cue.put(
    f"/v1/recipients/{user.id}",
    json={
        "locale": user.locale,
        "timezone": user.timezone,
        "addresses": [{"channel": "email", "value": user.email}],
    },
)
cue.post(
    "/v1/events",
    json={
        "name": "order.shipped",
        "recipient": str(user.id),
        "data": {"order_id": order.id},
        "idempotency_key": f"order-shipped:{order.id}",
    },
)
```

```ts
await fetch(`${CUE_URL}/v1/events`, {
  method: "POST",
  headers: { Authorization: `Bearer ${CUE_API_KEY}`, "Content-Type": "application/json" },
  body: JSON.stringify({ name: "order.shipped", recipient: userId,
    data: { orderId }, idempotency_key: `order-shipped:${orderId}` }),
});
```

Direct sends (codes, receipts) use `POST /v1/messages` with `template`, `channels`,
`importance` and `expires_at`. Events never fail because of policy; check results with
`GET /v1/events/{id}` and `GET /v1/messages?recipient=…` (`status`, `status_reason`).

## 5. Preferences and unsubscribe

Set `api.public_url` (`CUE_API__PUBLIC_URL`). Then every message carries
`links.preferences` and, for categories people may leave, `links.unsubscribe`; put them
in templates (`{% if links.unsubscribe %}…{% endif %}`). The hosted page needs no work.
For an in-app settings screen, call `GET/PUT /v1/preferences/{token}` with the
recipient's `preference_token` (from `GET /v1/recipients/{id}`).

## 6. AI agents that notify people

Register each agent (`POST /v1/agents` with `frequency_caps`, `max_importance`,
`categories`, `approval`) and create its key with `"agent": "<key>"`. Give that key to
the agent, ideally through the MCP server (`uvx cue-notify mcp` with `CUE_URL` and
`CUE_API_KEY`). Messages matching the approval policy wait in `pending_approval` until
someone calls `POST /v1/messages/{id}/approve` or `/reject`. For agents that write their
own copy, turn on `[screening]` (needs `TYPESAFE_API_KEY`): a decision model holds
messages that pressure, deceive or leak secrets, and lowers overstated importance.

## Rules of thumb

- Use `transactional` only for messages people genuinely cannot do without.
- Pick `importance` honestly: `critical` is for security and safety, and only takes
  effect in categories without opt-out.
- Give anything time-bound an expiry.
- Report engagement (`POST /v1/track/{cue_tracking_token}` from the client, or
  `/v1/messages/{id}/engagement`) before enabling `fatigue`.
- Never point development routes at real people; use the `console` connector.
