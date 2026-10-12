from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cue_notify import __version__
from cue_notify.cli import app
from cue_notify.db.models import Agent
from cue_notify.runtime import Runtime
from cue_notify.settings import get_settings
from tests.helpers import TEST_SECRET_KEY

runner = CliRunner()
HOOK = "https://hooks.example.com/cue"
TOKEN = "t" * 40


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


async def register_agent(key: str) -> None:
    runtime = Runtime.create(get_settings())
    try:
        async with runtime.transaction() as session:
            session.add(Agent(key=key, name=key))
    finally:
        await runtime.aclose()


@pytest.mark.usefixtures("cli_env")
def test_agent_keys_default_to_the_scopes_an_agent_may_hold() -> None:
    _run("db", "upgrade")
    asyncio.run(register_agent("helper"))

    _run("keys", "create", "helper-key", "--agent", "helper")
    _run("keys", "create", "helper-reader", "--agent", "helper", "-s", "messages:read")
    _run("keys", "create", "operator")
    rows = {line.split()[2]: line.split()[4] for line in _run("keys", "list").splitlines()}

    assert rows == {
        "helper-key": "events:write,messages:read,messages:write",
        "helper-reader": "messages:read",
        "operator": "admin",
    }
    refused = runner.invoke(app, ["keys", "create", "x", "--agent", "helper", "-s", "admin"])
    assert refused.exit_code == 1
    assert "cannot have the admin scope" in refused.output


@pytest.mark.usefixtures("cli_env")
def test_serve_can_migrate_first(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **_: started.append(app))
    output = _run("serve", "--migrate", "--port", "8123")
    assert "database upgraded to head" in output
    assert started == ["cue_notify.api.app:create_app"]


@pytest.mark.usefixtures("cli_env")
def test_serve_binds_to_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: calls.append(options))
    _run("serve")
    _run("serve", "--host", "0.0.0.0")
    assert [call["host"] for call in calls] == ["127.0.0.1", "0.0.0.0"]


@pytest.mark.parametrize(
    ("variables", "problem"),
    [
        ({"CUE_CHANNELS__HOOK__PROVIDER": "webhook", "CUE_CHANNELS__HOOK__URL": HOOK}, "'hook'"),
        (
            {
                "CUE_API__PUBLIC_URL": "http://cue.example.com",
                "CUE_API__SECRET_KEY": TEST_SECRET_KEY.get_secret_value(),
            },
            "public_url",
        ),
    ],
    ids=["unsigned-webhook", "plain-http-public-url"],
)
@pytest.mark.usefixtures("cli_env")
def test_check_fails_on_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], problem: str
) -> None:
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert problem in result.stderr


@pytest.mark.usefixtures("cli_env")
def test_check_accepts_explicitly_unsigned_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUE_CHANNELS__HOOK__PROVIDER", "webhook")
    monkeypatch.setenv("CUE_CHANNELS__HOOK__URL", HOOK)
    monkeypatch.setenv("CUE_CHANNELS__HOOK__ALLOW_UNSIGNED", "true")
    monkeypatch.setenv("CUE_API__PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setenv("CUE_API__SECRET_KEY", TEST_SECRET_KEY.get_secret_value())
    get_settings.cache_clear()
    assert json.loads(_run("check"))["warnings"] == []


@pytest.fixture
def mcp_serve(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def record(url: str, api_key: str, **options: object) -> None:
        calls.append({"url": url, "api_key": api_key, **options})

    monkeypatch.setattr("cue_notify.mcp_http.serve", record)
    monkeypatch.delenv("CUE_API_KEY", raising=False)
    monkeypatch.delenv("CUE_MCP_AUTH_TOKEN", raising=False)
    return calls


def test_mcp_http_refuses_other_hosts_without_a_token(mcp_serve: list[dict[str, object]]) -> None:
    args = ["mcp", "--transport", "streamable-http", "--host", "0.0.0.0"]
    result = runner.invoke(app, args, env={"CUE_API_KEY": "ck_a_b"})
    assert result.exit_code == 2
    assert "CUE_MCP_AUTH_TOKEN" in result.stderr
    assert mcp_serve == []

    short = runner.invoke(app, args, env={"CUE_API_KEY": "ck_a_b", "CUE_MCP_AUTH_TOKEN": "x"})
    assert short.exit_code == 2
    assert mcp_serve == []


def test_mcp_http_requires_the_configured_token(mcp_serve: list[dict[str, object]]) -> None:
    args = ["mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000"]
    env = {"CUE_API_KEY": "ck_a_b", "CUE_MCP_AUTH_TOKEN": TOKEN}
    result = runner.invoke(app, args, env=env)
    assert result.exit_code == 0, result.output
    assert "warning" not in result.stderr
    assert mcp_serve == [
        {
            "url": "http://127.0.0.1:8000",
            "api_key": "ck_a_b",
            "host": "0.0.0.0",
            "port": 9000,
            "token": TOKEN,
        }
    ]
    loopback = runner.invoke(
        app, ["mcp", "--transport", "streamable-http"], env={"CUE_API_KEY": "ck_a_b"}
    )
    assert loopback.exit_code == 0, loopback.output
    assert mcp_serve[-1]["host"] == "127.0.0.1"
    assert mcp_serve[-1]["token"] is None


def test_secrets_on_the_mcp_command_line_are_warned_about(
    mcp_serve: list[dict[str, object]],
) -> None:
    args = ["mcp", "--transport", "streamable-http", "--api-key", "ck_a_b"]
    result = runner.invoke(app, [*args, "--auth-token", TOKEN])
    assert result.exit_code == 0, result.output
    assert "--api-key on the command line" in result.stderr
    assert "--auth-token on the command line" in result.stderr
    assert len(mcp_serve) == 1


def test_version_flag_and_command_agree() -> None:
    flag = runner.invoke(app, ["--version"])
    command = runner.invoke(app, ["version"])
    assert flag.exit_code == command.exit_code == 0
    assert flag.output.strip() == command.output.strip() == __version__
