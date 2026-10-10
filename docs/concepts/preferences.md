# Preference centre

People need a way to say "less of this, not by SMS, and nothing at all from marketing"
without contacting support. Gmail and Yahoo also require bulk senders to support
**one-click unsubscribe** ([RFC 8058](https://www.rfc-editor.org/rfc/rfc8058)) and to
honour it within two days. Cue gives you three pieces:

1. A **preference token** per recipient: a link credential, like a password-reset link.
2. A **hosted preference page** and an RFC 8058 **one-click unsubscribe** endpoint,
   plus a JSON API for building the page into your own app instead.
3. A **consent ledger** recording every preference change, who made it and when.

## Turn it on

Tell Cue the public URL it is reachable at:

```toml
[api]
public_url = "https://notify.example.com"   # include any path prefix
product_name = "Acme"                       # shown on the hosted page
```

From then on every message knows its links, and so does every template:

| Variable | Contents |
|---|---|
| `links.preferences` | `https://notify.example.com/preferences/<token>`, the hosted page. |
| `links.unsubscribe` | One-click unsubscribe from this message's category. `null` for categories people cannot leave (`allow_unsubscribe: false`, e.g. `transactional`). |
| `links.token` | The raw token, to link to a preference screen in your own app. Always set, even without `public_url`. |

```jinja
{% if links.unsubscribe %}Don't want these? {{ links.unsubscribe }}{% endif %}
```

## What connectors do with the links

- **`smtp`** adds the headers mailbox providers look for whenever the category allows
  unsubscribing:

    ```text
    List-Unsubscribe: <https://notify.example.com/preferences/…/unsubscribe?category=marketing>
    List-Unsubscribe-Post: List-Unsubscribe=One-Click
    ```

- **`webhook`** includes `"links": {"preferences": …, "unsubscribe": …}` in its payload,
  so your own e-mail service can set the same headers.
- **Custom connectors** read `delivery.links`.

## The hosted page

`GET /preferences/{token}` is a small server-rendered page with no JavaScript. It works
in any mail client's browser, follows the system's dark mode and is accessible. It
lists every category, with the ones people cannot leave shown as locked, and every
route the person has an address on. It also offers **Unsubscribe from all optional
messages**.

`GET /preferences/{token}/unsubscribe?category=…` asks for confirmation, and a `POST`
to the same URL unsubscribes. Mailbox providers send exactly that `POST` for one-click
unsubscribe. A `GET` never changes anything, because security scanners open every link in
an e-mail before the person does.

The pages are served with `Cache-Control: no-store`, `Referrer-Policy: no-referrer`
(the token never leaks to another site), `X-Robots-Tag: noindex` and a strict Content
Security Policy that forbids framing.

## Your own preference screen

Prefer your app's look and feel? Use the same token with the JSON API. It needs no API
key, because the token is the credential:

```http
GET /v1/preferences/{token}
```

```json
{
  "locale": "en",
  "categories": [
    {"key": "marketing", "description": "Offers and news", "subscribed": true, "can_unsubscribe": true},
    {"key": "transactional", "description": "Receipts and security", "subscribed": true, "can_unsubscribe": false}
  ],
  "channels": [{"name": "push", "muted": false}, {"name": "sms", "muted": true}],
  "quiet_hours": null
}
```

```http
PUT /v1/preferences/{token}
{"unsubscribed_categories": ["marketing"], "muted_channels": ["sms"]}
```

Omitted fields are left unchanged. Unknown categories or channels are rejected with `422`,
as is unsubscribing from a category that does not allow it. Your backend gets the token
from `GET /v1/recipients/{external_id}` (`preference_token`). If the browser calls Cue
directly, list your app's origin in `api.cors_origins`.

## Consent ledger

Every change, from any source, is recorded with only what changed:

```http
GET /v1/recipients/user-42/preferences/history
```

```json
{"items": [
  {"id": "…", "source": "one_click", "changes": {"unsubscribed": ["marketing"]}, "created_at": "2026-10-10T09:12:44Z"},
  {"id": "…", "source": "api", "changes": {"muted": ["sms"]}, "created_at": "2026-10-01T16:03:10Z"}
], "next_cursor": null}
```

| `source` | Who |
|---|---|
| `api` | Your backend, via `PUT /v1/recipients/{id}/preferences`. |
| `self_service` | The person, on the hosted page or through your UI calling `/v1/preferences/{token}`. |
| `one_click` | The person's mail client (RFC 8058), or the confirmation page's button. |

Change keys are `unsubscribed`, `resubscribed`, `muted`, `unmuted` and `quiet_hours`.
Deleting a recipient erases their ledger along with their profile.

## Rotating a token

Anyone who holds the link can change that person's preferences. That is the point of an
unsubscribe link, but it is also why a forwarded e-mail is a risk. To cut off old links:

```http
POST /v1/recipients/{external_id}/preference-token
```

Links in messages sent before the rotation then show "This link is no longer valid".
