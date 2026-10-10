# CLI

```text
cuectl serve [--host 0.0.0.0] [--port 8000] [--workers 1] [--reload] [--migrate]
cuectl worker [--concurrency N] [--once]
cuectl mcp [--url URL] [--api-key KEY] [--transport stdio|streamable-http]
                                   MCP server for AI agents (see guides/ai-agents)
cuectl check                      validate configuration and database connectivity
cuectl db upgrade [REVISION]      apply migrations (default: head)
cuectl db downgrade REVISION
cuectl db current
cuectl keys create NAME [-s SCOPE]...   print a new API key once
cuectl keys list
cuectl keys revoke KEY_ID
cuectl jobs stats                 queued and dead jobs by kind
cuectl jobs retry-dead [--kind K]
cuectl prune [--older-than DAYS]  delete old events, finished messages and closed digests
cuectl version
```

`worker --once` processes every job that is due and exits — useful for cron-style
deployments and debugging. All commands read the same [configuration](configuration.md).

## API key scopes

| Scope | Grants |
|---|---|
| `events:write` | Ingest events, report engagement. |
| `messages:write` | Direct sends, cancel messages. |
| `messages:read` | Read messages and events. |
| `recipients:write` | Create, update and erase recipients, addresses and preferences. |
| `recipients:read` | Read recipients. |
| `admin` | Everything, including templates, rules, categories, broadcasts and keys. |
