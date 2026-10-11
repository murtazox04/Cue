# Agents

A person now hears from more software than ever: your support assistant, a sales agent,
a coding agent reporting back, background jobs. Each sender is reasonable alone, but
together they bury the person. An **agent** in Cue is an automated sender with its own
limits, applied on top of everything else Cue already checks:

| Limit | Default | Effect |
|---|---|---|
| `frequency_caps` | 5 per person per day | Its attention budget. Messages beyond it are `suppressed` with reason `agent_budget`. |
| `max_importance` | `high` | Anything higher is lowered to this, so only agents you trust can claim `critical`. |
| `categories` | any | Categories it may send in; others are refused with `403`. |
| `approval` | none | A JSON Logic condition (or `true`): when it holds, the message waits for a person to approve it. |
| `approval_ttl_seconds` | 1 day | Unreviewed messages expire. |
| `reviewers` | none | Recipients (external ids) to tell when a message waits for review. |
| `enabled` | `true` | Set `false` to stop the agent at once; its keys are refused. |

Recipient preferences, category policy (caps, quiet hours, unsubscribes), importance
rules and expiry all still apply. An agent can only ever be *more* restricted than your
own backend. Its keys can never:

- hold the `admin` scope, which would let it loosen its own limits;
- change, reset or delete anyone's preferences, rotate preference tokens or see them (they
  are redacted from recipients and message content it reads);
- create or change recipient profiles or addresses, inline or through
  `/v1/recipients`: that would decide where everyone's messages go, or change the time
  zone and attributes policy decides on; agents refer to people by external id;
- report events as another producer: an agent's events always have source
  `agent:<key>`, and the `cue.*` names and source `cue` are reserved for Cue;
- cancel, review or report engagement on messages that are not its own.

## Set one up

```http
POST /v1/agents
{
  "key": "support-bot",
  "name": "Support assistant",
  "categories": ["transactional", "support"],
  "frequency_caps": [{"period": "P1D", "limit": 3}],
  "approval": {"in": [{"var": "message.importance"}, ["high", "critical"]]}
}
```

Then give the agent its own key:

```bash
cuectl keys create support-bot-key -s messages:write -s messages:read --agent support-bot
```

Everything sent with that key is attributed to the agent (`"agent": "support-bot"` on
the message) and filtered by `GET /v1/messages?agent=support-bot`. Hand the key to the
agent, for example through the [MCP server](../guides/ai-agents.md).

Agent policy applies to direct sends (`POST /v1/messages`, the MCP `send_message` tool)
and to events the agent reports: those only fire rules in categories the agent may use
(other rules report `agent_not_allowed`), and the messages they produce count against its
budget and go through its approval policy.

## Approval

The condition sees:

```json
{
  "message": {"category": "support", "importance": "high", "channels": ["push"], "template": "refund-issued"},
  "recipient": {"external_id": "user-42", "locale": "en", "timezone": "Europe/Berlin", "attributes": {…}},
  "data": {…}
}
```

Matching messages are stored with status `pending_approval`. They are not sent, but they
count against the agent's budget, so an agent cannot flood the review queue. A person
then decides, with a key that is not bound to an agent (your admin UI or backend, or
Claude acting with such a key):

```http
GET  /v1/messages?status=pending_approval
POST /v1/messages/{id}/approve
POST /v1/messages/{id}/reject   {"reason": "wrong customer"}
```

- **Approved:** the message is queued for delivery, and policy is checked again at send
  time.
- **Rejected:** the message is `cancelled` with the reason recorded.
- **Expired while waiting:** an approval that arrives after `expires_at` drops the message
  instead (`suppressed`, `expired`).
- **Record:** the reviewer's key name and the time are stored on the message
  (`reviewed_by`, `reviewed_at`).
- **Only people review:** agent keys cannot approve or reject anything, not even
  another agent's messages, so two agents can never approve each other.

Over MCP, reviewers use the `review_message` tool.

### Telling reviewers

List the people who review an agent's messages in `reviewers`. For each held message, Cue
reports a `cue.approval_requested` event for each reviewer (source `cue`). Turn it into a
notification with an ordinary rule and template, so reviewers get it through their own
channels, quiet hours and preferences:

