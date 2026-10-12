# Channels and connectors

Cue decides and prepares; **delivery is yours to plug in**. A **channel** is a named
route — `push`, `sms`, `email`, `ops-alerts`… — and its **connector** (the `provider`
setting) is what actually hands the message over. Rules, broadcasts and recipient
addresses refer to channels by name, so you can change how a route is delivered without
touching any rule.

The most flexible connector is `webhook`: Cue POSTs the fully rendered, signed message to
your service, and your service sends it however you like.

```toml
[channels.push]
provider = "webhook"
url = "https://notifications.internal.example/push"
# secret from CUE_CHANNELS__PUSH__SECRET

[channels.sms]
provider = "twilio"            # or a bundled connector
account_sid = "AC…"
from_number = "+15550100"
```

Equivalent environment variables: `CUE_CHANNELS__PUSH__PROVIDER=webhook`,
`CUE_CHANNELS__PUSH__URL=…`. Configuration is validated at start-up;
`cuectl check` prints the configured channels.

## Addresses

A recipient has any number of addresses per channel — every phone, browser and tablet:

```json
POST /v1/recipients/user-42/addresses
{"channel": "push", "value": "<FCM registration token>", "label": "Pixel 9"}
```

A message goes to **all** enabled addresses of the first channel that works. When a
provider reports an address as permanently invalid (unregistered token, blocked bot,
unreachable number), the address is disabled with a reason and skipped from then on.
Re-adding it with `POST /v1/recipients/{external_id}/addresses` re-enables it; sending the
same value again in a profile update or inline with an event or message keeps it disabled,
so a stale token your app keeps reporting does not come back to life.

## Fallback and retries

For each planned channel, in order:

- **Success** on any address → message `sent`.
- **Transient error** (timeouts, HTTP 429/5xx) → the delivery job is retried with
  exponential backoff (honouring `Retry-After`). On the last attempt Cue falls back
  to the next channel instead.
- **Permanent error** → the next channel is tried immediately.
- **Unhealthy channel** → after `delivery.breaker_failures` (5) transient failures in a
  row, a channel is skipped for `delivery.breaker_cooldown_seconds` (60) by messages
  that have another channel planned after it, so a provider outage does not hold every
  message back. A channel that is a message's only route is always tried. Each worker
  process keeps its own count.

If no channel succeeds the message is `failed` with the reasons in `status_reason`.

## Delivery feedback

What a provider learns after accepting a message (it bounced, it was marked as spam, it
arrived) comes back to Cue, which acts on it so nobody has to clean lists by hand:

| Report | Effect |
|---|---|
| `delivered` | The message becomes `delivered`; the address's soft-bounce count resets. |
| `bounce` | The address is disabled (`disabled_reason: "bounced: …"`); a `sent` message becomes `failed`. |
| `soft_bounce` | Counted; `delivery.soft_bounce_limit` (3) in a row, with no delivery between, disable the address. |
| `complaint` | The person is unsubscribed from every optional category, recorded in the consent ledger with source `complaint`. Receipts and alerts still reach them. |

Report from your backend or a provider's webhook handler (`recipients:write`):

```http
POST /v1/feedback
{"type": "bounce", "channel": "email", "provider_message_id": "0100018c…",
 "address": "ann@example.com", "reason": "550 5.1.1 user unknown"}
```

Name the message by Cue's `message_id` or the `provider_message_id` Cue stored when it
sent it (with its `channel`), the address, or both. Without an address, a bounce only
disables an address when the message's recipient has a single one on that channel.
Disabled addresses come back only through `POST /v1/recipients/{id}/addresses`.

The SDKs have `report_feedback` (Python) and `reportFeedback` (TypeScript).

**Twilio** reports directly: set the channel's `status_callback` to
`<api.public_url>/v1/inbound/twilio/status`. Delivered texts are marked delivered;
numbers that cannot receive texts (unknown handset, landline, blocked, opted out) are
disabled, and other failures count as soft bounces. The request signature is checked
against the auth token of the Twilio channel for the request's `AccountSid`. Status
callbacks share the `api.rate_limit.inbound` budget (600 a minute per address); raise
it if you send more.

## Bundled connectors

### `fcm` — Firebase Cloud Messaging

Requires `pip install 'cue-notify[fcm]'`. Address: FCM registration token.

| Option | Default | |
|---|---|---|
| `credentials_file` / `credentials_json` | — | Service-account key (exactly one). |
| `project_id` | from the key | |
| `android_priority` | `high` | `normal` or `high`. |
| `sound` | `default` | `null` for silent. |
| `ttl_seconds` | — | Android time-to-live. |

The push `data` payload contains your template's `data`, `url`, plus
`cue_message_id` and `cue_tracking_token` for [engagement tracking](engagement.md).

### `twilio` — SMS

Address: E.164 phone number.

| Option | |
|---|---|
| `account_sid`, `auth_token` | Credentials. |
| `from_number` or `messaging_service_sid` | Sender. |
| `status_callback` | Delivery status webhook; set it to `<api.public_url>/v1/inbound/twilio/status` for [delivery feedback](#delivery-feedback). |
| `include_title` | Prefix the body with the title (default `false`). |

### `smtp` — e-mail

Address: e-mail address. Uses only the standard library.

| Option | Default |
|---|---|
| `host`, `port` | —, `587` |
| `security` | `starttls` (`tls` for implicit TLS on 465, `none`) |
| `username`, `password` | — |
| `from_address`, `from_name`, `reply_to` | — |

Sends a text part, plus an HTML part when the template has `html`. With
[`api.public_url`](preferences.md) set, messages in categories people may leave carry
`List-Unsubscribe` and `List-Unsubscribe-Post` headers for one-click unsubscribe.

### `telegram` — Telegram bots

Address: the chat id. Option: `bot_token`, `disable_notification`, `link_preview`,
`api_url` (for a self-hosted Bot API server).

### `webhook` — HTTP

Posts JSON to `url`, or — when `url` is not set — to the address itself (useful for
per-user Slack, Discord or Teams incoming webhooks).

Per-recipient URLs are untrusted input, so by default they must use `https`, must
resolve only to public IP addresses (no `localhost`, private ranges or cloud metadata
endpoints), and the connection is pinned to the address that was checked. Response bodies
from such URLs are never stored. `allow_http` and `allow_private_networks` relax this for
internal deployments; a fixed `url` is operator configuration and is trusted.

The body:

```json
{
  "id": "0192…",
  "channel": "email",
  "address": "ann@example.com",
  "locale": "en",
  "content": {"title": "Your order shipped", "body": "…", "url": "…", "thread": "order-A1"},
  "metadata": {"cue_message_id": "0192…", "cue_tracking_token": "…"},
  "links": {"preferences": "https://notify.example.com/preferences/…",
            "unsubscribe": "https://notify.example.com/preferences/…/unsubscribe?category=marketing"}
}
```

`links` is empty unless [`api.public_url`](preferences.md) is set; `unsubscribe` is only
present for categories people may leave. Put it in a `List-Unsubscribe` header if your
service sends e-mail.

With `secret` set, requests carry:

```text
Cue-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">
Idempotency-Key: <message id>
```

Verify the signature and reject timestamps older than a few minutes.

A channel with a fixed `url` and no `secret` sends unsigned requests that anyone could
imitate. Cue logs a warning at startup and `cuectl check` fails for such a channel; set
`allow_unsigned = true` if the receiver authenticates Cue another way (for example a
token in `headers`).

### `console`

Logs messages instead of sending them. For development and demos.

## Your own connector

Any package can add a connector — see [custom channels](../guides/custom-channels.md).
