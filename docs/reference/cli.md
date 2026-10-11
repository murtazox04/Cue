# CLI

```text
cuectl serve [--host 127.0.0.1] [--port 8000] [--workers 1] [--reload] [--migrate]
cuectl worker [--concurrency N] [--once]
cuectl mcp [--url URL] [--transport stdio|streamable-http] [--host 127.0.0.1] [--port 8765]
                                   MCP server for AI agents (see guides/ai-agents);
                                   key from CUE_API_KEY, HTTP token from CUE_MCP_AUTH_TOKEN
cuectl check                      validate configuration and database connectivity;
                                  exits 1 on unsafe settings (listed under "warnings")
cuectl db upgrade [REVISION]      apply migrations (default: head)
cuectl db downgrade REVISION
cuectl db current
cuectl keys create NAME [-s SCOPE]... [--agent AGENT]
                                  print a new API key once, optionally bound to an
                                  agent; without -s: admin, or with --agent:
                                  events:write, messages:write, messages:read
cuectl keys list
cuectl keys revoke KEY_ID
cuectl jobs stats                 queued and dead jobs by kind
cuectl jobs retry-dead [--kind K]
                                  requeue dead jobs and reopen the events, messages,
                                  broadcasts and digests they gave up on
cuectl prune [--older-than DAYS] [--ledger-older-than DAYS] [--batch-size N]
                                  delete old events, finished messages, closed digests,
                                  decided approval requests, expired review links and
                                  dead jobs; the consent ledger only when asked.
                                  Deletes N rows (1000) per transaction
cuectl version                    (or cuectl --version)
```

`serve` binds to `127.0.0.1`; pass `--host 0.0.0.0` to listen on every interface (the
container image, Compose file, Helm chart and Render Blueprint do).

`keys create` without `-s` gives the key `admin`. With `--agent` it gives the key every
scope an agent key may hold instead: `events:write`, `messages:write` and
`messages:read`. Pass `-s` to narrow it further, for example
`cuectl keys create helper-key --agent helper -s events:write -s messages:read`. Agent
keys cannot hold `admin`, `recipients:read`, `recipients:write` or `approvals:decide`;
asking for one of them fails. See [API key scopes](#api-key-scopes).

`worker --once` processes every job that is due and exits — useful for cron-style
deployments and debugging. All commands read the same [configuration](configuration.md).

## API key scopes

| Scope | Grants |
|---|---|
| `events:write` | Ingest events, report engagement. |
| `messages:write` | Direct sends, cancel messages, ask for approvals. |
| `messages:read` | Read messages and events. |
| `recipients:write` | Create, update and erase recipients, addresses and preferences. |
| `recipients:read` | Read recipients. |
| `approvals:decide` | Approve or reject held messages and approval requests. Give it to the keys your people decide with. |
| `admin` | Everything, including templates, rules, categories, broadcasts and keys. |
