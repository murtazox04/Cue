# AI agents (MCP)

Cue ships a [Model Context Protocol](https://modelcontextprotocol.io) server, so AI
agents — Claude, Cursor, your own agent framework — can notify people and check what
happened, through the same rules, policy and audit trail as the rest of your system.

Register the AI client as an [agent](../concepts/agents.md) (`POST /v1/agents` with
`"key": "support-agent"`), then, where Cue runs (for example
`docker compose exec api …`), give it a key bound to that agent with only the scopes
its tools need:

```bash
cuectl keys create support-agent-key --agent support-agent -s events:write -s messages:read
```

Without `-s`, a key created with `--agent` gets `events:write`, `messages:write` and
`messages:read`, which covers every tool an agent key can use. The MCP server itself
runs wherever the AI client does: `uvx cue-notify mcp`, or `cuectl mcp` after
`uv tool install cue-notify`.

A key bound with `--agent` gets a per-person attention budget, an importance ceiling and
approval for anything you want a person to check first. It can never hold `admin`,
`recipients:read`, `recipients:write` or `approvals:decide`: an AI client should not read
people's profiles or approve its own requests.

## Desktop agents (stdio)

Add Cue to your agent's MCP configuration, for example:

```json
{
  "mcpServers": {
    "cue": {
      "command": "cuectl",
      "args": ["mcp"],
      "env": {
        "CUE_URL": "https://cue.example.com",
        "CUE_API_KEY": "ck_…"
      }
    }
  }
}
```

## Remote agents (HTTP)

```bash
export CUE_API_KEY=ck_…
export CUE_MCP_AUTH_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
cuectl mcp --transport streamable-http --host 0.0.0.0 --port 8765
```

The endpoint is served at `/mcp`, and clients must send
`Authorization: Bearer <CUE_MCP_AUTH_TOKEN>`. Anyone holding that token acts with the
API key the server holds. Without a token the server only binds to a loopback address;
it refuses any other `--host`. Serve it over TLS (a reverse proxy) when it leaves the
machine.

Pass secrets through the environment: `--api-key` and `--auth-token` still work, but
command-line arguments are visible to other users in `ps`, so Cue prints a warning.

## Tools

| Tool | Does | Needs scope |
|---|---|---|
| `send_event` | Report something that happened; rules decide what to send. | `events:write` |
| `send_message` | Send a template directly over given channels. | `messages:write` |
| `get_message`, `list_messages` | Delivery status, content, engagement. | `messages:read` |
| `get_event` | Per-rule outcome of an event. | `messages:read` |
| `request_approval` | Ask people to approve an action before the agent takes it. | `messages:write` |
| `get_approval` | Poll an approval request for its decision. | `messages:read` |
| `explain` | Dry-run: which rules would fire and why not. | `admin` |
| `preview_template`, `list_templates` | Discover and render templates. | `admin` |

Read tools are annotated read-only, so agents can call them without confirmation where
their host allows it; sending tools are not.

There is no tool to approve or reject anything. Decisions belong to people: reviewers
decide from the one-time `review_url` in their notification, or through your own UI with
a key that holds `approvals:decide`.

## Good practice

- **Least privilege.** Bind the key to an agent and give it `events:write` +
  `messages:read`, letting your rules decide content. Grant `messages:write` only when the
  agent should choose templates or ask for approvals.
- **Idempotency.** The server instructs agents to pass an idempotency key derived from
  the action that triggered the notification, so a retried tool call never notifies
  twice.
- **Policy still applies.** Caps, quiet hours and unsubscribes protect your users from an
  over-eager agent exactly as they do from a buggy cron job.
