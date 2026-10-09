.PHONY: install lint format typecheck test test-postgres check docs serve

install:
	uv sync --all-extras --group docs
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run pyright

test:
	uv run pytest --cov

test-postgres:
	CUE_TEST_DATABASE_URL=$${CUE_TEST_DATABASE_URL:-postgresql+asyncpg://cue:cue@localhost:5432/cue_test} uv run pytest

check: lint typecheck test

docs:
	uv run mkdocs serve

serve:
	uv run cuectl serve --reload --migrate
