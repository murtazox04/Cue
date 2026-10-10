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
| `pool_size` / `max_overflow` | `10` / `10` | Per process (PostgreSQL). |
| `echo` | `false` | Log SQL. |

## `api`

| Key | Default | |
|---|---|---|
| `cors_origins` | `[]` | Origins allowed to call the API from a browser. |
| `docs` | `true` | Serve `/docs`, `/redoc` and `/openapi.json`. |
| `root_path` | `""` | When mounted under a path prefix behind a proxy. |
| `public_url` | none | Public base URL (with any path prefix). Enables [preference and unsubscribe links](../concepts/preferences.md). |
| `product_name` | none | Shown on the hosted preference page. |

## `worker`

| Key | Default | |
|---|---|---|
| `embedded` | `false` | Run the worker inside the API process. |
| `concurrency` | `16` | Concurrent jobs per worker process. |
| `poll_interval` | `1.0` | Seconds between polls when idle. |
| `lease_seconds` | `300` | How long a claimed job is hidden from other workers. |
| `max_attempts` | `10` | Attempts before a job is marked dead. |
| `retry_base_seconds` / `retry_max_seconds` | `5` / `3600` | Exponential backoff with jitter. |
| `shutdown_timeout` | `30` | Seconds to finish in-flight jobs on shutdown. |

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
