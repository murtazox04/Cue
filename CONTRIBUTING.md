# Contributing to Cue

Thanks for helping! Bug reports, docs fixes, new channels and features are all welcome.

## Development setup

You need [uv](https://docs.astral.sh/uv/). Python is installed by uv if missing.

```bash
git clone https://github.com/murtazox04/Cue && cd cue
uv sync --all-extras          # creates .venv with every optional feature
uv run pre-commit install     # lint and format on commit
uv run cuectl db upgrade     # SQLite at ./cue.db
uv run cuectl keys create dev
uv run cuectl serve --reload
```

## Checks

The same commands CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright                 # strict mode
uv run pytest                  # SQLite
CUE_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/cue_test uv run pytest
```

Run the PostgreSQL suite for anything that touches queries, locking or the job queue —
SQLite and PostgreSQL differ exactly there.

## Guidelines

- **Keep the layers honest.** `cue_notify.core` is pure logic with no I/O. `cue_notify.services`
  implements use cases on top of the database. `cue_notify.api` and `cue_notify.cli` are thin
  adapters. Channels and AI providers are plugins behind small interfaces.
- **Every send goes through the composer** (`services/composer.py`). New ways of sending
  must not bypass policy.
- **Handlers must be idempotent.** Jobs are delivered at least once.
- **Schema changes need a migration.** Generate one with
  `uv run alembic -c alembic.ini revision --autogenerate -m "…"`, then read and tidy it.
- **Tests come with the change.** Bug fixes start with a failing test.
- **User-visible changes** get a line in `CHANGELOG.md` and, when relevant, docs.

## Adding a channel

See [docs/guides/custom-channels.md](docs/guides/custom-channels.md). Channels for widely
used services are welcome in-tree; niche ones work great as separate packages registered
through the `cue.channels` entry point.

## Commit messages

Use the imperative mood and explain *why* in the body when it is not obvious
(`Fix cooldown counting messages from other rules`). Conventional-commit prefixes
(`feat:`, `fix:`…) are welcome but not required.
