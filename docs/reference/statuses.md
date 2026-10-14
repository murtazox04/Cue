# Statuses and reasons

Every decision Cue makes is stored. This page lists the values you will find in
`status`, `status_reason` and an event's `outcome`, and the error responses of the API.

## Message status

`GET /v1/messages/{id}` → `status`.

| Status | Meaning | Final |
|---|---|---|
| `queued` | Accepted and waiting for its send time. Can still be cancelled. | no |
| `sending` | Claimed by a worker and being handed to a provider; can no longer be cancelled. | no |
| `pending_approval` | Held until a person approves it (agent approval policy or screening). Not sent. | no |
| `sent` | A provider accepted it. | yes |
| `delivered` | The provider or the client app confirmed delivery. | yes |
| `failed` | Every planned channel failed permanently, retries ran out, or the template could not be rendered. | yes |
| `suppressed` | Policy decided against sending it; see `status_reason`. | yes |
| `cancelled` | Withdrawn before sending: cancelled through the API, its broadcast was cancelled, or a reviewer rejected it. | yes |

Engagement (`opened`, `clicked`, `converted`) is tracked separately from the status; see
[engagement tracking](../concepts/engagement.md).

## Message `status_reason`

Policy reasons come first in the order Cue checks them. Some carry detail after a colon.

| `status_reason` | Status | Why |
|---|---|---|
| `duplicate` | `suppressed` | The same event and rule, broadcast and recipient, or send `idempotency_key` already produced a message. Only seen in dry runs: a real duplicate returns the original message. |
| `unsubscribed` | `suppressed` | The person left this category. Checked again right before sending. |
| `consent_expired` | `suppressed` | The opt-in recorded for this category has expired. |
| `no_reachable_channel` | `suppressed` or `failed` | When composing (`suppressed`): the person has no enabled address on any of the planned channels (or muted them), or none of those channels is configured. At send time (`failed`): no address was left to try. |
| `compliance: <cap> marketing texts per 24 hours (<source>)` | `suppressed` | US state texting cap, with `[compliance] us_texting_rules`. See [compliance](../guides/compliance.md). |
| `cooldown` | `suppressed` | The rule already sent to this person within `cooldown_seconds`. |
| `expired` | `suppressed` | The message could not go out before `expires_at`: quiet hours or texting hours pushed it past its expiry, it waited for approval too long, or it was still queued when it expired. |
| `frequency_cap: <limit> per <period>` | `suppressed` | The category's rolling cap is used up around the send time, e.g. `frequency_cap: 3 per 1 day, 0:00:00`. |
| `fatigue` | `suppressed` | The person ignored this category's recent messages; see [policy](../concepts/policy.md). |
| `agent_budget: <limit> per <period>` | `suppressed` | The sending agent used its attention budget for this person. |
| `agent_not_allowed` | `suppressed` | Checked right before sending: the sending agent was disabled, or may no longer use the message's category. |
| `agent_deleted` | `suppressed` | Checked right before sending: the sending agent was deleted. |
| `template_error: <detail>` | `failed` | The template has no usable locale or failed to render. |
| `screening: <risks>` | `pending_approval` | [Screening](../concepts/agents.md#screening) held it: `pressure`, `deception`, `secret`, `critical not confirmed`, or `screening unavailable` when the screening API failed and `on_error = "hold"`. |
| `rejected` or `rejected: <reason>` | `cancelled` | A reviewer rejected it. |
| `cancelled via API` | `cancelled` | `POST /v1/messages/{id}/cancel`. |
| `broadcast cancelled` | `cancelled` | Its broadcast was cancelled. |
| `gave up: <error>` | `failed` | Its delivery job used up `worker.max_attempts` (or `max_defer_seconds`). `cuectl jobs retry-dead` reopens it. |
| provider errors, joined by `; ` | `failed` | Every channel failed permanently; one entry per channel. |
| `null` | any | Nothing to explain: queued, sent, delivered, or approved and queued. |

## Event status and rule outcomes

`GET /v1/events/{id}` → `status` is `pending` (waiting for the worker), `processed`, or
`failed` (processing gave up; the reason is in `error`).

`outcome` has one entry per rule whose `event` pattern matched the name:

| `reason` | `matched` | Meaning |
|---|---|---|
| `outside_rollout` | `false` | The recipient is not in the rule's `rollout_percent`. |
| `condition_false` | `false` | The rule's condition did not hold. |
| `condition_error: <detail>` | `false` | The condition could not be evaluated for this event. |
| `misconfigured` | `true` | The rule's template or category is missing. |
| `agent_not_allowed` | `true` | The event came from an agent that may not use the rule's category, or the agent is disabled. |
| `agent_deleted` | `true` | The event came from an agent that was deleted before the event was processed. |
| `digested` | `true` | The event joined a digest (`digest_id`). |
| a message reason | `true` | A message was composed: `status` and `message_id` are set, and `reason` is the message's `status_reason` above (absent when it is `null`). |

Rules lower in priority than a matching rule with `stop_processing` are not listed.
Wildcard rules (`*`, `cue.*`) never see Cue's own `cue.*` events.

## Other statuses

| Resource | Values |
|---|---|
| Digest | `open` (collecting until `closes_at`), `sent` (composed into a message, which policy may still suppress), `cancelled` (discarded, e.g. its rule was disabled). |
| Broadcast | `draft`, `scheduled`, `running`, `paused`, `completed`, `cancelled`, `failed`. |
| Approval request | `pending`, `approved`, `rejected`, `expired` (nobody decided before `expires_at`; treat it as a no). A request past its `expires_at` reads as `expired` even before the expiry job has run. |
| Consent ledger `source` | `api`, `self_service`, `one_click`, `sms_keyword`. |

## Problem responses

Errors are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem details
(`application/problem+json`): `{"type": "about:blank", "title", "status", "detail"}`,
plus `errors` (each with `loc`, `msg`, `type`) for validation errors.

| Status | Title | When |
|---|---|---|
| `400` | Bad request | A request the service cannot act on. |
| `401` | Unauthorized | No API key, or an unknown, revoked or expired one (`WWW-Authenticate: Bearer`). |
| `403` | Forbidden | The key lacks the scope; an agent key reaching past its limits (other categories, other senders' messages, recipient profiles, reserved `cue.*` names); a disabled agent; changing a stored profile inline without `recipients:write`; an invalid Twilio signature. |
| `404` | Not found | The resource, or the preference page for a token, does not exist. |
| `409` | Conflict | Duplicate keys, concurrent changes (retry), or an action the resource's state does not allow (title `Invalid state`), such as approving a message that is not waiting. |
| `410` | Gone | A review link or approval link that was already used or has expired. |
| `413` | Content too large | The body is over `api.max_body_bytes` (or `max_broadcast_body_bytes`). |
| `422` | Unprocessable content | Validation failed, or a request that cannot be honoured, such as an agent key with a forbidden scope. |
| `429` | Too many requests | A rate limit (with `Retry-After`), or an agent over `approvals.max_pending_per_agent`. |
| `500` | Internal server error | Logged with the request path. |

The hosted preference and review pages show the same errors as HTML pages.
