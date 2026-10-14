# Agents demo

Three agents message one on-call engineer, `dana`, through Cue. The script starts Cue
locally, sets everything up through the HTTP API and prints what Cue decided for each
message, next to the lines the `console` connector logs when it delivers one.

```bash
git clone https://github.com/murtazox04/Cue && cd Cue
uv run python examples/agents-demo/demo.py
```

It needs nothing else: Cue runs from the checkout with SQLite in a temporary directory,
the worker embedded in the API process (`CUE_WORKER__EMBEDDED=true`) and the `push`
route on the console connector (`CUE_CHANNELS__PUSH__PROVIDER=console`). The directory
and the server are removed when the script ends. `CUE_DEMO_PAUSE=0` skips the pauses
between lines.

## What it sets up

| | Configuration |
|---|---|
| `dana`, `lead` | Recipients with one `push` address each. `dana`'s time zone is picked so that it is night there when you run the script, inside the default quiet hours (22:00–08:00). |
| `incidents` category | `respect_quiet_hours: false`, `allow_unsubscribe: false`. |
| `dev-updates` category | Respects quiet hours. |
| `triage-bot` agent | `categories: ["incidents"]`, `max_importance: "high"`, `frequency_caps: [{"period": "PT1H", "limit": 3}]`. |
| `review-bot` agent | `categories: ["dev-updates"]`; reports `review.finished` events. |
| `billing-bot` agent | `approval: {"in": [{"var": "message.importance"}, ["high", "critical"]]}`, `reviewers: ["lead"]`. |
| `review-digest` rule | Groups `review.finished` events: `{"window": "PT1H", "send_after": 4}`. |
| `review-requests` rule | Turns `cue.approval_requested` into a notification to the reviewer. |

Each agent gets its own API key (`POST /v1/api-keys` with `agent`), and sends with it.

## What happens

1. **Budget and importance ceiling.** `triage-bot` sends five alerts. The first three are
   queued and the console connector delivers them, since `incidents` ignores quiet
   hours. The one sent as `critical` comes back as `high`, its agent's
   `max_importance`. The last two are `suppressed` with reason
   `agent_budget: 3 per 1:00:00`.
2. **Digest.** `review-bot` reports four events; each one's outcome is `digested`. The
   fourth reaches `send_after`, so the digest becomes one message, which waits for the
   end of quiet hours in `dana`'s time zone (`send_at` is 08:00 there).
3. **Quiet hours and approval.** `billing-bot`'s `normal` summary also waits for 08:00.
   Its `high` alert matches the approval condition and is stored as `pending_approval`.
   `lead` is notified, and the script plays the reviewer: it opens the one-time
   `review_url` from that notification and posts **Approve**, the same request the
   hosted page's button sends. The alert is then delivered, with `reviewed_by:
   "link:lead"`.

The summary at the end comes from `GET /v1/messages?recipient=dana`.

## Recording the GIF

[`agents-demo.tape`](agents-demo.tape) records `docs/assets/agents-demo.gif` with
[VHS](https://github.com/charmbracelet/vhs). From the repository root:

```bash
brew install vhs
vhs examples/agents-demo/agents-demo.tape
```
