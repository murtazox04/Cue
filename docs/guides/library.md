# Use Cue as a library

`cue_notify.embedded` runs Cue's decision core inside your own process: no HTTP server,
no worker, no PostgreSQL. You ask it whether a notification should go out now, later,
in a digest or not at all, send it with the code you already have, and tell it what you
sent. State lives in one SQLite file (or in memory).

It suits a Django or Flask app, a cron job or a script that sends its own emails or
pushes and needs quiet hours, per-person caps and digests without running another
service.

## Install

```bash
pip install cue-notify
```

Python 3.12 or newer. The package also contains the server, so its dependencies are
installed too, but importing `cue_notify.embedded` loads none of them (no FastAPI,
SQLAlchemy or HTTP client) and starts nothing.

## Quick start

```python
from cue_notify.embedded import Gate

gate = Gate("sqlite:///cue.db", quiet_hours=("22:00", "08:00"), caps={"marketing": [("P1D", 1)]})
decision = gate.check("user-42", category="marketing", timezone="Europe/Berlin", key="sale-42")
if decision.action == "send":
    print("sending now")
    gate.sent(decision)
else:
    print(decision.action, decision.at, decision.reason)
for due in gate.due():
    print("sending", due.key)
    gate.sent(due)
```

Replace the `print` calls with your own email, push or SMS code.

`check()` returns a `Decision`. Call `due()` periodically (a cron entry, a Celery beat
task, a loop in a management command) to collect deferred messages and digests whose
time has come. [`examples/library/night.py`](https://github.com/murtazox04/Cue/tree/main/examples/library)
walks through a whole night, including the autumn clock change.

## How a decision is made

`check()` runs these steps in order and stops at the first one that decides:

1. **Duplicate key.** A `key` seen before returns the stored decision, unchanged, so
   retries and webhook redeliveries never notify twice.
2. **Digest.** In a category with a digest, the item joins the person's open batch
   (`digest`) and is delivered later as part of one message.
3. **Send time.** Now, or `at` if it is later, moved past quiet hours in the person's
   time zone.
4. **Cooldown.** The category already sent this person something within its cooldown
   (`suppress`, reason `cooldown`).
5. **Expiry.** The send time is at or after `expires_at` (`suppress`, reason `expired`).
6. **Frequency caps.** A rolling window of the category's caps is full (`suppress`,
   reason `frequency_cap: 1 per 1 day, 0:00:00`).
7. **Sender budget.** The `sender` used up its budget for this person, across all
   categories (`suppress`, reason `agent_budget: 3 per 1:00:00`).
8. **Send or defer.** `send` if the send time is now, otherwise `defer` until it.

Only `critical` messages in a category listed in `essential` skip quiet hours and caps,
as in the server, where they must be in a category people cannot unsubscribe from.
`critical` anywhere else is treated as `high`.

Deferred messages count towards caps and budgets at their send time from the moment they
are deferred, the way queued messages do in the server. So a burst of ten marketing
messages at 02:00 with a cap of one a day gives one `defer` until 08:00 and nine
`suppress`, not ten messages at 08:00.

## Decisions

| Field | Meaning |
|---|---|
| `id` | Identifier of the decision. |
| `action` | `Action.SEND`, `DEFER`, `SUPPRESS` or `DIGEST` (a `StrEnum`, so `== "send"` works). |
| `at` | When to send, timezone-aware UTC. For `defer` the time it waits for; for `digest` when the batch closes. |
| `reason` | Why: `None` for an immediate send, `quiet_hours`, `scheduled`, `digest`, `cooldown`, `expired`, `frequency_cap: …`, `agent_budget: …`. |
| `person`, `category`, `sender`, `key`, `data` | What you passed to `check()`. |
| `importance` | The importance the policy acted on (`critical` lowered to `high` outside `essential`). |
| `items`, `count` | For a digest batch from `due()`: the grouped decisions (latest `max_items`) and how many there were. |
| `sent_at` | Set once you called `sent()`. A repeated `key` returns it too, so you can tell a retry from a first attempt. |

