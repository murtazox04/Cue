# Quiet hours are harder than they look

"Don't send notifications at night" sounds like one `if` statement. In practice it is
a small scheduling problem with three traps: the time a message is *decided* is not the
time it is *sent*, two different "allowed hours" rules can fight each other, and the
clock repeats an hour once a year. This note walks through each one with the concrete
bug it produces and the fix Cue ships.

## 1. Deciding is not sending

The obvious implementation checks quiet hours when the notification is created:

```python
send_at = defer_past_quiet_hours(now, recipient_tz, quiet_hours)
enqueue(message, run_at=send_at)
```

That is correct for the instant it runs, and wrong for everything that happens after it:

- **Retries.** The provider returns a 5xx at 21:55. Exponential backoff schedules the
  next attempt for 22:40, inside quiet hours that started at 22:00.
- **Slow queues.** A broadcast queues 200,000 deliveries at 20:50. The workers drain them
  over twenty minutes; the tail goes out after 21:00.
- **Human approval.** A message from an AI agent waits for a person to approve it. They
  approve at 23:30, and the "release" code sets `send_at = max(send_at, now)`, which is
  23:30.

None of these paths is exotic, and each one sends a message at night that the policy said
it would not send. The fix is to treat quiet hours as a property of the *send*, not of the
*decision*: the delivery worker computes the next allowed moment again right before it
calls the provider, and if now is not allowed, it puts the job back with that moment as
its run time. Releasing an approved message goes through the same function. Cue has one
function, `allowed_send_time`, used by composing, delivering and releasing, so the three
can't drift apart.

## 2. Two windows that move each other

Marketing text messages in the US have a legal window on top of the user's own quiet
hours. Federal rules (47 CFR 64.1200(c)(1)) allow them between 8 a.m. and 9 p.m. at the
recipient's location, and several states are narrower; Florida, for example, is 8 a.m.
to 8 p.m. with at most three marketing texts a day.

Applying the two rules one after the other looks fine until they interact. Take quiet
hours of 21:30–10:00 and a Florida recipient at 21:15:

1. Quiet hours don't apply yet (21:15 is before 21:30), so the time stays 21:15.
2. The legal window is closed after 20:00, so the time moves to 08:00 the next day.
3. 08:00 is inside the user's quiet hours, but nothing checks again.

The message goes out at 08:00, two hours before the person asked to be disturbed. The
fix is to apply both constraints in a loop until neither moves the time:

```python
for _ in range(SEND_TIME_ROUNDS):
    moved = defer_past_quiet_hours(when, tz, quiet)
    if window is not None:
        moved = next_allowed(moved, tz, window)
    if moved == when:
        return when
    when = moved
```

The loop is bounded because some combinations never overlap (quiet hours that cover the
whole legal window). In that case the legal window wins: the message goes out at the
earliest legal moment rather than never.

## 3. The hour that happens twice

On the night clocks go back, 02:00–03:00 happens twice in Europe/Berlin. Python's aware
`datetime` objects that share the same `ZoneInfo` compare by *wall-clock time* and ignore
`fold`, the attribute that says which of the two 02:30s you mean:

```python
tz = ZoneInfo("Europe/Berlin")
first = datetime(2026, 10, 25, 2, 30, tzinfo=tz, fold=0)
second = datetime(2026, 10, 25, 2, 15, tzinfo=tz, fold=1)

first > second                                  # True  (wall clock)
first.astimezone(UTC) > second.astimezone(UTC)  # False (00:30 vs 01:15 UTC)
```

So "is the next 02:30 digest time still ahead of now?" answers yes during the second pass
of that hour, even though that 02:30 already happened 45 minutes earlier. A daily digest
scheduled that way closes in the past and goes out immediately with a single item; quiet
hours that end inside the repeated hour can resume before the message was due. The fix
is boring and total: convert both sides to UTC before comparing, everywhere a local
schedule is compared with now.

## 4. Waiting is not the same as being throttled

Once the delivery worker can say "not now, try at 07:00", it needs a way to put the job
back. A job queue usually already has one: the "retry later" path used for provider rate
limits. That path has sensible guards for throttling, such as capping each delay at an
hour and giving up after a day of deferrals, so one flaky provider can't hold a job
forever.

Those guards are wrong for planned waits. A message held until quiet hours end ten hours
from now would wake up every hour, defer again, and burn worker time. Worse, an approval
request that expires in a week would be given up on after a day of "deferring". Cue now
distinguishes the two: a planned wait keeps its full delay and never counts toward the
throttling limits, while a provider's `Retry-After` still does.

## 5. Caps count the future too

Frequency caps ("at most one marketing message per day") have the same shape of bug. The
first implementation counted every message in the category with `send_at` after
`now - period`, which includes messages scheduled for next week. One reminder planned
three days out silently used up today's budget. A cap should count only the messages in
a window of `period` around the candidate send time.

## The common thread

Every one of these bugs came from treating a time as known when it was decided. Retries,
approvals, queues, clock changes and future schedules all move the moment a message
actually reaches someone, and the rules have to hold at that moment. In Cue each of these
cases has a regression test that sets the clock and checks when the message goes out.

The code is MIT on [GitHub](https://github.com/murtazox04/Cue); the
[delivery policy](../concepts/policy.md) page lists the rules as they work today.
