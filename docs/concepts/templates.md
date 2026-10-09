# Templates

A template holds the content of a message in one or more locales.

```json
{
  "key": "payment-due",
  "default_locale": "en",
  "locales": {
    "en": {
      "title": "Payment due {{ data.due_date }}",
      "body": "{{ data.amount | number }} {{ data.currency }} is due. Tap to pay.",
      "url": "myapp://invoices/{{ data.invoice_id }}",
      "image_url": null,
      "data": {"screen": "invoice", "invoice_id": "{{ data.invoice_id }}"},
      "channels": {
        "sms": {"body": "{{ data.amount | number }} {{ data.currency }} due {{ data.due_date }}."},
        "email": {"html": "<p>Your invoice <b>{{ data.invoice_id }}</b> is due.</p>"}
      }
    },
    "ru": {"title": "…", "body": "…"}
  },
  "ai_instructions": null,
  "ai_attributes": []
}
```

## Fields

| Field | Used by |
|---|---|
| `title` | Push title, e-mail subject, bold first line in Telegram. |
| `body` | Every channel. |
| `url` | Deep link: push `data.url`, appended to SMS/Telegram, link in e-mail text. |
| `image_url` | Push notification image. |
| `html` | E-mail HTML part (auto-escaped). |
| `data` | Push data payload / webhook payload. Values are templates too. |
| `channels` | Per-channel overrides. Unset fields inherit from the locale's base content. |

## Variables

| Variable | Contents |
|---|---|
| `data` | The event's `data`, the broadcast's `variables`, or the direct send's `data`. |
| `recipient` | `external_id`, `locale`, `timezone`, `attributes`. |
| `event` | `name`, `source`, `occurred_at` — `null` for direct sends and broadcasts. |
| `category` | The message category key. |

Filters: everything [Jinja2 provides](https://jinja.palletsprojects.com/en/stable/templates/#list-of-builtin-filters)
plus `number(decimals=0, separator=" ", point=".")` for thousands separators:
`{{ 1250000 | number }}` → `1 250 000`.

## Safety and strictness

- Templates run in Jinja's **immutable sandbox**: no access to Python internals, no
  mutation of data.
- **Undefined variables are errors**, not empty strings. A message that would read
  "Your order  has shipped" fails with `status_reason: template_error: …` instead. Use
  `{{ data.name | default("there") }}` for optional values.
- Syntax is checked when the template is saved.
- `html` is auto-escaped; other fields are plain text.
- **Resource limits** stop runaway templates: `range()` is capped at 1 000 items,
  powers at an exponent of 64, sequence repetition at 10 000 items, `center`/`indent`
  widths at 200 and each rendered field at 20 000 characters. Python string formatting
  (`%`, `.format`, the `format` filter) is disabled — use `~` to concatenate.

## Locale selection

For a recipient with locale `uz-Latn-UZ`, Cue tries `uz-latn-uz`, `uz-latn`, `uz`,
then the template's `default_locale`, then any available locale. Matching is
case-insensitive and treats `_` like `-`.

## Versions and previews

Every content change increments `version`; messages record the template version they were
rendered from. `POST /v1/templates/{key}/preview` renders all variants with sample data
(and optionally a real recipient) without sending anything.
