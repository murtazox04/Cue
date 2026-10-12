# HTTP API

The complete, always-current reference is generated from the code: open
**`/docs`** (Swagger UI) or **`/redoc`** on a running instance, or fetch
`/openapi.json` to generate a client.

## Conventions

- **Authentication:** `Authorization: Bearer ck_…`. Keys carry [scopes](scopes.md).
- **Errors** use [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem details
  (`application/problem+json`) with `title`, `status`, `detail` and, for validation
  errors, an `errors` list. See [problem responses](statuses.md#problem-responses).
- **Pagination:** list endpoints take `limit` (≤ 500) and `cursor`, and return
  `{"items": [...], "next_cursor": "…" | null}`. Results are newest first.
- **Idempotency:** events take `idempotency_key`; direct sends take `idempotency_key`.
  A repeat returns `200` with the original resource instead of `202`.
- **Timestamps** are ISO 8601 in UTC.
- **Statuses and reasons** of messages, events, digests, broadcasts and approval requests
  are listed in [statuses and reasons](statuses.md).

## Endpoints

| Method & path | Scope | |
|---|---|---|
| `POST /v1/events` | `events:write` | Ingest an event. Inline `region`, `attributes`, `addresses`, or a changed `locale`/`timezone`, also need `recipients:write`. |
| `GET /v1/events`, `GET /v1/events/{id}` | `messages:read` | Events with per-rule outcomes. |
| `POST /v1/messages` | `messages:write` | Send a template directly. |
| `GET /v1/messages`, `GET /v1/messages/{id}` | `messages:read` | Filter by `recipient`, `status`, `event_id`, `broadcast_id`, `agent`. |
| `POST /v1/messages/{id}/approve`, `…/reject` | `approvals:decide` | Review a message an agent sent that is `pending_approval`. |
| `POST /v1/messages/{id}/cancel` | `messages:write` | Cancel a queued message. |
| `POST /v1/messages/{id}/engagement` | `events:write` | Record delivered/opened/clicked/converted. |
| `POST /v1/track/{tracking_token}` | none (the token) | Engagement from client apps. |
| `POST /v1/approvals` | `messages:write` | Ask people to approve an action. |
| `GET /v1/approvals`, `GET /v1/approvals/{id}` | `messages:read` | Approval requests and their decisions. |
| `POST /v1/approvals/{id}/approve`, `…/reject` | `approvals:decide` | Decide an approval request. |
| `GET /v1/digests`, `GET /v1/digests/{id}` | `messages:read` | Grouped events; filter by `recipient`, `status`. |
| `POST /v1/digests/{id}/send` | `messages:write` | Close a digest's window early. |
| `PUT /v1/recipients/{external_id}` | `recipients:write` | Create or update. |
| `GET /v1/recipients`, `GET /v1/recipients/{external_id}` | `recipients:read` | |
| `DELETE /v1/recipients/{external_id}` | `recipients:write` | Erase profile and history. |
| `POST /v1/recipients/{id}/addresses` | `recipients:write` | Add a device, number, e-mail…; re-enables a disabled address. |
| `DELETE /v1/recipients/{id}/addresses/{channel}/{value}` | `recipients:write` | |
| `PUT /v1/recipients/{id}/preferences` | `recipients:write` | Unsubscribes, muted channels, quiet hours. |
| `POST /v1/recipients/{id}/consent` | `recipients:write` | Record an opt-in with its disclosure; see [compliance](../guides/compliance.md). |
| `GET /v1/recipients/{id}/preferences/history` | `recipients:read` | Consent ledger. |
| `POST /v1/recipients/{id}/preference-token` | `recipients:write` | Rotate the preference token. |
| `GET`, `PUT /v1/preferences/{token}` | none (the token) | Self-service preferences for your own UI. |
| `POST /v1/inbound/sms` | `recipients:write` | STOP/START replies from any SMS provider (not for agent keys). |
| `POST /v1/inbound/twilio` | none (Twilio signature) | Twilio's incoming-message webhook; needs `api.public_url`. |
| `POST /v1/inbound/twilio/status` | none (Twilio signature) | Twilio's delivery status callback: [delivery feedback](../concepts/channels.md#delivery-feedback). |
| `POST /v1/inbound/resend/{channel}` | none (Svix signature) | Resend's webhook for a `resend` channel: delivery feedback. |
| `POST /v1/inbound/ses/{channel}` | none (SNS signature, listed topics) | Amazon SNS notifications for an `ses` channel: delivery feedback. |
| `POST /v1/feedback` | `recipients:write` | Report a delivery, bounce, soft bounce or spam complaint (not for agent keys). |
| `/v1/templates[/{key}]`, `POST …/{key}/preview` | `admin` | |
| `/v1/rules[/{key}]`, `POST /v1/rules/evaluate` | `admin` | |
| `/v1/categories[/{key}]` | `admin` | |
| `/v1/broadcasts[/{id}]`, `…/start`, `…/pause`, `…/resume`, `…/cancel` | `admin` | |
| `/v1/agents[/{key}]` | `admin` | Automated senders: budgets, importance ceiling, approval. |
| `/v1/api-keys`, `…/{id}/revoke` | `admin` | Keys may be bound to an agent. |
| `GET`, `POST /preferences/{token}` | none (the token) | Hosted preference page. |
| `GET`, `POST /preferences/{token}/unsubscribe` | none (the token) | Confirmation page and RFC 8058 one-click unsubscribe. |
| `GET`, `POST /review/{token}` | none (the token) | One-time review page for a held message or approval request. |
| `GET /healthz`, `GET /readyz` | none | Liveness and readiness (checks the database). |
| `GET /metrics` | none | Prometheus metrics, only with `metrics = true`; keep it off public ingress. |
| `GET /docs`, `/redoc`, `/openapi.json` | none | Interactive docs and the OpenAPI schema, unless `api.docs = false`. |

The routes without a key are rate limited per client address; see
[`api.rate_limit`](configuration.md#apirate_limit).

## Messages

Direct sends are for messages your code decides to send — one-time codes, receipts,
alerts:

```json
POST /v1/messages
{
  "recipient": "user-42",
  "template": "login-code",
  "category": "transactional",
  "channels": ["sms"],
  "data": {"code": "482913"},
  "idempotency_key": "login-7f3a"
}
```

They pass the same [policy](../concepts/policy.md) as everything else; check the returned
`status`.
