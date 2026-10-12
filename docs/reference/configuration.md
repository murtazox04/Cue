# Configuration

Settings are read, highest precedence first, from:

1. environment variables — prefix `CUE_`, nested keys joined with `__`
   (`CUE_DATABASE__URL`, `CUE_CHANNELS__SMS__AUTH_TOKEN`);
2. a `.env` file in the working directory;
3. a TOML file: `./cue.toml`, or the path in `CUE_CONFIG_FILE` (which must exist).

A complete example lives in [`examples/cue.toml`](https://github.com/murtazox04/Cue/blob/main/examples/cue.toml).
Run `cuectl check` to validate a configuration.

## Top level

| Key | Default | |
|---|---|---|
| `environment` | `development` | `development`, `test` or `production`. |
| `log_level` | `INFO` | |
| `log_format` | `console` | `json` for log shippers (the Docker image defaults to `json`). |
| `metrics` | `false` | Expose Prometheus metrics at `/metrics` (needs the `metrics` extra). |

## `database`

| Key | Default | |
|---|---|---|
| `url` | `sqlite+aiosqlite:///./cue.db` | Use `postgresql+asyncpg://user:pass@host/db` in production. |
| `pool_size` / `max_overflow` | worker concurrency + 4 / `10` | Per process (PostgreSQL). |
| `echo` | `false` | Log SQL. |

## `api`

| Key | Default | |
|---|---|---|
| `cors_origins` | `[]` | Origins allowed to call the API from a browser. |
| `docs` | `true` | Serve `/docs`, `/redoc` and `/openapi.json`. |
| `root_path` | `""` | When mounted under a path prefix behind a proxy. |
| `public_url` | none | Public base URL (with any path prefix). Enables [preference and unsubscribe links](../concepts/preferences.md). Requires `secret_key`. |
| `secret_key` | none | Signs preference tokens, so the database never holds a working link. At least 32 characters; required with `public_url`. Set it as `CUE_API__SECRET_KEY`. See [below](#apisecret_key). |
| `product_name` | none | Shown on the hosted preference page. |
| `max_body_bytes` | `1048576` | Larger request bodies get `413` before anything reads them. |
| `max_broadcast_body_bytes` | `67108864` | The same limit for `POST /v1/broadcasts` with an admin key, whose audience can list a million ids. Without one, `max_body_bytes` applies. |
| `trusted_proxies` | `[]` | Addresses or networks (`10.0.0.0/8`) of your reverse proxies. Only requests from them have `X-Forwarded-For` believed when rate limiting. |

`public_url` with plain `http://` (other than `localhost`) logs a warning at startup and
fails `cuectl check`: the links it builds carry tokens.

### `api.secret_key`

Generate it once and keep it in your secret store:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

- **Required with `public_url`.** Without it Cue refuses to start. It can be set on its
  own too: then `preference_token` and templates' `links.token` work for your own
  preference screen, without hosted links.
- **Shared.** Give the same value to every API and worker process. A link signed by one
  process must verify on all the others.
- **Rotation.** Changing it invalidates every preference and unsubscribe link signed with
  the old value, including those in e-mails already delivered. Rotate it only if it
  leaks; to cut off one person's links, rotate their token instead
  (`POST /v1/recipients/{id}/preference-token`).
- **Unset.** Without it, `preference_token` and `links.token` are `null` and messages
  carry no preference or unsubscribe links.

## `api.rate_limit`

Per client address, in each process. Requests over a limit get `429` with `Retry-After`.

| Key | Default | |
|---|---|---|
| `enabled` | `true` | |
| `pages` | `{per_minute = 60, burst = 30}` | `/preferences/…`, `/review/…` and `/v1/preferences/…`. |
| `tracking` | `{per_minute = 600, burst = 300}` | `/v1/track/…`. |
| `inbound` | `{per_minute = 600, burst = 300}` | `/v1/inbound/…`, except status callbacks. |
| `feedback` | `{per_minute = 6000, burst = 3000}` | Delivery status callbacks and provider webhooks: `/v1/inbound/twilio/status`, `/v1/inbound/resend/…`, `/v1/inbound/ses/…`. |
| `failed_auth` | `{per_minute = 10, burst = 20}` | Responses `401` on `/v1`; once used up, requests from that address without a valid key get `429` on `/v1` until it refills. Valid keys are never blocked by it. |
| `max_clients` | `100000` | Addresses remembered; the least recently seen are forgotten first. |

## `delivery`

[Delivery feedback](../concepts/channels.md#delivery-feedback) and channel health.

| Key | Default | |
|---|---|---|
| `soft_bounce_limit` | `3` | Soft bounces in a row (no delivery between) that disable an address. |
| `breaker_failures` | `5` | Transient failures in a row after which a channel is skipped when another is planned. `0` turns this off. |
| `breaker_cooldown_seconds` | `60` | How long a failing channel is skipped. Then one message at a time tries it: a success closes the breaker, a failure skips it for another cooldown. |

## `worker`

| Key | Default | |
|---|---|---|
| `embedded` | `false` | Run the worker inside the API process. |
| `concurrency` | `16` | Concurrent jobs per worker process. |
| `poll_interval` | `1.0` | Seconds between polls when idle. |
| `lease_seconds` | `300` | How long a claimed job is hidden from other workers. |
| `max_attempts` | `10` | Attempts before a job is marked dead. A job whose worker died this many times is marked dead when it is next claimed. |
| `retry_base_seconds` / `retry_max_seconds` | `5` / `3600` | Exponential backoff with jitter. |
| `shutdown_timeout` | `30` | Seconds to finish in-flight jobs on shutdown; jobs still running are cancelled and handed back at once. |
| `job_timeout` | `120` | Seconds a job may run before it is cancelled; a timeout is retried like any other failure. |
| `max_defer_seconds` | `86400` | How long a job may keep deferring (rate limits, `Retry-After`) before it is marked dead. Deferrals do not count towards `max_attempts`. |

## `policy`

| Key | Default | |
|---|---|---|
| `default_locale` | `en` | For recipients without a locale. |
| `default_timezone` | `UTC` | For recipients without a valid time zone. |
| `quiet_hours` | `{start = "22:00", end = "08:00"}` | `null` disables the default window. |

## `channels.<name>`

`provider` selects the implementation; `enabled = false` turns a channel off. Every other
key belongs to the provider — see [Channels](../concepts/channels.md). Names are lowercase
letters, digits, `-` and `_`.

## `ai`

| Key | Default | |
|---|---|---|
| `enabled` | `false` | |
| `model` | — | Pydantic AI model id (`provider:model`). Required when enabled. |
| `fallback_models` | `[]` | Tried in order if the primary fails. |
| `instructions` | — | Brand voice for every message. |
| `temperature` | `0.7` | |
| `max_output_tokens` | `512` | |
| `timeout` | `20` | Seconds per request. |
| `daily_token_budget` | — | Tokens per UTC day; afterwards template text is used. |

## `screening`

Screens [agents'](../concepts/agents.md#screening) messages before delivery.

| Key | Default | |
|---|---|---|
| `enabled` | `false` | |
| `provider` | `typesafe` | TypeSafe's Jev decision model. |
| `api_key` | `TYPESAFE_API_KEY` | Required when enabled. |
| `base_url` | `https://api.typesafe.ai` | |
| `model` | `jev-latest` | |
| `timeout` | `5` | Seconds per request. |
| `hold_above` | `0.5` | Hold a message when any risk is at least this likely. |
| `min_confidence` | `0.6` | Lower importance only when the urgency answer is this sure. |
| `on_error` | `hold` | `hold` or `send` a message when screening fails. |

## `approvals`

Where decisions on [approval requests](../concepts/approvals.md) are reported.

| Key | Default | |
|---|---|---|
| `callback_url` | — | Receives a signed `POST` for each decision. |
| `callback_secret` | — | Signs callbacks (`Cue-Signature`). Required with `callback_url`. |
| `timeout` | `10` | Seconds per callback request. |
| `max_pending_per_agent` | `10` | Open requests one agent may have; more get `429`. Requests from keys not bound to an agent are not limited. |

## `compliance`

Legal safety nets for marketing text messages; see [Compliance](../guides/compliance.md).

| Key | Default | |
|---|---|---|
| `us_texting_rules` | `false` | Hold marketing texts to US legal hours and state caps. |
| `sms_channels` | `["sms"]` | Channels that send text messages. |
| `unknown_state` | `strict` | Rules for US recipients without a region: `strict` or `federal`. |

