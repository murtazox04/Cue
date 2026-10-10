# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased] — 0.1.0

First public release of Cue, a self-hosted notification decision engine.

### Added
- Event ingestion with idempotency keys and inline recipient profiles.
- Rules: JSON Logic conditions, wildcard event names, priorities, `stop_processing`,
  delays, cooldowns and deterministic percentage rollouts, plus a dry-run endpoint.
- Digests: a rule can group matching events per recipient and grouping key for a
  window (or until `send_after` events) and send one message rendered from all of them.
  Smart digests let the AI layer summarise the group from allow-listed fields, falling
  back to the template. API: `/v1/digests`.
- A `thread` content field for device-side grouping (APNs `thread-id`, FCM data, e-mail
  `References`).
- Preference centre: a per-recipient preference token, a hosted preference page, a
  token-authenticated JSON API for your own UI (`/v1/preferences/{token}`), RFC 8058
  one-click unsubscribe with `List-Unsubscribe` headers from the SMTP connector and
  `links` in webhook payloads, template `links`, token rotation, and a consent ledger
  (`/v1/recipients/{id}/preferences/history`). Configure with `api.public_url`.
- Importance (`low`, `normal`, `high`, `critical`) mapped to iOS interruption levels,
  Android priority, silent Telegram messages and e-mail headers; `critical` breaks
  through quiet hours and caps only in categories people cannot leave.
- Expiry: `expires_at` on sends and `expires_after_seconds` on rules; stale messages are
  dropped and push TTLs shortened to match.
- Template actions (buttons) for push, Telegram, e-mail and webhooks.
- Agents: automated senders with a per-person attention budget (5 a day by default), an
  importance ceiling, allowed categories and an approval policy. Held messages wait in
  `pending_approval` until approved or rejected (`/v1/messages/{id}/approve`,
  `/reject`, MCP `review_message`), with the reviewer recorded. API keys can be bound
  to an agent (`--agent`).
- Fatigue: a category can pause low and normal messages to people who ignored its
  last N messages, probe again after the pause, and resume on any engagement.
- Agent-first distribution: the MCP server is part of the core package
  (`uvx cue-notify mcp`), an MCP registry manifest (`server.json`), a Claude Code
  plugin with an integration skill, `llms.txt` and `AGENTS.md`.
- `cuectl serve --migrate` applies migrations before starting.
- Categories carrying delivery policy: rolling frequency caps, quiet hours and
  unsubscribe rules. `transactional` and `marketing` are created by default.
- Localised Jinja2 templates (sandboxed, strict) with per-channel overrides, versioning
  and a preview endpoint.
- Pluggable delivery: a signed webhook connector for your own sending service,
  bundled connectors for Firebase Cloud Messaging, Twilio SMS, SMTP e-mail and Telegram,
  a console connector, and third-party connectors via entry points.
- Multi-address delivery, channel fallback, retry with backoff and automatic disabling
  of dead addresses.
- Broadcasts with resumable batches, pause/resume/cancel and audience filters.
- Optional AI copy personalisation through Pydantic AI (any provider) with fact
  guardrails, allow-listed context and a daily token budget.
- Delivery and engagement tracking, including a token-based endpoint for client apps.
- Scoped API keys, RFC 9457 problem responses, cursor pagination, Prometheus metrics.
- Hardening: SSRF-safe per-recipient webhooks, resource-bounded template sandbox and
  rule evaluator, race-free cancellation, job lease heartbeats, and failure states for
  events, messages and broadcasts whose jobs exhaust their retries.
- A transactional, database-backed job queue (no Redis required).
- `cuectl` CLI: `serve`, `worker`, `db`, `keys`, `jobs`, `prune`, `check`, `mcp`.
- A Model Context Protocol server so AI agents can send notifications and inspect
  delivery with scoped API keys.
