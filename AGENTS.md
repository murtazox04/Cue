# AGENTS.md

Guidance for AI coding agents working **on Cue's own code**. To add Cue to another
application, use the integration skill in
[`plugins/cue/skills/integrate-cue/SKILL.md`](plugins/cue/skills/integrate-cue/SKILL.md)
and the index at [`docs/llms.txt`](docs/llms.txt).

## Commands

```bash
uv sync --all-extras                 # environment
uv run ruff check . && uv run ruff format --check .
uv run pyright                       # strict, src/ only
uv run pytest                        # SQLite
CUE_TEST_DATABASE_URL=postgresql+asyncpg://… uv run pytest   # PostgreSQL, as CI does
uv run mkdocs build --strict         # docs
```

All four checks must pass before a change is done. CI runs them on Python 3.12–3.14
with SQLite and PostgreSQL.

## Layout

| Path | What lives there |
|---|---|
| `src/cue_notify/core/` | Pure domain logic: content, policy, JSON Logic, templating, enums. No I/O. |
| `src/cue_notify/services/` | Use cases over the database: composer (the single policy path), delivery, events, digests, preferences, agents. |
| `src/cue_notify/api/` | FastAPI routers and schemas; thin, they call services. |
| `src/cue_notify/channels/` | Delivery connectors; one class per provider, registered by entry point. |
| `src/cue_notify/db/` | SQLAlchemy models and Alembic migrations (hand-written, one per change). |
| `src/cue_notify/queue.py` | The database-backed job queue. |
| `tests/unit`, `tests/integration` | Unit tests for core and connectors; integration tests through the HTTP API. |

## Conventions

- **One policy path.** Every message goes through `services/composer.py`; never send
  around it. New policy steps go into `decide()` in the documented order
  (`docs/concepts/policy.md`).
- **Migrations:** add a new revision for every schema change; never edit a released one.
  `tests/integration/test_migrations.py` checks the chain matches the models exactly.
  Enum columns are VARCHAR(16), so enum values must be 16 characters or fewer.
- **Errors:** services raise `ServiceError` subclasses (`services/errors.py`); the API
  turns them into RFC 9457 problem responses.
- **Tests** go through the public API wherever possible, using `tests/helpers.py`
  (`FakeChannel`, `FakeGenerator`, builders, `drain`). New tables must be added to
  `TABLES` in `tests/conftest.py` for PostgreSQL isolation.
- **Docs:** user-facing behaviour changes need the matching page under `docs/` and a
  `CHANGELOG.md` entry.
- **Style:** match the surrounding code; comments explain *why*, not *what*.
