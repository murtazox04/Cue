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

## Kubernetes (Helm)

The chart in [`deploy/helm/cue`](https://github.com/murtazox04/Cue/tree/main/deploy/helm/cue)
runs the API and the worker against your PostgreSQL:

```bash
helm install cue ./deploy/helm/cue \
  --set database.existingSecret=cue-db \
  --set env.CUE_API__PUBLIC_URL=https://cue.example.com \
  --set ingress.enabled=true --set ingress.host=cue.example.com
kubectl exec deploy/cue-api -- cuectl keys create admin
```

- **Configuration:** structure (channels, quiet hours) goes in `config`, which becomes
  `cue.toml` in a ConfigMap. Secrets go in `secretEnv` or `envFrom`, from your own
  Secrets.
- **Migrations:** every pod runs `cuectl db upgrade` in an init container. Migrations take
  a PostgreSQL advisory lock, so replicas starting together wait for each other.
- **Security:** pods run as a non-root user with a read-only root filesystem, and drop
  all capabilities.
- **Tested in CI:** the chart is installed on a kind cluster on every change, and the
  getting-started guide runs against it.

## Render

[`render.yaml`](https://github.com/murtazox04/Cue/blob/main/render.yaml) is a Blueprint:
**New → Blueprint →** this repository. It creates the API (migrating on start), a worker
and a PostgreSQL database from the published image. Set `CUE_API__PUBLIC_URL` when Render
asks, then create a key in the API service's shell: `cuectl keys create admin`.

## Railway, Heroku and similar platforms

Cue reads the platform's conventions:

- **Database:** `DATABASE_URL` is used when `CUE_DATABASE__URL` is not set, and plain
  `postgres://` URLs are accepted.
- **Port:** `cuectl serve` listens on `PORT` when it is set.

On Railway:

1. Create a project from the image `ghcr.io/murtazox04/cue:0.3.0` (or this repository).
2. Add PostgreSQL, and give the service `DATABASE_URL=${{Postgres.DATABASE_URL}}`.
3. Set the start command to `cuectl serve --migrate`.
4. Add a second service from the same image with the start command `cuectl worker`.
