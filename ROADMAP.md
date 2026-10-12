# Roadmap

Cue's direction: send fewer, better notifications, decided in one place and explainable
afterwards, in front of whatever delivers them. The order below follows what people
running notifications in production ask for most. There are no dates. Open an issue or
a discussion to argue for a change in priorities.

## Now

- [ ] Decision explorer and admin UI: "why didn't this person get it?", rule and
      template editing with dry runs
- [x] Delivery feedback: bounces and complaints suppress automatically; health-based
      route fallback
- [x] More connectors: APNs, Amazon SES, Resend, Slack
- [ ] Multi-tenancy: tenant-scoped rules, templates, branding, preferences and keys
- [ ] OpenTelemetry traces and Grafana dashboards

## Next

- [ ] Headless in-app feed: API, server-sent events and a small React component
- [ ] Learned send times: deliver when each person usually engages
- [ ] Cross-sender arbitration: when many agents compete for one person, the most
      important message wins and the rest are merged or deferred

## Later

- [ ] 1.0: stable API and schema guarantees
- [ ] SDKs for Go, Java, PHP and Ruby when someone asks for one (the HTTP API and the
      webhook signature are small enough to port in a day)

## Shipped

See [CHANGELOG.md](CHANGELOG.md) for details.

- **0.4.0** (11 Oct 2026): approvals need their own `approvals:decide` scope; agent keys
  cannot hold recipient scopes and events cannot rewrite profiles inline; signed
  preference tokens with `api.secret_key`; quiet hours and texting hours checked again
  at send time; caps counted around the send time; rate limits on the routes without a
  key; MCP over HTTP requires a token; `cuectl serve` binds to `127.0.0.1`;
  job timeouts, lease loss detection and working `jobs retry-dead`; batched `prune`;
  `cuectl --version`.
- **0.3** (11 Oct 2026): marketing categories with US federal and state texting hours
  and caps; STOP and START replies; consent records with provenance and expiry;
  LangGraph and Pydantic AI approval bridges; `cue-hook` for OpenHands and Claude Code;
  Helm chart, Render Blueprint, Railway and Heroku conventions; Django and FastAPI
  webhook verification helpers. 0.3.1 was a security release.
- **0.2** (11 Oct 2026): approval requests with CrewAI Flows and Dapr Agents bridges,
  MCP tools and signed callbacks; one-time, expiring review links; daily and weekly
  digests at a local time of day; agent keys see only what they sent; screening of
  agents' messages (optional, experimental).
- **0.1** (10 Oct 2026): rules, delivery policy, templates, broadcasts, pluggable
  delivery, optional AI copy, MCP server; digests and thread grouping; preference centre
  with one-click unsubscribe and consent ledger; importance and expiry; agents with
  budgets and approval; fatigue; Python and TypeScript SDKs; benchmark; PyPI, GHCR and
  MCP registry.

## Not planned

- **Sending infrastructure.** Cue prepares the message and decides on it; delivery
  stays yours to plug in.
- **A visual journey builder** for marketing campaigns. Tools like Customer.io do this
  well; Cue focuses on product notifications.
