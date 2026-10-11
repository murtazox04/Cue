# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added
- A Helm chart (`deploy/helm/cue`), tested on a kind cluster in CI, and a Render
  Blueprint (`render.yaml`).
- `DATABASE_URL` is used when `CUE_DATABASE__URL` is not set, plain `postgres://` URLs
  are accepted, and `cuectl serve` listens on `PORT`, as hosting platforms expect.

### Fixed
- Migrations take a PostgreSQL advisory lock, so replicas that start together (and all
  run `db upgrade` or `serve --migrate`) wait for each other instead of failing on
  duplicate keys.

## [0.2.0] — 2026-10-11

### Added
- Approval requests (`POST /v1/approvals`): an agent or service asks people to approve
  an action outside Cue. Reviewers are told through the `cue.approval_requested` event
  with a one-time link; the decision is sent, signed, to `approvals.callback_url` and
  can be polled. Undecided requests expire. MCP tools `request_approval` and
  `get_approval`.
- Python SDK 0.2.0: `request_approval` / `get_approval`, a CrewAI Flows
  `CueFeedbackProvider`, and Dapr Agents event translation (`cue_client.dapr`).
  TypeScript SDK 0.2.0: `requestApproval` / `getApproval`.

## [0.1.2] — 2026-10-11

### Added
- Digests at a time of day in each recipient's time zone: `deliver_at: "08:00"`, with
  optional `days` (`["mon"]`) for weekly summaries.
- One-time review links: each reviewer of a held agent message gets `review_url` in the
  `cue.approval_requested` event, opening a hosted page to approve or reject it. Links
  expire with the message, close after the first decision, and are stored only as hashes.
- A CI job that brings up Docker Compose and walks through the getting-started guide.

### Security
- Agent keys now see only the messages and events they sent themselves, and cannot read
  or send digests. Before, an agent key with `messages:read` could read every message
  and event, including other senders' content.

### Fixed
- The getting-started guide configured only the push channel, so its push-then-SMS rule
  was rejected; the clone instructions also used the wrong directory name.

## [0.1.1] — 2026-10-11

### Added
- Screening for agents' messages (`[screening]`): before delivery, TypeSafe's Jev
  decision model judges urgency and whether a message pressures, deceives or leaks a
  secret. Risky messages wait for approval, overstated importance is lowered, and the
  answers are stored on the message (`screening`).
- A manual "Live checks" workflow that tests screening against TypeSafe's API.
- The SDKs are published: `pip install cue-notify-client` and `npm install cue-client`.

## [0.1.0] — 2026-10-10

First public release of Cue, the attention layer between software, AI agents and people.

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
- A throughput benchmark (`benchmarks/throughput.py`) and a performance guide.
- Agent `reviewers`: Cue reports a `cue.approval_requested` event for each reviewer when
  a message waits for approval, so your own rules and templates notify them.
- Python (`cue-notify-client`, httpx only) and TypeScript (`cue-client`, no dependencies) SDKs
  with webhook signature verification, tested in CI and released by tag.

### Security
- Agent keys can no longer review messages (also not another agent's), hold the
  `admin` scope, change people's preferences or rotate preference tokens.
- The hosted preference form is read with a size limit, and the unsubscribe
  confirmation page only names categories that exist.
- Agent keys cannot see preference tokens (redacted from recipients and message
  content), delete recipients, send inline profiles, cancel or report engagement on
  other senders' messages; events they report follow the agent's categories, budget and
  approval.
- A `send_at` in the past counts as now, so backdated sends cannot slip past frequency
  caps or agent budgets.
- Event and message `data` is limited to 16 KB.
- The MCP server refuses ids that are not UUIDs instead of resolving them into other API
  paths.
- Line breaks in e-mail subjects are collapsed, and messages that cannot be built as
  e-mail fail over instead of retrying.

[Unreleased]: https://github.com/murtazox04/Cue/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/murtazox04/Cue/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/murtazox04/Cue/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/murtazox04/Cue/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/murtazox04/Cue/releases/tag/v0.1.0
