# AI agents (MCP)

Cue ships a [Model Context Protocol](https://modelcontextprotocol.io) server, so AI
agents — Claude, Cursor, your own agent framework — can notify people and check what
happened, through the same rules, policy and audit trail as the rest of your system.

```bash
pip install cue-notify
cuectl keys create support-agent -s events:write -s messages:read
```

If the agent sends messages itself, register it as an [agent](../concepts/agents.md)
and bind its key with `--agent`. It then gets a per-person attention budget, an
importance ceiling, and approval for anything you want a person to check first.

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
| `review_message` | Approve or reject a message waiting for review. | `messages:write` |
| `explain` | Dry-run: which rules would fire and why not. | `admin` |
| `preview_template`, `list_templates` | Discover and render templates. | `admin` |

Read tools are annotated read-only, so agents can call them without confirmation where
their host allows it; sending tools are not.

## Good practice

- **Least privilege.** Give agents `events:write` + `messages:read` and let your rules
  decide content. Grant `messages:write` only when the agent should choose templates.
- **Idempotency.** The server instructs agents to pass an idempotency key derived from
  the action that triggered the notification, so a retried tool call never notifies
  twice.
- **Policy still applies.** Caps, quiet hours and unsubscribes protect your users from an
  over-eager agent exactly as they do from a buggy cron job.
