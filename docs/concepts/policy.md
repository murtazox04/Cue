# Delivery policy

Every message — from a rule, the direct send API or a broadcast — passes through the
same policy, in this order:

1. **Duplicate check**: an identical idempotency or dedup key returns the original
   message.
2. **Unsubscribe**: the recipient opted out of the category (unless the category does
   not allow it).
3. **Reachability**: keep the planned channels that are configured, not muted by the
   recipient, and have at least one enabled address.
4. **Send time**: now, plus the rule's delay, moved past quiet hours when the category
   respects them. [`critical`](importance.md) messages are not delayed.
5. **Expiry**: a message that would arrive after its `expires_at` is dropped.
6. **Cooldown**: the rule already messaged this recipient within `cooldown_seconds`.
7. **Frequency caps**: the category's rolling limits (`critical` messages are exempt).
8. **Fatigue**: the person ignored the category's last messages (see below).
9. **Agent budget**: for [agents](agents.md), their per-person limits.
10. **Rendering**: the template is rendered for the recipient's locale.
11. **Approval**: for agents, messages that match their approval policy wait for a
    reviewer.
12. **Screening** (at delivery, optional): a decision model reads an agent's message;
    risky ones wait for a reviewer and overstated importance is lowered. See
    [agents](agents.md#screening).

A message that stops at any step is stored with `status: suppressed` (or `failed` for
template errors) and a `status_reason`, so you can always explain the outcome.

## Categories

Categories carry policy. Two exist out of the box:

| Category | Caps | Quiet hours | Unsubscribe |
|---|---|---|---|
| `transactional` | none | ignored | not allowed |
| `marketing` | 2/day, 5/week | respected | allowed |

Create your own for anything in between:

```json
POST /v1/categories
{
  "key": "product-updates",
  "description": "Feature announcements and tips",
  "frequency_caps": [{"period": "P1D", "limit": 1}, {"period": "P30D", "limit": 6}],
  "respect_quiet_hours": true,
  "allow_unsubscribe": true
}
```

Periods are ISO 8601 durations (`PT1H`, `P1D`, `P7D`) or seconds. Caps are rolling
windows counted over queued, sent and delivered messages of the category.

## Fatigue

Caps treat everyone the same. Fatigue adapts to each person: someone who has ignored
the last few messages of a category is not helped by more of them.

```json
POST /v1/categories
{"key": "social", "fatigue": {"after": 5, "pause": "P7D"}}
```

After `after` messages in a row that the person did not open, click or convert on,
Cue pauses the category's `low` and `normal` messages to them for `pause` (`suppressed`,
reason `fatigue`). When the pause is over, one message is let through as a probe. If
that one is ignored too, the pause starts again; any engagement ends it immediately.
`high` and `critical` messages are never paused.

!!! warning "Report engagement first"
    Fatigue relies on `opened` and `clicked` reports, from your app through
    [`/v1/track/{token}`](engagement.md) or from your backend. Without them every
    message looks ignored.

!!! note "Correct under concurrency"
    Policy decisions lock the recipient row (PostgreSQL) or serialise writes (SQLite),
    so ten simultaneous events cannot sneak past a limit of three.

## Recipient preferences

```json
PUT /v1/recipients/{external_id}/preferences
{
  "unsubscribed_categories": ["marketing"],
  "muted_channels": ["sms"],
  "quiet_hours": {"start": "23:00", "end": "07:30"}
}
```

`quiet_hours` overrides the default for this person; equal `start` and `end` disables
quiet hours for them. Preferences are re-checked right before sending, so an
unsubscribe also stops messages already queued.

People can manage these themselves through the [preference centre](preferences.md), and
every change, from any source, is kept in a consent ledger.

## Quiet hours

The default window (`policy.quiet_hours`, 22:00–08:00) is applied in the **recipient's**
time zone (`recipient.timezone`, falling back to `policy.default_timezone`). A message
due inside the window is scheduled for its end. Daylight-saving transitions are handled
by the IANA time-zone database.