The `frequency_cap`, `agent_budget`, `cooldown` and `expired` reasons are the same
strings the server stores as `status_reason`.

## Due messages

`due(now=None)` returns, sorted by time, every decision that should be sent now:

- **Deferred messages** whose time has come. Quiet hours are checked again with the
  person's current time zone (the last `timezone` passed to `check()` for them), so a
  person who moved from Berlin to New York overnight is not woken up at 02:00. A message
  that would now arrive after its `expires_at` is suppressed instead.
- **Digest batches** that closed. A batch goes through the same steps as a single
  message (send time, cooldown, caps, budget), so a batch closing during quiet hours
  waits for their end and a capped category can suppress a batch.

Each returned decision has `action == "send"` and is returned once. Send it and call
`sent(decision)`. If sending fails, keep the decision and retry it; it already counts
towards caps.

## API

```python
Gate(
    database="sqlite:///cue.db",          # or a path, or ":memory:" (the default)
    *,
    quiet_hours=("22:00", "08:00"),       # None disables quiet hours
    timezone="UTC",                       # for people without a known time zone
    caps={"marketing": [("P1D", 1), ("P7D", 3)]},
    cooldowns={"reminders": "PT1H"},
    budgets={"billing-bot": [("PT1H", 2)]},
    digests={"comments": {"window": "PT30M"}, "news": {"deliver_at": "08:00", "days": ["mon"]}},
    essential=["security"],
    clock=utcnow,                         # any callable returning the time, for tests
)
```

Periods are ISO 8601 durations (`PT1H`, `P1D`), seconds or `timedelta`. Caps, cooldowns
and digests are keyed by category, budgets by sender. Digest settings are the server's:
`window` or `deliver_at` (local time) with optional `days`, `send_after` and `max_items`.
Invalid settings raise `ValueError` when the `Gate` is created.

| Method | Returns | |
|---|---|---|
| `check(person, *, category, timezone=None, importance="normal", key=None, sender=None, at=None, expires_at=None, data=None)` | `Decision` | Decide and record. `data` must be JSON-serialisable; it comes back with the decision and in digest `items`. |
| `sent(decision)` | `Decision` | Record that you sent it. Raises `ValueError` for `suppress` and `digest` decisions. |
| `due(now=None)` | `list[Decision]` | Deferred messages and digest batches to send now. |
| `close()` | `None` | Close the database. `Gate` is also a context manager. |

`importance` is `low`, `normal`, `high` or `critical`. Naive datetimes are read as UTC.
An unknown `timezone` falls back to the default, as in the server.

The API is synchronous. From async code, run it in a thread:
`await asyncio.to_thread(gate.check, "user-42", category="orders")`.

## Storage

State is kept in tables prefixed `cue_` in the SQLite file, created on first use. Every
call runs in one `BEGIN IMMEDIATE` transaction, so several threads, or several processes
sharing the same file, never exceed a cap together. One `Gate` may be shared between
threads. Decisions are kept; delete old rows from `cue_decisions` if the file grows too
large for you.

## Relation to the server

The library calls the same policy functions as the server (`cue_notify.core.policy`
and `cue_notify.core.digest`): quiet hours with the daylight saving rules from
[quiet hours are harder than they look](../notes/quiet-hours.md), rolling caps, digest
closing times, importance breakthrough and expiry. A test checks that both compute the
same send time.

It covers the decision only. Not included, use the [server](deployment.md) for these:

- templates, localisation and AI wording;
- channels and connectors (you send the message yourself);
- recipients' preferences, the preference centre and unsubscribe links;
- agent approvals, screening and API keys;
- fatigue, engagement tracking, US texting hours and broadcasts;
- PostgreSQL and the HTTP, MCP and SDK interfaces.

Categories and senders are plain strings here. In the server the same settings live on
categories (caps, quiet hours, `allow_unsubscribe: false` for `essential`), rules
(cooldowns, digests) and agents (budgets), so moving to the server later means creating
those through its API.
