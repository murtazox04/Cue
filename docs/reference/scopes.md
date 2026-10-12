# API key scopes

Every API key carries one or more scopes. `admin` implies all the others. Create keys
with `cuectl keys create NAME -s SCOPE…` or `POST /v1/api-keys`, and give each caller
only what it needs.

| Scope | Allows |
|---|---|
| `events:write` | `POST /v1/events`; `POST /v1/messages/{id}/engagement`. Inline recipient profiles may create a recipient, fill an empty `locale` or `timezone`, or repeat the stored values. |
| `messages:write` | `POST /v1/messages` (direct sends), `POST /v1/messages/{id}/cancel`, `POST /v1/digests/{id}/send`, `POST /v1/approvals` (ask people to approve an action). |
| `messages:read` | `GET /v1/messages[/{id}]`, `GET /v1/events[/{id}]`, `GET /v1/digests[/{id}]`, `GET /v1/approvals[/{id}]`. |
| `recipients:write` | Create, update and erase recipients, add and remove addresses, replace preferences, record consent, rotate preference tokens, `POST /v1/inbound/sms`. Also needed to set `region`, `attributes` or `addresses` inline on an event or send, or to change a stored `locale` or `timezone` that way. |
| `recipients:read` | `GET /v1/recipients[/{id}]` (including `preference_token`) and the consent ledger. |
| `approvals:decide` | Approve or reject held messages (`POST /v1/messages/{id}/approve`, `/reject`) and approval requests (`POST /v1/approvals/{id}/approve`, `/reject`). Give it to the keys your people decide with, not to the keys that send. |
| `admin` | Everything above, plus templates, rules (including the `/v1/rules/evaluate` dry run), categories, broadcasts, agents and API keys. Only admin keys may send broadcast bodies up to `api.max_broadcast_body_bytes` and see review links in message content; other keys see `/review/[redacted]`. |

A key without the scope a route needs gets `403`. The routes that need no key are listed
in the [HTTP API](api.md#endpoints) reference.

## Typical keys

```bash
cuectl keys create setup                                   # admin, for the operator
cuectl keys create backend -s events:write -s messages:write -s messages:read \
  -s recipients:write                                      # your application
cuectl keys create approvals-ui -s approvals:decide -s messages:read
cuectl keys create dashboard -s messages:read -s recipients:read
```

## Keys bound to an agent

A key created with `--agent` acts for an [agent](../concepts/agents.md): its budget,
importance ceiling, categories and approval policy apply to everything it sends.

- It can hold only `events:write`, `messages:write` and `messages:read`. Asking for
  `admin`, `recipients:read`, `recipients:write` or `approvals:decide` is refused with
  `422`, and an older key that holds one of them is denied it on every request.
- `cuectl keys create NAME --agent X` without `-s` gives it those three scopes; narrow
  them with `-s`, for example `-s events:write -s messages:read` for an agent that only
  reports events.
- It refers to people by external id only: inline profiles are refused with `403`.
- It sees only the messages and events it sent, and no digests.
- Its events always have the source `agent:<key>`; the `cue.*` event names and the
  source `cue` are reserved.
- It cannot cancel or report engagement on other senders' messages, report SMS replies,
  or change anyone's preferences.
- Keys of a disabled agent are refused on every endpoint.