```json
POST /v1/rules
{"key": "review-requests", "name": "Ask a reviewer", "event": "cue.approval_requested",
 "template": "review-request", "category": "transactional", "channels": ["push", "email"]}
```

The event's `data` holds `message_id`, `agent`, `agent_name`, `recipient` (whom the agent
wants to message), `category`, `importance`, `title`, `body` (the first 500 characters),
`expires_at` and `review_url`:

```json
"en": {"title": "{{ data.agent_name }} needs your OK",
       "body": "To {{ data.recipient }}: {{ data.body }}",
       "url": "{{ data.review_url }}"}
```

### Deciding from the notification

`review_url` (set when `api.public_url` is configured) opens a hosted page that shows the
message with **Approve and send** and **Reject** buttons, so a reviewer can decide from
their phone without an API key. Each reviewer gets their own link:

- **One-time:** the first decision, through any link or the API, closes every link for
  that message. Later visits say who already decided.
- **Expiring:** a link lives as long as the held message (`approval_ttl_seconds`).
- **Safe to preview:** opening the link changes nothing (mail scanners prefetch links);
  only the buttons, which `POST`, act.
- **Recorded:** the message's `reviewed_by` is `link:<reviewer>`.
- **Unguessable:** the token has 256 bits of randomness, and links are looked up by its
  SHA-256 hash.

Agent keys see only the messages and events they sent themselves, and no digests, so an
agent can never read its own review link. Other keys without `admin` see review links as
`/review/[redacted]`, so a read-only dashboard key cannot decide either.

## Screening

!!! warning "Experimental"
    Screening is built and tested against TypeSafe's published request and response
    format, but has not yet been proven against the live service in production. Try it
    on a staging agent first, and tell us how it behaves.

Approval rules see the category and the importance an agent claims, not what its message
says. Screening reads the words. When it is on, every agent message is checked just
before delivery by a decision model, [Jev](https://docs.typesafe.ai) from TypeSafe AI. It
returns calibrated probabilities instead of text, typically in 70–500 ms, at
$0.042 per million input tokens. Cue asks four narrow questions:

| Question | Kind | Used for |
|---|---|---|
| How urgent is this for the reader, from its content? | score, 4 levels (low → critical) | the real importance |
| Does it pressure or manipulate the reader? | yes/no probability | holding it |
| Does it look deceptive (impersonation, credential or payment requests)? | yes/no probability | holding it |
| Does it expose a password, API key or other credential? | yes/no probability | holding it |

Cue's code, not the model, decides what happens:

- **Held:** any risk at or above `hold_above` puts the message in `pending_approval`
  with `status_reason: "screening: pressure"` (or whichever risks were found).
  Reviewers are told, and the approval TTL applies, as with
  [approval](#approval).
- **Critical not confirmed:** a `critical` claim the model does not confirm with
  `min_confidence` is held too. Critical has already skipped quiet hours and caps.
- **Lowered:** when the model is at least `min_confidence` sure that the message is
  less urgent than claimed, it is sent with the lower importance. Importance is never
  raised.
- **Passed:** otherwise, it is sent as is.

The model sees the sender (agent key and `description`), the category, the claimed
importance and the rendered text, links and actions. It never sees the recipient's
profile or the message's `data`. What it answered and what Cue did are stored in
`screening` on the message:

```json
"screening": {"model": "jev-1.13.0", "outcome": "lowered", "lowered_from": "high",
              "risks": {"pressure": 0.04, "deception": 0.01, "secret": 0.0},
              "urgency": {"importance": "normal", "confidence": 0.82}}
```

Each message is screened once. Retries reuse the stored verdict, and a message a person
approved is not screened again. Your own backend's messages are never screened.

```toml
[screening]
enabled = true          # key from TYPESAFE_API_KEY or CUE_SCREENING__API_KEY
hold_above = 0.5
min_confidence = 0.6
on_error = "hold"       # or "send": what to do when the screening API is unavailable
```

Screening runs in the worker, after the API has accepted the message, so it adds no
latency to sends. The answers are a second opinion, not a guarantee: keep budgets and
approval rules for the cases that must never slip through.
