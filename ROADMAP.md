# Roadmap

Cue's direction: **decide fewer, better notifications, and prove it.** Each release
moves one step toward that, ordered by what people running notifications in production
ask for most. Dates are targets, not promises. Open an issue or a discussion to argue
for a change in priorities.

## 0.1: Launch (Oct–Nov 2026)

- [x] Rules, delivery policy, templates, broadcasts, pluggable delivery, AI copy, MCP
- [x] Digests with optional AI summaries; thread grouping on devices and in inboxes
- [x] Preference centre: hosted page, JSON API, RFC 8058 one-click unsubscribe,
      consent ledger
- [ ] Python and TypeScript SDKs: event helpers and webhook signature verification
- [ ] Deploy kits: Helm chart, Railway and Render templates
- [ ] Django and FastAPI integration packages
- [ ] Published benchmark: events decided per second on a single PostgreSQL

## 0.2: Compliance (Dec 2026–Jan 2027)

- [ ] Message class on every rule (transactional or marketing) with policy presets
- [ ] Jurisdiction-aware quiet hours (US TCPA 8:00–21:00, Florida 8:00–20:00)
- [ ] "Revoke all" opt-out propagation ahead of the FCC rule taking effect on 31 Jan 2027
- [ ] Consent records with provenance (where and how consent was given)
- [ ] Agent governance over MCP: per-agent budgets, caps and an approval queue

## 0.3: Visibility (Feb–Apr 2027)

- [ ] Decision explorer and admin UI: "why didn't this person get it?", rule and
      template editing with dry runs
- [ ] Multi-tenancy: tenant-scoped rules, templates, branding, preferences and keys
- [ ] Delivery feedback: bounces and complaints suppress automatically; health-based
      route fallback
- [ ] OpenTelemetry traces and Grafana dashboards

## 1.0: Scale (May–Sep 2027)

- [ ] Headless in-app feed: API, server-sent events and a small React component
- [ ] Mapping priorities to iOS interruption levels and Android channels
- [ ] Stable API and schema guarantees, with long-term support releases

## Not planned

- **Sending infrastructure.** Cue prepares the message and decides on it; delivery
  stays yours to plug in.
- **A visual journey builder** for marketing campaigns. Tools like Customer.io do this
  well; Cue focuses on product notifications.
