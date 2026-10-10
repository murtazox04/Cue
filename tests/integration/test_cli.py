from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cue_notify.cli import app
from cue_notify.settings import get_settings

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)  # no stray .env / cue.toml
    monkeypatch.setenv("CUE_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("CUE_CHANNELS__LOG__PROVIDER", "console")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run(*args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.usefixtures("cli_env")
def test_cli_lifecycle() -> None:
    assert "upgraded" in _run("db", "upgrade")

    key = _run("keys", "create", "backend", "--scope", "events:write").splitlines()[0]
    assert key.startswith("ck_")
    listed = _run("keys", "list")
    assert "backend" in listed
    assert "events:write" in listed

    missing = runner.invoke(app, ["keys", "create", "bot", "--agent", "nope"])
    assert missing.exit_code == 1
    assert "agent nope does not exist" in missing.output

    summary = json.loads(_run("check"))
    assert summary["channels"] == {"log": "console"}
    assert summary["ai"] is None

    assert "processed 0 job(s)" in _run("worker", "--once")
    assert "requeued 0" in _run("jobs", "retry-dead")
    assert "deleted 0 message(s)" in _run("prune", "--older-than", "30")

    key_id = listed.split()[0]
    _run("keys", "revoke", key_id)
    assert runner.invoke(app, ["keys", "revoke", key_id]).exit_code == 1  # already revoked


@pytest.mark.usefixtures("cli_env")
def test_serve_can_migrate_first(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **_: started.append(app))
    output = _run("serve", "--migrate", "--port", "8123")
    assert "database upgraded to head" in output
    assert started == ["cue_notify.api.app:create_app"]
