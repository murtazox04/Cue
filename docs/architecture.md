# Architecture

## Components

```mermaid
flowchart TB
    subgraph API["cuectl serve (FastAPI)"]
        R[Routers] --> S[Services]
    end
    subgraph W["cuectl worker"]
        Q[Job runner] --> S2[Services]
    end
    S -- "rows + jobs, one transaction" --> DB[(PostgreSQL / SQLite)]
    Q -- "claim (SKIP LOCKED)" --> DB
    S2 --> CH[Channel providers]
    S2 -.optional.-> AI[Pydantic AI]
    CH --> EXT[FCM · Twilio · SMTP · Telegram · webhooks]
```

| Package | Responsibility |
|---|---|
| `cue_notify.core` | Pure logic, no I/O: JSON Logic, templating, quiet hours, locales, rollout, content models. |
| `cue_notify.db` | SQLAlchemy models, portable types, Alembic migrations. |
| `cue_notify.queue` | Durable job queue on the application database. |
| `cue_notify.services` | Use cases: ingest, evaluate, compose, deliver, broadcast, engagement. |
| `cue_notify.channels` | Provider contract, built-in providers, plugin registry. |
| `cue_notify.ai` | Personalisation contract, guardrails and the Pydantic AI implementation. |
| `cue_notify.api` / `cue_notify.cli` | Thin adapters over services. |
| `cue_notify.runtime` | Long-lived resources (engine, HTTP client, channels, generator) shared by all entry points. |

## The life of an event

```mermaid
sequenceDiagram
    participant P as Your backend
    participant A as API
    participant D as Database
    participant W as Worker
    participant C as Channel
    P->>A: POST /v1/events
    A->>D: insert event + "process_event" job (1 tx)
    A-->>P: 202 Accepted
    W->>D: claim job
    W->>D: lock recipient, evaluate rules & policy,<br/>insert message + "deliver" job at send_at (1 tx)
    W->>D: claim "deliver" job when due
    W->>W: optional AI personalisation
    W->>C: send to each address
    W->>D: record outcome
```

## Design decisions

**The database is the queue.** Jobs are rows written in the same transaction as the data
they act on (transactional outbox), so there is no window where an event exists without
its processing job or a message without its delivery. Delays and quiet hours are a
future `run_at`, which survives restarts without a separate scheduler. Workers claim
with `SELECT … FOR UPDATE SKIP LOCKED` and a conditional update that doubles as a lease;
crashed workers' jobs reappear when the lease expires. This removes Redis and a broker
from the deployment at the cost of throughput in the low thousands of jobs per second
per database — far beyond what notification volumes need.

**One composer.** Rules, direct sends and broadcasts all build an *intent* and pass it
to `services.composer`. Policy cannot be bypassed by adding a new way to send.

**Policy is serialised per recipient.** Composing locks the recipient row, so caps and
cooldowns are exact under concurrency. On SQLite, `BEGIN IMMEDIATE` provides the same
guarantee by serialising writers.

**Decisions are data.** Suppressed and failed messages are stored with reasons; events
store per-rule outcomes. Debugging is a query, not a log search.

**Render early, personalise late.** Templates render when the message is composed (so
errors surface immediately and content is auditable); AI personalisation runs at send
time, after policy, so tokens are never spent on messages that will not be sent.

**Idempotency everywhere.** Events dedupe on `(source, idempotency_key)`; messages on
`dedup_key` (`event:<id>:<rule>`, `broadcast:<id>:<recipient>`, `api:<key>:<key>`); jobs
on `key`. Every job handler re-checks state before acting, so at-least-once execution
is safe.

**UUIDv7 keys.** Time-ordered identifiers keep indexes compact and double as stable
pagination cursors.

**Plugins over configuration flags.** Channels are classes discovered through entry
points, configured by name. Adding a provider never touches the core.
