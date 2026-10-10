# Performance

Cue's cost per event is a handful of indexed database statements plus Python work, and it
scales by adding worker processes. Measure on your own hardware with the bundled
benchmark before sizing a deployment.

## What one event costs

| Step | Database statements | Transactions |
|---|---|---|
| Ingest (`POST /v1/events`) | 5: authenticate, find the recipient, insert the event and its job | 2 |
| Process the event (rules, policy, render, queue delivery) | about 11 | 3 |
| Deliver (claim, check preferences again, send, record) | about 11 | 3 |

There are no N+1 patterns: caps, cooldowns, fatigue and agent budgets are each one
indexed `COUNT` (or a `LIMIT n` read) per message.

## Run the benchmark

```bash
createdb cue_bench
CUE_BENCH_DATABASE_URL=postgresql+asyncpg://localhost/cue_bench \
  uv run python benchmarks/throughput.py --events 20000 --workers 4
```

The benchmark runs the real application in-process: HTTP through ASGI, the real job
queue, the full policy path and template rendering. Delivery goes to a connector that
accepts everything instantly, so the numbers measure Cue rather than a provider. Use a
throwaway database.

## Reference numbers

These come from a deliberately modest machine: a 4-vCPU sandboxed cloud container, where
every system call is several times slower than on ordinary hardware (a bare `SELECT 1`
costs about 150 µs). PostgreSQL 16 ran on the same machine. Treat the numbers as a
floor; expect more on dedicated hardware.

| Setup | Ingest (1 API process) | Decide and deliver |
|---|---|---|
| 1 worker process × 16 jobs | 146 events/s | 56 events/s |
| 4 worker processes × 16 jobs | 154 events/s | 164 events/s |

That is roughly 14 million events a day end to end on 4 vCPUs.

## Scaling

- **Workers scale horizontally.** Run one `cuectl worker` per CPU core, on as many
  machines as you like. Jobs are claimed with `FOR UPDATE SKIP LOCKED`, so workers never
  block each other.
- **The API scales the same way**: `cuectl serve --workers N` or more replicas behind a
  load balancer.
- **Connection pool.** Each process keeps `worker.concurrency + 4` connections by
  default, so busy workers do not churn connections. Keep the total across all processes
  below PostgreSQL's `max_connections`, or put PgBouncer in front of it.
- **SQLite** serialises writes. It suits development and small installations, not high
  throughput.
