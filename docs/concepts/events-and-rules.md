# Events and rules

## Events

An event is a fact about a recipient: `order.shipped`, `payment.failed`,
`user.signed_up`. Send events from your backend with `POST /v1/events`:

| Field | Required | Description |
|---|---|---|
| `name` | yes | Letters, digits and `._-:/`. Dots namespace nicely: `billing.invoice.overdue`. |
| `recipient` | yes | An external id (`"user-42"`) or a profile to create/update inline. |
| `data` | no | Any JSON object. Available to rule conditions and templates. |
| `occurred_at` | no | When it happened (defaults to receipt time). |
| `idempotency_key` | recommended | Retries with the same key are ignored. |
| `source` | no | The producing system (`billing`, `crm`…). Idempotency keys are unique per source. Defaults to the API key's name. |

Ingestion is fast and durable: the event and its processing job are written in one
transaction, then evaluated asynchronously by the worker. The recipient is created
automatically if it does not exist yet. An inline profile may set `locale`, `timezone`
and `attributes`; adding `addresses` inline additionally requires the
`recipients:write` scope, because addresses decide where messages are delivered.

If an event cannot be processed even after retries, it is marked `failed` with the
reason in `error`.

`GET /v1/events/{id}` returns the event with its `outcome` — one entry per rule that was
considered:

```json
[
  {"rule": "vip-sms", "matched": false, "reason": "condition_false"},
  {"rule": "default-push", "matched": true, "status": "queued", "message_id": "…"}
]
```

## Rules

A rule connects an event to a template:

```json
{
  "key": "large-payment-alert",
  "name": "Alert on large payments",
  "event": "payment.*",
  "condition": {">": [{"var": "event.data.amount"}, 1000000]},
  "template": "large-payment",
  "category": "transactional",
  "channels": ["push", "sms"],
  "priority": 10,
  "stop_processing": true,
  "delay_seconds": 0,
  "cooldown_seconds": 600,
  "rollout_percent": 100
}
```

| Field | Meaning |
|---|---|
| `event` | Event name, with shell-style wildcards (`payment.*`, `*`). |
| `condition` | [JSON Logic](https://jsonlogic.com) over `{event, recipient}`. `null` always matches. |
| `channels` | Channels to try, in fallback order. |
| `priority` | Higher is evaluated first. |
| `stop_processing` | When this rule matches, lower-priority rules are skipped (default `true`). Set `false` to let several rules fire for one event. |
| `delay_seconds` | Wait before sending (e.g. "remind me in an hour"). |
| `cooldown_seconds` | At most one message from this rule per recipient per window. |
| `importance` | `low`, `normal` (default), `high` or `critical` — see [importance and expiry](importance.md). |
| `expires_after_seconds` | Drop the message if it cannot be delivered within this time. |
| `digest` | Group matching events into one message per window instead of one per event — see [digests](digests.md). |
| `rollout_percent` | Deterministic percentage rollout; the same recipient always gets the same answer, and raising the percentage only adds people. |
| `enabled` | Disable without deleting. |

### Writing conditions

The condition sees this document:

```json
{
  "event": {"name": "payment.received", "source": "billing", "data": {…}, "occurred_at": "…"},
  "recipient": {"external_id": "u1", "locale": "en", "timezone": "Europe/Berlin", "attributes": {…}}
}
```

Some patterns:

```json
{"and": [
  {">=": [{"var": "event.data.amount"}, 100]},
  {"in": [{"var": "recipient.attributes.plan"}, ["pro", "enterprise"]]}
]}
```

```json
{"!": {"var": "recipient.attributes.onboarded"}}
```

```json
{"some": [{"var": "event.data.items"}, {"==": [{"var": "category"}, "electronics"]}]}
```

All standard JSON Logic operators are supported (`var`, `missing`, `if`, comparison,
arithmetic, `map`/`filter`/`reduce`/`all`/`some`/`none`, `in`, `cat`, `substr`, …), plus
`starts_with`, `ends_with`, `lower` and `upper`. Conditions are validated when saved, and
are evaluated safely — no code execution, bounded nesting.

### Dry runs

`POST /v1/rules/evaluate` evaluates rules **and delivery policy** for a hypothetical
event without storing or sending anything. Use it in CI for your rule definitions, or to
answer "would this user get that?". Digest rules report `"reason": "digested"`.
