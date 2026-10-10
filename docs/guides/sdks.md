# SDKs

Cue is a plain HTTP API (`/openapi.json` on any instance generates a client for any
language). Two small official clients cover the common cases and, more importantly,
verify the signature on webhooks Cue sends to your service.

| Language | Package | Dependencies | Source |
|---|---|---|---|
| Python 3.10+ | `cue-notify-client` (import `cue_client`) | `httpx` | [`sdks/python`](https://github.com/murtazox04/Cue/tree/main/sdks/python) |
| TypeScript / JavaScript | `cue-notify-client` | none (Node 18+, Deno, Bun, edge runtimes) | [`sdks/typescript`](https://github.com/murtazox04/Cue/tree/main/sdks/typescript) |

Both offer the same operations:

| Operation | Python | TypeScript |
|---|---|---|
| Report an event | `send_event(name, recipient, data, idempotency_key=)` | `sendEvent({ name, recipient, data, idempotencyKey })` |
| Send a template directly | `send_message(recipient, template, channels, data, importance=, expires_at=)` | `sendMessage({ … })` |
| Create or update a profile | `upsert_recipient(id, timezone=, addresses=)` | `upsertRecipient(id, { … })` |
| Read messages | `get_message(id)`, `list_messages(**filters)` | `getMessage(id)`, `listMessages(filters)` |
| Verify a webhook | `verify_webhook(body, header, secret)` | `await verifyWebhook(body, header, secret)` |

## Behaviour

- **Errors.** Error responses raise `CueError` carrying Cue's
  [problem details](../reference/api.md): `status`, `title`, `detail` and `errors`.
- **Retries.** HTTP 429, 502, 503 and 504 are retried with backoff (honouring
  `Retry-After`), but only for requests that are safe to repeat: reads, profile updates,
  and sends that carry an idempotency key. A send without a key is never retried, because
  a retry could notify someone twice.
- **Path safety.** Ids are escaped in paths, so a value like `../api-keys` cannot reach
  another endpoint.
- **Async.** Python has `AsyncCue` with the same methods.

## Verifying webhooks

```python
from cue_client import InvalidSignatureError, verify_webhook

try:
    verify_webhook(raw_body, headers.get("Cue-Signature"), WEBHOOK_SECRET)
except InvalidSignatureError:
    ...  # respond 401
```

Pass the raw body bytes exactly as received. The signature is
`t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">`, and requests signed more
than five minutes ago are rejected, so a captured request cannot be replayed later.
Both SDKs are tested against signatures produced by the server.
