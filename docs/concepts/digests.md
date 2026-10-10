# Digests

Twelve people comment on a post within a few minutes. Sending twelve notifications
trains the author to ignore you, and modern phones increasingly silence senders that do
this. A **digest** turns the burst into one message: *"Ann, Bo and 10 others commented
on your post."*

A digest is a property of a rule. Add a `digest` policy and the rule stops messaging
for every matching event. Instead, it groups events per recipient and per **grouping key**
for a time window, then composes a single message.

```json
POST /v1/rules
{
  "key": "comment-digest",
  "name": "Group comments per post",
  "event": "comment.created",
  "template": "comments",
  "category": "social",
  "channels": ["push", "email"],
  "digest": {
    "window": "PT10M",
    "key": "{{ event.data.post_id }}",
    "send_after": 20
  }
}
```

| Field | Default | Meaning |
|---|---|---|
| `window` | — | How long to collect after the first event: ISO 8601 (`PT10M`) or seconds. 1 s – 7 days. |
| `key` | one group per rule | Template rendered per event. Events with different keys form separate digests (one per post, per project…). |
| `send_after` | none | Send as soon as this many events are grouped, without waiting for the window to end. |
| `max_items` | 50 | Most recent events kept for the template; older ones are still counted. |
| `summarize` | `false` | Smart mode: let the [AI layer](ai.md) write the summary (see below). |
| `summary_fields` | `[]` | Event `data` keys the AI may see. Required with `summarize`. |

## Writing the template

A digest's template receives the group as `data`:

| Variable | Contents |
|---|---|
| `data.count` | Events grouped, including any beyond `max_items`. |
| `data.events` | The kept events' `data`, oldest first. |
| `data.first`, `data.last` | The first and last kept event's `data`. |
| `data.omitted` | `count` minus the events kept. |
| `data.key` | The rendered grouping key. |

```json
{
  "key": "comments",
  "locales": {
    "en": {
      "title": "{{ data.count }} new {{ 'comment' if data.count == 1 else 'comments' }}",
      "body": "{% for c in data.events[-2:] %}{{ c.author }}{% if not loop.last %} and {% endif %}{% endfor %}{% if data.count > 2 %} and {{ data.count - 2 }} others{% endif %} commented on “{{ data.last.post_title }}”",
      "url": "myapp://posts/{{ data.key }}",
      "thread": "post-{{ data.key }}"
    }
  }
}
```

!!! tip "Grouping on the device too"
    `thread` is the device-side half of grouping. Push connectors map it to the APNs
    `thread-id` and the FCM `data.thread` (so iOS and Android stack messages from the same
    conversation), e-mail gets a stable `References` header, and the webhook connector
    forwards it as-is. It works on any template, not only digests.

## Lifecycle

1. The first matching event **opens** a digest for *(rule, recipient, key)* and schedules
   its send for the end of the window. The event's outcome is `digested`, with the
   `digest_id`.
2. Later events within the window join it. Reaching `send_after` moves the send to now.
3. When it is sent, the digest composes one message through the **regular
   [policy](policy.md)**. Unsubscribes, frequency caps, cooldowns and quiet hours apply
   to the digest message just as to any other message, and a message suppressed by policy
   shows up as such in the message log.
4. The next matching event opens a fresh digest.

A digest whose rule was disabled or deleted before the window closed is **cancelled**,
not sent. Events for the same person are grouped under a row lock, so concurrent workers
never open two digests for one key, and the group stays in the order the events
*happened*, not the order they were processed.

```http
GET  /v1/digests?recipient=user-1&status=open
GET  /v1/digests/{id}
POST /v1/digests/{id}/send     # close the window early
```

Use the [dry-run endpoint](events-and-rules.md) to check a rule. A digest rule reports
`"reason": "digested"` without grouping anything.

## Smart digests

Templates are predictable. Sometimes a summary reads better: *"Your PR got 3 approvals
and 2 change requests, mostly about the migration."* Set `summarize` and list which
event fields the model may read:

```json
"digest": {
  "window": "PT30M",
  "key": "{{ event.data.pr }}",
  "summarize": true,
  "summary_fields": ["author", "verdict", "excerpt"]
}
```

When the digest sends, the template still renders first. That rendered copy is the
**baseline**, and the AI layer rewrites it with the grouped updates as context. Only
`summary_fields` values are shared (plus the template's own `ai_attributes`). The same
guardrails apply as for [AI personalisation](ai.md): numbers and links must survive
verbatim, and any failure, timeout or exhausted token budget falls back to the
template's text. So a smart digest is never worse than an automatic one.

## Choosing a mode

| | Automatic | Smart |
|---|---|---|
| Copy | Your template, deterministic | AI summary of your template's baseline |
| Needs | Nothing | AI enabled (`CUE_AI__MODEL`) |
| Cost | None | One model call per digest |
| Best for | Counts, lists of names, activity feeds | Heterogeneous updates where the gist matters |

Start automatic. Switch on `summarize` for the rules where people tell you the list is
noise.
