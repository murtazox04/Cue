# Importance and expiry

Phones now decide for themselves what deserves attention. iOS has interruption levels
and AI summaries, and Android's Notification Organizer silences promotions by default.
A message that says how important it is, and when it stops mattering, is handled
better by the device. It also gives Cue's policy something real to decide on.

## Importance

| Level | Use it for | iOS | Android | Telegram | E-mail |
|---|---|---|---|---|---|
| `low` | Informational: a weekly summary, a new follower | passive (no sound) | normal priority | silent | `Importance: low` |
| `normal` | Most messages (default) | active | channel default | normal | none |
| `high` | Time-sensitive: "your order is at the door" | time-sensitive | high priority | normal | `Importance: high` |
| `critical` | Security and safety: a login from a new device | time-sensitive | high priority | normal | `Importance: high` |

Policy treats `critical` differently from the rest: it **breaks through quiet hours and
frequency caps**. To stop that being abused, it is only honoured in categories people
cannot unsubscribe from (`allow_unsubscribe: false`). Anywhere else it is stored and
delivered as `high`. Unsubscribes, muted channels, cooldowns and deduplication always
apply.

Set importance on a rule, or on a direct send:

```json
POST /v1/rules
{"key": "new-login", "event": "auth.login_new_device", "template": "new-login",
 "category": "security", "channels": ["push", "email"], "importance": "critical"}
```

```json
POST /v1/messages
{"recipient": "user-42", "template": "courier-arriving", "channels": ["push"],
 "importance": "high", "expires_at": "2026-10-10T18:30:00Z"}
```

Templates can read it as `{{ importance }}`, and connectors receive it as
`delivery.importance`. The webhook payload includes it too.

## Expiry

A one-time code is useless after five minutes, and "your driver is here" is wrong an
hour later. Give such messages an expiry:

- direct sends: `expires_at`;
- rules: `expires_after_seconds`, counted from when the event is processed.

Cue drops the message (`suppressed`, reason `expired`) if quiet hours would delay it past
its expiry, and checks again right before sending, so a slow worker or a provider outage
never delivers stale news. Push connectors pass the expiry on to the push service as well
(FCM `ttl`, APNs `apns-expiration`), so a phone that was offline does not show it later.

## Actions

Templates can add up to four buttons. Their `label` and `url` are templates:

```json
"en": {
  "title": "Your order shipped",
  "body": "Order {{ data.order_id }} is on its way.",
  "actions": [
    {"id": "track", "label": "Track", "url": "https://shop.example/track/{{ data.order_id }}"},
    {"id": "dismiss", "label": "Not now"}
  ]
}
```

| Connector | What it does with actions |
|---|---|
| `fcm` | `data.actions` (JSON) for your app to render as notification actions |
| `telegram` | Inline keyboard buttons for actions with a `url` |
| `smtp` | "Label: URL" lines in the text part |
| `webhook` | `content.actions`, as is |
