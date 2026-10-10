# Deployment

Cue is two processes from one image — the **API** (`cuectl serve`) and the
**worker** (`cuectl worker`) — and a database. Scale both horizontally.

## Container image

```bash
docker build -t cue .                               # everything, including AI
docker build -t cue --build-arg EXTRAS="postgres fcm metrics" .   # without AI
```

The image runs as an unprivileged user, contains no source tree, logs JSON and has a
health check on `/healthz`. [`compose.yaml`](https://github.com/murtazox04/Cue/blob/main/compose.yaml)
is a complete reference stack.

## Production checklist

- **PostgreSQL.** SQLite serialises all writes and suits single-process installs only.
  Use `postgresql+asyncpg://…` and size `database.pool_size` × processes below the
  server's `max_connections`.
- **Migrations before rollout.** Run `cuectl db upgrade` as a one-off job/init container
  before starting new versions.
- **Separate workers** for anything beyond small installs; keep `worker.embedded=false`.
  Workers coordinate through the database (`FOR UPDATE SKIP LOCKED`), so any number can
  run. `worker.concurrency` is per process.
- **Graceful shutdown.** Workers finish in-flight jobs on `SIGTERM` within
  `worker.shutdown_timeout`; set your orchestrator's grace period higher.
- **Probes.** Liveness: `GET /healthz`. Readiness: `GET /readyz` (checks the database).
- **Metrics.** Set `metrics = true` and scrape `/metrics` (install the `metrics` extra).
- **TLS and network.** Terminate TLS at your ingress. Only `/v1/track/*`, `/healthz` and
  `/readyz` are meant to be reachable without a key.
- **Retention.** Schedule `cuectl prune --older-than 90` (cron/Kubernetes CronJob).
- **Dead jobs.** `cuectl jobs stats` shows jobs that exhausted retries;
  `cuectl jobs retry-dead` requeues them after you fix the cause.

## Delivery guarantees

Jobs are **at least once**. Every handler is idempotent with respect to Cue's own
state, but if a worker dies after a provider accepted a message and before recording it,
the message can be sent again. Webhook deliveries carry an `Idempotency-Key` header so
receivers can deduplicate.

## Kubernetes sketch

```yaml
apiVersion: batch/v1
kind: Job
metadata: { name: cue-migrate }
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: migrate
          image: ghcr.io/murtazox04/cue:0.1
          args: ["db", "upgrade"]
          envFrom: [{ secretRef: { name: cue } }]
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: cue-worker }
spec:
  replicas: 2
  selector: { matchLabels: { app: cue-worker } }
  template:
    metadata: { labels: { app: cue-worker } }
    spec:
      terminationGracePeriodSeconds: 60
      containers:
        - name: worker
          image: ghcr.io/murtazox04/cue:0.1
          args: ["worker"]
          envFrom: [{ secretRef: { name: cue } }]
```

The API deployment is the same with `args: ["serve", "--host", "0.0.0.0"]`, a
`containerPort: 8000` and the probes above.
