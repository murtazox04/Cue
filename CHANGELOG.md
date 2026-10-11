# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.3.1] — 2026-10-11

A security release: upgrade if agents or other less-trusted services hold API keys.

### Security
- Agent keys can no longer create or change recipient profiles and addresses through
  `/v1/recipients` (only inline profiles were refused before). An agent with
  `recipients:write` could otherwise redirect someone's messages, including password
  resets from other senders, or rewrite attributes its approval condition reads.
- An agent's events always have the source `agent:<key>`, so it cannot claim another
  producer's idempotency keys and suppress their events. Event names starting with
  `cue.` and the source `cue` are reserved: forged approval notifications are refused.
- Review links are shown only to `admin` keys; other keys see `/review/[redacted]` in
  events and message content, so a read-only key can no longer decide.
- Keys of a disabled agent are refused on every endpoint, not only on sends.
- Agents may have at most `approvals.max_pending_per_agent` (10) open approval requests,
  so approval notifications cannot be used to bypass the attention budget.
- Request bodies over `api.max_body_bytes` (1 MiB; 64 MiB for broadcasts) are refused
  with `413` before they are read; unauthenticated clients could exhaust memory before.
- Tokens in request paths (preference pages, review links, open tracking) are redacted
  in logs.
- Twilio STOP replies with empty parameters (`FromCity=`, common on inbound SMS) were
  rejected as forged; signatures now follow Twilio's algorithm exactly. A junk
  signature header returns 403 instead of 500.
- The hosted preference page reads the form before locking, so a client sending its
  body slowly cannot hold the SQLite write lock.
- Two reviewers deciding at once no longer deadlock on PostgreSQL.
- AI guardrails: numbers keep their grouping ("1 250 000" cannot become "12 500.00"),
  dates cannot have their parts swapped, links without a scheme count as facts, and
  prompt data cannot close its tag.
- Screening sees HTML bodies (as text, with link targets), image links and push data,
  and treats out-of-range or NaN scores as errors (`on_error` applies). The review page
  lists every link the message carries, on every channel.
- Channel credentials no longer appear in configuration errors or `repr`.
- Static webhook headers go only to the configured URL, never to recipient-registered
  URLs; IPv6 addresses that embed IPv4 (NAT64, 6to4) are refused for those URLs.
- E-mail addresses must be a single mailbox; a list would have copied messages past
  preferences and caps.
- `/readyz` no longer reports channel names or database error types; metrics count at
  most 200 distinct event names.
- `cuectl prune` also deletes decided approval requests, expired review links and dead
  jobs; `--ledger-older-than` prunes the consent ledger when you choose to.
- Helm: no service-account token is mounted, the ingress exposes only the API, token
  pages and docs (not `/metrics`), and an optional NetworkPolicy.
- Python SDK: a non-ASCII signature header is rejected instead of raising `TypeError`.

### Supply chain
- Every GitHub Action is pinned to a commit SHA; checkouts don't keep credentials;
  release jobs don't use caches; `mcp-publisher` is pinned and checksum-verified.
- Container images get signed build provenance (`gh attestation verify`) and an SBOM;
  PyPI uploads carry PEP 740 attestations. Base images are pinned by digest.
- CodeQL, OpenSSF Scorecard, `pip-audit`, `npm audit` and zizmor run in CI; Dependabot
  covers both SDKs and Compose.
- `SECURITY.md` describes the trust model and how to verify releases.

## [0.3.0] — 2026-10-11

### Added
- Python SDK 0.3.0: `cue-hook` for OpenHands and Claude Code hooks; webhook verification
  for FastAPI (`cue_client.fastapi`) and Django (`cue_client.django`); LangGraph (`cue_client.langgraph`) and Pydantic AI
  (`cue_client.pydantic_ai`) bridges for approval requests, tested end to end with
  LangGraph 1.2 and Pydantic AI 2.55.
- US texting rules (`[compliance] us_texting_rules`): marketing texts wait for federal
  and state legal hours in the recipient's time zone, and state caps of 3 a day apply.
  Categories have a `marketing` flag; recipients a `region` (ISO 3166, e.g. `US-FL`).
- STOP and START replies: `POST /v1/inbound/sms` for any provider and a verified Twilio
  webhook. STOP mutes texts and unsubscribes from optional categories; START restores
  texts.
- Opt-in records (`POST /v1/recipients/{id}/consent`): the exact disclosure, its hash,
  method, source, IP address and user agent go into the consent ledger, with an optional
  expiry after which messages are suppressed (`consent_expired`).
- Consent provenance: every preference change records how it was made (IP address and
  user agent for the hosted page and one-click unsubscribe, keyword and number for
  replies, API key for your backend).
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

[Unreleased]: https://github.com/murtazox04/Cue/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/murtazox04/Cue/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/murtazox04/Cue/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/murtazox04/Cue/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/murtazox04/Cue/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/murtazox04/Cue/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/murtazox04/Cue/releases/tag/v0.1.0
