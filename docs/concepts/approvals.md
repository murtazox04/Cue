# Approval requests

An agent about to do something a person should own (delete data, issue a refund, publish
a draft, email a thousand customers) can **ask first**. Cue finds the people, tells them
on their own channels with a one-time link, collects the decision and reports it back.

A [held message](agents.md#approval) is about something Cue itself sends. An approval
request is about an action that happens **outside** Cue.

```http
POST /v1/approvals
{
  "title": "Delete 1,204 inactive accounts?",
  "body": "Accounts with no login since 2024. This cannot be undone.",
  "reviewers": ["lead", "deputy"],
  "importance": "high",
  "expires_in_seconds": 3600,
  "metadata": {"workflow_id": "wf-42"}
}
```

| Field | Default | |
|---|---|---|
| `title` | — | What needs a decision (up to 200 characters). |
| `body` | `""` | Details the reviewer sees (up to 4,000 characters). |
| `reviewers` | — | External ids of the people who may decide (1–10). |
| `importance` | `high` | Capped by an agent's `max_importance`. |
| `expires_in_seconds` | `3600` | 1 minute to 7 days. |
| `metadata` | `{}` | Returned with the decision (up to 16 KB), e.g. a flow or workflow id. |

Any key with `messages:write` can ask. An agent key may only name the people in its
agent's `reviewers` list, so it cannot use approval notifications to reach anyone else,
and may have at most `approvals.max_pending_per_agent` (10) open requests at a time.
The response is the request, with `status: "pending"`.

## How reviewers hear about it

For each reviewer, Cue reports a `cue.approval_requested` event (source `cue`), the same
event used for [held messages](agents.md#telling-reviewers), with `data.kind` set to
`"action"`. One rule and template serve both:

```json
"en": {"title": "{{ data.agent_name }} needs your OK",
       "body": "{{ data.title }}",
       "url": "{{ data.review_url }}"}
```

`data` holds `kind`, `approval_id`, `agent`, `agent_name`, `title`, `body` (the first 500
characters), `importance`, `expires_at` and `review_url`. The review link has the same
guarantees as for messages:

- **Personal:** each reviewer gets their own link.
- **One-time:** the first decision closes all of them.
- **Safe to open:** opening the link changes nothing; only the buttons act.
- **Looked up by hash:** links are found by the token's SHA-256 hash. The token itself
  exists only in the reviewer's notification and the event behind it, and the API shows
  those to `admin` keys alone; other keys see `/review/[redacted]`.

A reviewer can add a comment; it is returned with the decision.

## Getting the decision

`status` becomes `approved`, `rejected` or `expired`. A request nobody decided on before
`expires_at` is **expired: treat it as a no**.

**Callback (recommended).** Configure where decisions go:

```toml
[approvals]
callback_url = "https://app.example.com/internal/cue/decisions"
# secret via CUE_APPROVALS__CALLBACK_SECRET
```

Cue `POST`s each decision there, signed with the `Cue-Signature` header like its
[webhooks](channels.md), so the SDKs' `verify_webhook` / `verifyWebhook` checks it.
Failed callbacks are retried with backoff.

```json
{"type": "approval.decided", "id": "…", "status": "approved", "title": "…",
 "agent": "ops-bot", "decided_by": "link:lead", "comment": "go ahead",
 "decided_at": "2026-10-11T08:00:00Z", "metadata": {"workflow_id": "wf-42"}}
```

The URL comes from your configuration, never from the request, so an agent cannot point
Cue at an internal address.

**Polling.** You can also call `GET /v1/approvals/{id}` until `status` is not `pending`.

**API decisions.** A person can also decide through the API, with a key that holds the
`approvals:decide` scope (or `admin`), for example from your own approval UI:

```http
POST /v1/approvals/{id}/approve  {"reason": "go ahead"}
POST /v1/approvals/{id}/reject   {"reason": "not this week"}
```

Agent keys can never hold `approvals:decide`, so they can never decide, and they see only
their own requests.

## From agents

**MCP.** The `request_approval` and `get_approval` tools let Claude, Cursor or your own
agent ask before acting. Its instructions tell it to treat anything but `approved` as no.

**SDKs.**

=== "Python"

    ```python
    asked = cue.request_approval(
        "Refund order 9?", ["lead"], body="Never arrived.", metadata={"order": "9"}
    )
    cue.get_approval(asked["id"])["status"]
    ```

=== "TypeScript"

    ```ts
    const asked = await cue.requestApproval({ title: "Refund order 9?", reviewers: ["lead"] });
    (await cue.getApproval(asked.id as string)).status;
    ```

### CrewAI Flows

`@human_feedback` waits for console input by default. `CueFeedbackProvider` asks the
reviewers through Cue instead and pauses the flow; CrewAI persists it. When the decision
arrives at your callback, resume the flow:

```python
import json

from crewai.flow import Flow, human_feedback, listen, start
from cue_client import Cue, verify_webhook
from cue_client.crewai import CueFeedbackProvider, resume_args

provider = CueFeedbackProvider(Cue(CUE_API_KEY, CUE_URL), reviewers=["lead"])


class PublishFlow(Flow):
    @start()
    @human_feedback(
        message="Publish this post?",
        emit=["approved", "rejected"],
        llm="gpt-4o-mini",
        provider=provider,
    )
    def draft(self):
        return "Cue 0.2 is out…"

    @listen("approved")
    def publish(self, result): ...


# The approvals.callback_url handler:
async def on_decision(body: bytes, signature: str):
    verify_webhook(body, signature, CALLBACK_SECRET)
    flow_id, feedback = resume_args(json.loads(body))
    await PublishFlow.from_pending(flow_id).resume_async(feedback)
```

The feedback reads `"Approved."` or `"Rejected. <comment>"`, which CrewAI's `emit` turns
into an outcome. Tested with CrewAI 1.15.

### Dapr Agents

A `DurableAgent` hook returning `RequireApproval` publishes an `ApprovalRequiredEvent`
and waits for an `ApprovalResponseEvent`. A small service subscribed to the request topic
hands it to Cue, and publishes Cue's decision back:

```python
from cue_client import AsyncCue, verify_webhook
from cue_client.dapr import approval_request, approval_response

cue = AsyncCue(CUE_API_KEY, CUE_URL)


@app.post("/approval-requests")  # Dapr subscription, request topic
async def on_request(cloud_event: dict):
    await cue.request_approval(**approval_request(cloud_event["data"], ["oncall"]))


@app.post("/cue/decisions")  # Cue's approvals.callback_url
async def on_decision(request: Request):
    body = await request.body()
    verify_webhook(body, request.headers.get("Cue-Signature"), CALLBACK_SECRET)
    await dapr.publish_event(
        "pubsub", "agent-approval-responses", json.dumps(approval_response(json.loads(body)))
    )
```

The request expires when Dapr's own `timeout_seconds` runs out, so nobody approves a
step the workflow has already denied. Checked against dapr-agents 1.0.9's event schemas.

### LangGraph

A node calls `interrupt(...)` and the graph pauses on its checkpointer. Hand the pending
interrupt to Cue and resume the thread with the decision:

```python
from langgraph.types import interrupt

from cue_client.langgraph import approval_request, resume


def delete_accounts(state):
    decision = interrupt({"title": "Delete 1,204 inactive accounts?", "body": "…"})
    if not decision["approved"]:
        return {"result": f"kept: {decision['comment']}"}
    ...


result = graph.invoke(inputs, config)
for pending in result.get("__interrupt__", []):
    cue.request_approval(**approval_request(pending, thread_id, ["lead"]))

# The approvals.callback_url handler, after verify_webhook(...):
thread_id, command = resume(json.loads(body))
graph.invoke(command, {"configurable": {"thread_id": thread_id}})
```

The node receives `{"approved", "status", "comment", "decided_by"}`. Tested with
LangGraph 1.2.

### Pydantic AI

Tools registered with `requires_approval=True` end a run with `DeferredToolRequests`. Ask
through Cue, keep the messages, and continue with the decisions:

```python
from pydantic_ai import Agent, DeferredToolRequests

from cue_client.pydantic_ai import approval_requests, deferred_results

agent = Agent(model, output_type=[str, DeferredToolRequests])


@agent.tool_plain(requires_approval=True)
def refund(order_id: str) -> str: ...


result = agent.run_sync("Refund order 9")
if isinstance(result.output, DeferredToolRequests):
    save(conversation_id, result.all_messages())
    for request in approval_requests(result.output, conversation_id, ["lead"]):
        cue.request_approval(**request)

# When every pending call is decided:
agent.run_sync(
    message_history=load(conversation_id),
    deferred_tool_results=deferred_results(decisions),
)
```

A rejected call reaches the model as a denial carrying the reviewer's comment. Tested with
Pydantic AI 2.55. Pydantic AI can also use Cue's MCP server directly
(`MCPServerStdio("uvx", ["cue-notify", "mcp"])`), which gives the agent the
`request_approval` tool.

