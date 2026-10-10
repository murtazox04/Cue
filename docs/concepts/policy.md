# Delivery policy

Every message — from a rule, the direct send API or a broadcast — passes through the
same policy, in this order:

1. **Duplicate check** — an identical idempotency/dedup key returns the original message.
2. **Unsubscribe** — the recipient opted out of the category (unless the category does
   not allow it).
3. **Reachability** — keep the planned channels that are configured, not muted by the
   recipient, and have at least one enabled address.
4. **Send time** — now, plus the rule's delay, moved past quiet hours when the category
   respects them.
5. **Cooldown** — the rule already messaged this recipient within `cooldown_seconds`.
6. **Frequency caps** — the category's rolling limits.
7. **Rendering** — the template is rendered for the recipient's locale.

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
