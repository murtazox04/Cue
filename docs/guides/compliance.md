# Compliance

Cue keeps a few legal safety nets for messages that need consent: legal hours and caps for
marketing texts in the US, opt-out replies, and a consent ledger that records how each
change was made. They are guard rails, **not legal advice**; check what applies to you.

## Marketing categories

Mark the categories that carry promotions:

```http
POST /v1/categories  {"key": "deals", "marketing": true, "allow_unsubscribe": true}
```

The built-in `marketing` category is marked already. Receipts, codes and alerts belong
in categories that are not marketing.

## US texting hours and caps

```toml
[compliance]
us_texting_rules = true
sms_channels = ["sms"]   # your channels that send text messages
unknown_state = "strict" # or "federal"
```

When it is on, a **marketing** message planned for a text channel and a US recipient is
held until the recipient's legal hours, in their time zone. In states with a cap, a
fourth marketing message within 24 hours is suppressed with
`status_reason: "compliance: 3 marketing texts per 24 hours (…)"`. Transactional texts
and other channels are not affected.

| Jurisdiction | Hours (recipient's local time) | Cap per 24 h | Source |
|---|---|---|---|
| US (federal) | 8:00–21:00 | — | 47 CFR 64.1200(c)(1) |
| Connecticut | 9:00–20:00 | — | Conn. Gen. Stat. 42-288a(c) |
| Florida | 8:00–20:00 | 3 | Fla. Stat. 501.616(6) |
| Maryland | 8:00–20:00 | 3 | Md. Code, Com. Law 14-4502(c) |
| Oklahoma | 8:00–20:00 | 3 | Okla. Stat. tit. 15, 775C.4 |
| Oregon | 8:00–20:00 | 3 | Oregon HB 3865 (2025) |
| Texas | 9:00–21:00; Sundays 12:00–21:00 | — | Tex. Bus. & Com. Code 301.051(b)(2) |
| Utah | 8:00–21:00; not on Sundays | — | Utah Code 13-25a-103(3) |
| Virginia | 8:00–21:00 | — | Va. Code 59.1-511 |
| Washington | 8:00–20:00 | — | RCW 80.36.390(8) |
| **Strict** (state unknown) | 9:00–20:00, not on Sundays | 3 | all of the above |

**How Cue knows where someone is.** Set the recipient's `region` (ISO 3166: `US`,
`US-FL`) and `timezone`. Without a region, a `+1` number counts as a US recipient in an
unknown state, and `unknown_state` decides between the strict window (the default) and
the federal one. The table reflects statutes checked in October 2026. Some state rules
are written for calls, and whether they reach texts is unsettled; Cue applies them to
texts to stay on the safe side. Legal holidays (Utah, among others) are not modelled;
add them with quiet hours if you need them.

## STOP and START replies

The FCC treats a reply of *stop, quit, end, revoke, opt out, cancel* or *unsubscribe* as
withdrawn consent, to be honoured within 10 business days (47 CFR 64.1200(a)(10)). Cue
honours it at once. For every recipient with that number:

- **On STOP:** text messages are muted, and the person is unsubscribed from every
  optional category.
- **On START (or UNSTOP, YES):** text messages come back. Categories stay unsubscribed
  until the person opts in again.
- **Recorded:** both changes go into the consent ledger with source `sms_keyword`, the
  keyword and the number.

Report replies from any provider:

```http
POST /v1/inbound/sms  {"from": "+15551230000", "body": "STOP", "message_id": "SM…"}
```

Or point Twilio's incoming-message webhook at `https://<api.public_url>/v1/inbound/twilio`.
Cue checks Twilio's request signature with the auth token of the Twilio channel listed in
`sms_channels`. Twilio's own opt-out handling still replies to the person; Cue makes sure
nothing more is planned for them.

!!! note "FCC 26-67"
    On 30 September 2026 the FCC adopted an order that replaces the "revoke-all" rule
    due on 31 January 2027. Once it takes effect, opting out of informational messages
    may be limited to the category the person named. Marketing opt-outs still stop all
    marketing, and the standard keywords must still be honoured. Cue's STOP handling
    already does the stricter of the two, which satisfies both.

## Consent ledger and provenance

Every change to someone's preferences is kept (`GET /v1/recipients/{id}/preferences/history`)
with **who** (`source`), **what** (`changes`), **when** (`created_at`) and **how**
(`provenance`):

| Source | Provenance |
|---|---|
| Hosted page and preference API | `method`, `ip`, `user_agent` |
| One-click unsubscribe | `method: "one_click"`, `ip`, `user_agent` |
| STOP / START | `method: "sms_reply"`, `keyword`, `from`, `message_id`, `provider` or `api_key` |
| Your backend | `method: "api"`, `api_key` |

## Recording opt-ins

Regulators expect you to show who agreed, when, how, and to what. Record each opt-in
with the exact wording the person saw:

```http
POST /v1/recipients/{id}/consent
{
  "categories": ["marketing"],
  "disclosure": "Yes, text me deals from Shop. Msg & data rates may apply. Reply STOP to end.",
  "disclosure_version": "2026-10",
  "method": "checkbox",
  "source": "https://shop.example/checkout",
  "ip": "198.51.100.7",
  "user_agent": "Mozilla/5.0 …",
  "expires_at": "2027-10-11T00:00:00Z"
}
```

Cue subscribes the person to those categories. It writes a ledger entry with the
disclosure text, its SHA-256, the method, the source, the IP address, the user agent and
the time. The entry is written even if the person was already subscribed.

With `expires_at`, messages in those categories are suppressed with
`status_reason: "consent_expired"` after that moment, until consent is recorded again.
France, for example, limits consent to telephone canvassing to one year. Agent keys
cannot record consent.
