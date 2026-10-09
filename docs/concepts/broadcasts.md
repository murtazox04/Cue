# Broadcasts

A broadcast sends one template to an audience: a product announcement, a promotion, an
incident notice.

```json
POST /v1/broadcasts
{
  "name": "Autumn sale",
  "template": "autumn-sale",
  "category": "marketing",
  "channels": ["push", "email"],
  "audience": {"type": "filter", "condition": {"==": [{"var": "recipient.attributes.plan"}, "free"]}},
  "variables": {"discount": 20}
}
```

Audiences:

| `type` | |
|---|---|
| `all` | Every recipient. |
| `recipients` | `external_ids`: an explicit list (unknown ids are skipped). |
| `filter` | `condition`: JSON Logic over `{"recipient": …}`. |

A broadcast is created as a **draft**. `POST /v1/broadcasts/{id}/start` (optionally with
`{"at": "2026-11-01T09:00:00Z"}`) schedules it.

## How it runs

Recipients are processed in batches of 500, each in its own transaction, with a stored
cursor. Every recipient passes through the regular [policy](policy.md) — unsubscribes,
caps and quiet hours apply — and every message carries a dedup key unique to the
broadcast and recipient. Consequently:

- **Pause** (`/pause`) takes effect after the current batch.
- **Resume** (`/resume`) continues from the cursor.
- **Retries and crashes never double-send**: a re-run batch finds the existing messages.
- **Cancel** (`/cancel`) stops the broadcast and withdraws messages not sent yet —
  including ones waiting for quiet hours to end, even after the broadcast completed.

`GET /v1/broadcasts/{id}` reports progress in `stats` (`targeted`, `queued`,
`suppressed`, `failed`…). Starting a scheduled broadcast again moves its start time.
If a batch keeps failing until its retries run out, the broadcast becomes `failed` with
the reason in `error`. Messages are listed with `GET /v1/messages?broadcast_id=…`.
