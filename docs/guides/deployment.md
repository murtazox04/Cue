# Deployment

Cue is two processes from one image — the **API** (`cuectl serve`) and the
**worker** (`cuectl worker`) — and a database. Scale both horizontally.

## Container image

```bash
docker build -t cue .                               # everything, including AI
docker build -t cue --build-arg EXTRAS="postgres fcm metrics" .   # without AI
```

Released images are published as `ghcr.io/murtazox04/cue:<version>` (for example
`0.4.0`) and `ghcr.io/murtazox04/cue:<major>.<minor>` (for example `0.4`, which follows
patch releases). The versions are listed on the
[releases page](https://github.com/murtazox04/Cue/releases); pin an exact version in
production.

The image's entrypoint is `cuectl` and its default command is
`serve --host 0.0.0.0 --port 8000`. It runs as an unprivileged user, contains no source
tree, logs JSON and has a health check on `/healthz`. [`compose.yaml`](https://github.com/murtazox04/Cue/blob/main/compose.yaml)
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
  `/metrics` has no key; keep it off the public ingress (the Helm chart routes it only
  inside the cluster).
- **TLS and network.** Terminate TLS at your ingress. Without a key, only the probes,
  `/v1/track/*`, and the token pages (`/preferences/…`, `/review/…`, Twilio's webhook)
  are reachable.
- **Request size.** Bodies over `api.max_body_bytes` (1 MiB) are refused with `413`
  before they are read. Only `POST /v1/broadcasts` with an admin key may send up to
  `api.max_broadcast_body_bytes`. Set a similar limit at your proxy as well.
- **Rate limits.** The routes reachable without a key (`/preferences/…`, `/review/…`,
  `/v1/track/…`, `/v1/inbound/…`) are limited per client address, and so are failed
  logins: after too many `401`s on `/v1`, requests from that address without a valid
  key get `429` for a while (valid keys keep working). See
  [`api.rate_limit`](../reference/configuration.md#apirate_limit). Behind a proxy, list it
  in `api.trusted_proxies`, or every client shares the proxy's address. The limits are
  kept **in each process**: with several replicas or `--workers`, a client gets the limit
  once per process. Add a limit at your reverse proxy or ingress as well for
  multi-replica setups.
- **Bind address.** `cuectl serve` listens on `127.0.0.1` unless you pass
  `--host 0.0.0.0`; the image, Compose file, Helm chart and Render Blueprint pass it.
- **HTTPS links.** Set `api.public_url` to an `https://` URL. A plain-`http` URL (other
  than `localhost`) logs a warning at startup and fails `cuectl check`.
- **Secret key.** `api.public_url` requires `api.secret_key` (`CUE_API__SECRET_KEY`), which
  signs preference and unsubscribe links. Generate it once:
  `python -c 'import secrets; print(secrets.token_urlsafe(32))'` (at least 32
  characters), keep it in your secret store, and give **the same value to every API and
  worker process**: a link signed by a worker must verify on the API. Changing it later
  breaks every preference and unsubscribe link already sent, so rotate it only if it
  leaks. The Render Blueprint generates one for you.
- **Check before rollout.** `cuectl check` validates the configuration and the database
  connection and exits with status 1 when it finds an unsafe setting (an unsigned webhook,
  a plain-`http` public URL), listing it under `warnings`.
- **Logs.** Tokens in paths (preference pages, review links, open tracking) are replaced
  with `[redacted]` in Cue's logs. Your proxy's access log is yours to redact.
- **Retention.** Nothing is deleted unless you run `cuectl prune`; the worker does not
  run it for you. Schedule it daily with the same configuration as the API, for
  example from cron (`0 3 * * * cuectl prune --older-than 90`) or a Kubernetes
  CronJob running the image with the arguments `prune --older-than 90`. It deletes in
  batches of `--batch-size` rows (1000), each in its own transaction, so it can run
  while Cue is serving. The consent ledger is proof of consent, so it is kept unless you
  add `--ledger-older-than DAYS`.
- **Dead jobs.** `cuectl jobs stats` shows jobs that exhausted retries;
  `cuectl jobs retry-dead` requeues them after you fix the cause, and reopens the
  events, messages, broadcasts and digests they had marked failed.

## Delivery guarantees

Jobs are **at least once**. Every handler is idempotent with respect to Cue's own
state, but if a worker dies after a provider accepted a message and before recording it,
the message can be sent again. Webhook deliveries carry an `Idempotency-Key` header so
receivers can deduplicate.

## Kubernetes (Helm)

The chart in [`deploy/helm/cue`](https://github.com/murtazox04/Cue/tree/main/deploy/helm/cue)
runs the API and the worker against your PostgreSQL:

```bash
kubectl create secret generic cue-secrets \
  --from-literal=secret-key="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
helm install cue ./deploy/helm/cue \
  --set database.existingSecret=cue-db \
  --set env.CUE_API__PUBLIC_URL=https://cue.example.com \
  --set secretEnv.CUE_API__SECRET_KEY.secretName=cue-secrets \
  --set secretEnv.CUE_API__SECRET_KEY.key=secret-key \
  --set ingress.enabled=true --set ingress.host=cue.example.com
kubectl exec deploy/cue-api -- cuectl keys create admin
```

`env` and `secretEnv` apply to both the API and the worker, so they share the secret key.

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

1. Create a project from the image `ghcr.io/murtazox04/cue:<version>` (or this
   repository).
2. Add PostgreSQL, and give the service `DATABASE_URL=${{Postgres.DATABASE_URL}}`.
3. Set the start command to `cuectl serve --migrate --host 0.0.0.0`. Without
   `--host 0.0.0.0` it listens only inside its own container.
4. Add a second service from the same image with the start command `cuectl worker`.
5. If you set `CUE_API__PUBLIC_URL`, set the same `CUE_API__SECRET_KEY` on both services.
