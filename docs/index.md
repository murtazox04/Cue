# Cue

**The right message, at the right moment.** Cue is a self-hosted notification decision
engine. Your product reports what happened; Cue decides whether, what, when and over
which route to tell each person, renders a ready-to-send message, and hands it to the
delivery you plug in — your own service through a signed webhook, or a ready-made
connector.

```mermaid
flowchart LR
    A[Your backend] -- "POST /v1/events" --> B[Rules]
    B --> C[Policy<br/>preferences · caps · quiet hours]
    C --> D[Templates<br/>+ optional AI]
    D --> E{Your delivery}
    E --> F[Webhook → your service]
    E --> G[Connectors<br/>FCM · Twilio · SMTP · Telegram]
    E --> H[Your own plugin]
```

## Why Cue?

Most products start with `send_push(user, "Your order shipped")` sprinkled through the
codebase. It works until it doesn't: people get five notifications at 3 a.m., marketing
ignores unsubscribes, one dead device token fails a whole job, and nobody can answer
*"why didn't this user get the message?"*.

Cue moves that logic into one place:

- **Events, not sends.** Your code emits facts (`order.shipped`); rules decide what to send.
  Change messaging without redeploying your product.
- **Policy everywhere.** Every message — rule-triggered, direct or broadcast — passes the
  same checks: unsubscribes, muted channels, frequency caps, cooldowns and quiet hours in
  the recipient's own time zone.
- **Fewer, better messages.** [Digests](concepts/digests.md) turn a burst of events
  into one message ("Ann, Bo and 10 others commented"), automatically or summarised by
  AI, and thread ids let phones and inboxes stack related messages.
- **People in control.** A [preference centre](concepts/preferences.md), hosted or
  headless, one-click unsubscribe as Gmail and Yahoo require, and a consent ledger
  recording who changed what, and when.
- **Bring your own delivery.** Cue does not lock you into a provider. Point a route
  at your existing sending service with a signed webhook, use a bundled connector, or
  write your own in a few dozen lines.
- **Reliable routing.** Multiple devices per person, channel fallback (push → SMS),
  retries with backoff, automatic removal of dead tokens, at-least-once jobs that are
  safe to retry.
- **Answers, not guesses.** Every event records which rules matched and why; every message
  records its status and the reason it was suppressed or failed.
- **AI that cannot lie about money.** Optional personalisation through any major model
  provider, constrained to rephrasing: numbers and links must survive verbatim, and only
  attributes you allow-list are ever sent to the model.
- **Agent-ready.** An [MCP server](guides/ai-agents.md) lets AI agents send
  notifications and inspect delivery with scoped credentials.
- **Simple to run.** One Python service and PostgreSQL (or SQLite). No Redis, no message
  broker, no cron — the job queue lives in your database.

## Next steps

- [Getting started](getting-started.md) — running locally in five minutes.
- [Events and rules](concepts/events-and-rules.md) — the core model.
- [Deployment](guides/deployment.md) — production checklist.
