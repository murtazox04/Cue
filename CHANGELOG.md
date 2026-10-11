# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Upgrading
- `cuectl serve` now binds to `127.0.0.1` by default instead of `0.0.0.0`. The container
  image, `compose.yaml`, the Helm chart and the Render Blueprint pass `--host 0.0.0.0`
  explicitly; if you start `cuectl serve` yourself (Railway, Heroku, systemd…), add
  `--host 0.0.0.0` to keep accepting outside connections.
- `cuectl check` exits with status 1 when it finds an unsafe setting (listed under
  `warnings`), so CI or deploy scripts that run it may now fail until you fix it.
- Behind a reverse proxy, set `api.trusted_proxies` so rate limits apply per client
  instead of to the proxy's address.
- Give the keys your people approve with the `approvals:decide` scope, for example
  `cuectl keys create approvals-ui -s approvals:decide -s messages:read`. Keys with only
  `messages:write` now get `403` on `…/approve` and `…/reject`.
- Set `CUE_API__SECRET_KEY` (at least 32 random characters, the same value on every API
  and worker process) before upgrading. It is required whenever `api.public_url` is set,
  and without it `preference_token` and templates' `links.token` are `null` (guard them
  with `{% if links.token %}` as you would `links.unsubscribe`).
  Changing it later invalidates every preference link already sent.
- Migration `0013` hashes existing preference tokens in place. It cannot be undone:
  downgrading issues new random tokens, and links sent before then stop working.
- `preference_token` in `GET /v1/recipients/{id}` is a new, longer token after the
  upgrade; the old one still works until you rotate it, but it is no longer shown.
- Agent keys holding `recipients:*` or `approvals:decide` lose those scopes; recreate
  them without, and give profile updates to your backend's own key.
- Event producers that change people's region, attributes, addresses, locale or time
  zone inline need `recipients:write`.

### Security
- `POST /v1/broadcasts` accepts bodies up to `api.max_broadcast_body_bytes` (64 MiB) only
  with an admin key; the key is checked before the body is read. Anyone else gets the
  ordinary `api.max_body_bytes` limit, for announced and streamed bodies alike.
  Unauthenticated clients could make the server read 64 MiB per request before.
- Per-client rate limits (token buckets, in each process) on the routes reachable
  without a key — `/preferences/…`, `/review/…`, `/v1/preferences/…`, `/v1/track/…`,
  `/v1/inbound/…` — and on failed authentication: after too many `401`s on `/v1`, requests
  from that address get `429` with `Retry-After` until its budget refills, unless they
  carry a valid key, so guessing cannot lock out a legitimate client sharing the address.
  Configure with `api.rate_limit`; `X-Forwarded-For` is believed only from
  `api.trusted_proxies`.
- `cuectl mcp --transport streamable-http` refuses a non-loopback `--host` unless a bearer
  token is set (`CUE_MCP_AUTH_TOKEN` or `--auth-token`, at least 32 characters), and then
  requires `Authorization: Bearer <token>` on every request. Before, anyone who could
  reach the port acted with the server's API key.
- `cuectl mcp` warns when `--api-key` or `--auth-token` is given on the command line,
  where other users can read it in `ps`; use `CUE_API_KEY` and `CUE_MCP_AUTH_TOKEN`.
- A webhook channel with a fixed `url` and no `secret` logs a warning at startup and fails
  `cuectl check`, since receivers cannot verify its requests. Set `allow_unsigned = true`
  on the channel to accept that.
- A plain-`http` `api.public_url` (other than `localhost`) logs a warning at startup and
  fails `cuectl check`: preference, unsubscribe and review links carry tokens.
- Release images are built without the GitHub Actions cache that pushes to `main` also
  write, so a poisoned cache entry cannot reach a signed release. Manual release runs
  are refused outside the default branch.
- The docs workflow no longer leaves the token in the checkout; only the publishing step
  receives it.
- Approving or rejecting held messages and approval requests through the API needs the
  new `approvals:decide` scope (or `admin`). Any key with `messages:write` could decide
  before, including keys that only send. One-time review links keep working.
- The MCP server no longer has a `review_message` tool: AI clients ask for approval,
  people decide.
- Keys bound to an agent can no longer hold `recipients:read`, `recipients:write` or
  `approvals:decide` (nor `admin`, as before). Existing agent keys with these scopes are
  denied them on every request.
- Event producers can no longer rewrite profiles inline: `region`, `attributes` and
  `addresses` on an event or direct send need `recipients:write`, and changing a stored
  `locale` or `timezone` does too. Creating a recipient inline, filling an empty locale or
  time zone, or repeating the stored values still works with `events:write`.
- Review links are redacted for every non-admin key in every response that carries
  message content or digest items: engagement reports, approve and reject responses and
  `/v1/digests` showed them before.
- Wildcard rules (`*`, `cue.*`) no longer match Cue's own `cue.*` events; a rule or
  digest sees them only when it names the event exactly, so a catch-all rule cannot copy
  review links into other messages.
- Preference tokens are no longer stored. New tokens are signed with `api.secret_key`
  (HMAC-SHA256 over a random per-recipient id) and verified on use; tokens issued before
  this release are kept only as SHA-256 hashes, so links in e-mails already sent,
  including one-click unsubscribe, keep working until the token is rotated.
- Screening replaces preference, unsubscribe and review links and preference tokens with
  fixed placeholders before the message reaches the screening model.

### Changed
- `cuectl serve` binds to `127.0.0.1` by default (see Upgrading).
- `cuectl check` lists configuration warnings and exits with status 1 when there are any.
- The body limit and rate limiter honour `api.root_path` only when the request path
  starts with it.

### Fixed
- Quiet hours and US texting hours are checked again right before sending. A message
  that comes due at a time no longer allowed (preferences changed, a retry backoff, a
  slow queue) waits for the next allowed moment instead of going out.
- Approving a held message schedules it for the next allowed moment; it could be sent
  during quiet hours or outside legal texting hours before.
- Legal texting hours could move a marketing text into the recipient's quiet hours.
  Both are now applied together until neither moves the send time.
- Frequency caps, cooldowns, agent budgets and the state texting caps counted every
  message scheduled for later, so one send planned for next week could suppress
  today's. They now count the messages within the period around the send time.
- Quiet hours ending inside the repeated hour on the night clocks go back could resume
  before the message was due, sending it during the second pass of quiet hours.
- A daily digest opened during the repeated hour on the night clocks go back could be
  scheduled in the past.
- An address disabled by delivery (an unregistered push token, a blocked bot) came back
  to life whenever your app sent the same value inline with an event or message, or in
  a profile update. Only `POST /v1/recipients/{id}/addresses` re-enables it now.

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
