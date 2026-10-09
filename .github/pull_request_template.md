## What does this change?

<!-- The problem and the approach. Link related issues. -->

## How was it tested?

<!-- New or updated tests; manual checks. -->

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pyright` pass
- [ ] `uv run pytest` passes (and against PostgreSQL if the change touches queries)
- [ ] Docs and `CHANGELOG.md` updated for user-visible changes
- [ ] New schema changes come with an Alembic migration
