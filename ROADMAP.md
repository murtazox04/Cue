# Roadmap

Cue's direction: **be the attention layer between software, AI agents and people:
decide fewer, better notifications, and prove it.** Each release
moves one step toward that, ordered by what people running notifications in production
ask for most. Dates are targets, not promises. Open an issue or a discussion to argue
for a change in priorities.

## 0.1: Launch (released 10 Oct 2026)

- [x] Rules, delivery policy, templates, broadcasts, pluggable delivery, AI copy, MCP
- [x] Digests with optional AI summaries; thread grouping on devices and in inboxes
- [x] Preference centre: hosted page, JSON API, RFC 8058 one-click unsubscribe,
      consent ledger
- [x] Importance levels mapped to iOS and Android, expiry, action buttons
- [x] Agents: per-person attention budgets, importance ceilings, human approval,
      reviewer notifications
- [x] Fatigue: back off from people who stopped paying attention
- [x] Agent-first distribution: MCP in core, Claude Code plugin, `llms.txt`, `AGENTS.md`
- [x] Python and TypeScript SDKs with webhook signature verification
- [x] Benchmark tool and performance guide
- [x] Published on PyPI and GHCR; MCP registry listing

## 0.2: Agents and compliance (October 2026)

- [x] Screening agents' messages with a decision model (TypeSafe Jev): hold risky ones,
      lower overstated importance
- [ ] Signed, expiring approval links, so a reviewer can approve from the notification itself
- [ ] Adapters for agent frameworks: Pydantic AI, LangGraph, CrewAI, OpenHands, Dapr Agents
- [ ] Message class on every rule (transactional or marketing) with policy presets
- [ ] Jurisdiction-aware quiet hours (US TCPA 8:00–21:00, Florida 8:00–20:00)
- [ ] "Revoke all" opt-out propagation, ahead of the FCC rule taking effect on 31 Jan 2027
- [ ] Consent records with provenance (where and how consent was given)
- [ ] Deploy kits: Helm chart, Railway and Render templates
- [ ] Django and FastAPI integration packages
- [ ] SDKs for Go, Java, PHP and Ruby when a user asks for one (the HTTP API and the
      webhook signature are small enough to port in a day)

## 0.3: Visibility (November 2026)

- [ ] Decision explorer and admin UI: "why didn't this person get it?", rule and
      template editing with dry runs
- [ ] Multi-tenancy: tenant-scoped rules, templates, branding, preferences and keys
- [ ] Delivery feedback: bounces and complaints suppress automatically; health-based
      route fallback
- [ ] More connectors: APNs, Amazon SES, Resend, Slack
- [ ] OpenTelemetry traces and Grafana dashboards

## 1.0: Scale (December 2026)

- [ ] Headless in-app feed: API, server-sent events and a small React component
- [ ] Learned send times: deliver when each person usually engages
- [ ] Cross-sender arbitration: when many agents compete for one person, the most
      important message wins and the rest are merged or deferred
- [ ] Stable API and schema guarantees, with long-term support releases

## Not planned

- **Sending infrastructure.** Cue prepares the message and decides on it; delivery
  stays yours to plug in.
- **A visual journey builder** for marketing campaigns. Tools like Customer.io do this
  well; Cue focuses on product notifications.
