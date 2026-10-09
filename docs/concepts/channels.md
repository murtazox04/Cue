# Channels

A **channel** is a named, configured delivery route — `push`, `sms`, `email`, `alerts`…
Its **provider** is the implementation. Rules, broadcasts and recipient addresses refer
to channels by name, so you can switch SMS vendors without touching any rule.

```toml
[channels.push]
provider = "fcm"
credentials_file = "/run/secrets/firebase.json"
```

Equivalent environment variables: `CUE_CHANNELS__PUSH__PROVIDER=fcm`,
`CUE_CHANNELS__PUSH__CREDENTIALS_FILE=…`. Configuration is validated at start-up;
`cuectl check` prints the configured channels.

## Addresses

A recipient has any number of addresses per channel — every phone, browser and tablet:

```json
POST /v1/recipients/user-42/addresses
{"channel": "push", "value": "<FCM registration token>", "label": "Pixel 9"}
```

A message goes to **all** enabled addresses of the first channel that works. When a
provider reports an address as permanently invalid (unregistered token, blocked bot,
unreachable number), the address is disabled with a reason and skipped from then on;
re-adding it re-enables it.

## Fallback and retries

For each planned channel, in order:

- **Success** on any address → message `sent`.
- **Transient error** (timeouts, HTTP 429/5xx) → the delivery job is retried with
  exponential backoff (honouring `Retry-After`). On the last attempt Cue falls back
  to the next channel instead.
- **Permanent error** → the next channel is tried immediately.

If no channel succeeds the message is `failed` with the reasons in `status_reason`.

## Built-in providers

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
| `status_callback` | Optional Twilio status webhook URL. |
| `include_title` | Prefix the body with the title (default `false`). |

### `smtp` — e-mail

Address: e-mail address. Uses only the standard library.

| Option | Default |
|---|---|
| `host`, `port` | —, `587` |
| `security` | `starttls` (`tls` for implicit TLS on 465, `none`) |
| `username`, `password` | — |
| `from_address`, `from_name`, `reply_to` | — |

Sends a text part, plus an HTML part when the template has `html`.

### `telegram` — Telegram bots

Address: the chat id. Option: `bot_token`, `disable_notification`, `link_preview`,
`api_url` (for a self-hosted Bot API server).

### `webhook` — HTTP

Posts JSON to `url`, or — when `url` is not set — to the address itself (useful for
per-user Slack, Discord or Teams incoming webhooks). With `secret` set, requests carry:

```text
Cue-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">
Idempotency-Key: <message id>
```

Verify the signature and reject timestamps older than a few minutes.

### `console`

Logs messages instead of sending them. For development and demos.

## Your own provider

Any package can add a provider — see [custom channels](../guides/custom-channels.md).
