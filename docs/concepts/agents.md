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
- send inline recipient profiles, which could change the time zone or attributes policy
  decides on; agents refer to people by external id;
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
wants to message), `category`, `importance`, `title`, `body` (the first 500 characters)
and `expires_at`:

```json
"en": {"title": "{{ data.agent_name }} needs your OK",
       "body": "To {{ data.recipient }}: {{ data.body }}",
       "url": "https://admin.example.com/review/{{ data.message_id }}"}
```
