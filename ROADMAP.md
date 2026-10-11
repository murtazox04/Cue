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

## 0.2: Agents ask before they act (released 11 Oct 2026)

- [x] Approval requests for agents' actions, with CrewAI Flows and Dapr Agents bridges,
      MCP tools and signed decision callbacks
- [x] One-time, expiring approval links, so a reviewer can decide from the notification
- [x] Daily and weekly digests at a local time of day (`deliver_at`)
- [x] Agent keys see only what they sent
- [x] Screening agents' messages with a decision model (TypeSafe Jev), experimental

## 0.3: Compliance and integrations (October 2026)

- [x] Marketing categories with US federal and state texting hours and caps
- [x] STOP and START replies (FCC keywords), stricter than both the 2024 revoke-all rule and its replacement, FCC 26-67
- [x] Consent records with provenance (where and how consent was given)
- [x] Recording opt-ins with the exact disclosure shown (versioned), and consent expiry
- [x] Approval bridges for LangGraph and Pydantic AI
- [x] `cue-hook`: OpenHands and Claude Code tell people when they finish or wait for input
- [x] Deploy kits: Helm chart, Render Blueprint, Railway and Heroku conventions
- [x] Django and FastAPI webhook verification helpers
- [ ] SDKs for Go, Java, PHP and Ruby when a user asks for one (the HTTP API and the
      webhook signature are small enough to port in a day)

## 0.4: Visibility (November 2026)

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
