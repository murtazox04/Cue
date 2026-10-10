# HTTP API

The complete, always-current reference is generated from the code: open
**`/docs`** (Swagger UI) or **`/redoc`** on a running instance, or fetch
`/openapi.json` to generate a client.

## Conventions

- **Authentication:** `Authorization: Bearer ck_…`. Keys carry [scopes](cli.md#api-key-scopes).
- **Errors** use [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem details
  (`application/problem+json`) with `title`, `status`, `detail` and, for validation
  errors, an `errors` list.
- **Pagination:** list endpoints take `limit` (≤ 500) and `cursor`, and return
  `{"items": [...], "next_cursor": "…" | null}`. Results are newest first.
- **Idempotency:** events take `idempotency_key`; direct sends take `idempotency_key`.
  A repeat returns `200` with the original resource instead of `202`.
- **Timestamps** are ISO 8601 in UTC.

## Endpoints

| Method & path | Scope | |
|---|---|---|
| `POST /v1/events` | `events:write` | Ingest an event. |
| `GET /v1/events`, `GET /v1/events/{id}` | `messages:read` | Events with per-rule outcomes. |
| `POST /v1/messages` | `messages:write` | Send a template directly. |
| `GET /v1/messages`, `GET /v1/messages/{id}` | `messages:read` | Filter by `recipient`, `status`, `event_id`, `broadcast_id`. |
| `POST /v1/messages/{id}/cancel` | `messages:write` | Cancel a queued message. |
| `GET /v1/digests`, `GET /v1/digests/{id}` | `messages:read` | Grouped events; filter by `recipient`, `status`. |
| `POST /v1/digests/{id}/send` | `messages:write` | Close a digest's window early. |
| `POST /v1/messages/{id}/engagement` | `events:write` | Record delivered/opened/clicked/converted. |
| `POST /v1/track/{token}` | none | Engagement from client apps. |
| `PUT /v1/recipients/{external_id}` | `recipients:write` | Create or update. |
| `GET /v1/recipients`, `GET /v1/recipients/{external_id}` | `recipients:read` | |
| `DELETE /v1/recipients/{external_id}` | `recipients:write` | Erase profile and history. |
| `POST /v1/recipients/{id}/addresses` | `recipients:write` | Add a device, number, e-mail… |
| `DELETE /v1/recipients/{id}/addresses/{channel}/{value}` | `recipients:write` | |
| `PUT /v1/recipients/{id}/preferences` | `recipients:write` | Unsubscribes, muted channels, quiet hours. |
| `GET /v1/recipients/{id}/preferences/history` | `recipients:read` | Consent ledger. |
| `POST /v1/recipients/{id}/preference-token` | `recipients:write` | Rotate the preference token. |
| `GET`, `PUT /v1/preferences/{token}` | the token | Self-service preferences for your own UI. |
| `GET`, `POST /preferences/{token}[/unsubscribe]` | the token | Hosted page and RFC 8058 one-click unsubscribe. |
| `/v1/templates[/{key}]`, `POST …/{key}/preview` | `admin` | |
| `/v1/rules[/{key}]`, `POST /v1/rules/evaluate` | `admin` | |
| `/v1/categories[/{key}]` | `admin` | |
| `/v1/broadcasts[/{id}]`, `…/start`, `…/pause`, `…/resume`, `…/cancel` | `admin` | |
| `/v1/api-keys`, `…/{id}/revoke` | `admin` | |
| `GET /healthz`, `GET /readyz` | none | Probes. |

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
