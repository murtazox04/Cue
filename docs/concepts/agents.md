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
| `enabled` | `true` | Set `false` to stop the agent at once; its keys are refused. |

Recipient preferences, category policy (caps, quiet hours, unsubscribes), importance
rules and expiry all still apply. An agent can only ever be *more* restricted than your
own backend.

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

Agent policy applies to direct sends (`POST /v1/messages`, the MCP `send_message` tool).
Events an agent reports go through your rules, which you already control.

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
count against the agent's budget, so an agent cannot flood the review queue. A reviewer,
a person or a supervising agent with its own key, then decides:

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
- **No self-review:** an agent can never approve or reject its own messages.

Over MCP, reviewers use the `review_message` tool.
