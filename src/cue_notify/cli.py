"""Command-line interface: ``cuectl --help``."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

import typer
from sqlalchemy import ColumnElement, and_, delete, func, select, update
from sqlalchemy.orm import InstrumentedAttribute

from cue_notify import __version__
from cue_notify.core.enums import Scope
from cue_notify.core.time import utcnow
from cue_notify.observability import configure_logging
from cue_notify.runtime import Runtime
from cue_notify.services.errors import ServiceError
from cue_notify.settings import get_settings, is_loopback_host

if TYPE_CHECKING:
    from cue_notify.db.base import Base

app = typer.Typer(
    name="cuectl",
    help="Cue: a self-hosted notification decision engine.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
keys_app = typer.Typer(help="API keys.", no_args_is_help=True)
jobs_app = typer.Typer(help="Background job maintenance.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(keys_app, name="keys")
app.add_typer(jobs_app, name="jobs")

API_KEY_ENV = "CUE_API_KEY"
MCP_BEARER_ENV = "CUE_MCP_AUTH_TOKEN"
MIN_MCP_TOKEN_LENGTH = 32
AGENT_DEFAULT_SCOPES = (Scope.EVENTS_WRITE, Scope.MESSAGES_WRITE, Scope.MESSAGES_READ)


def _with_runtime[T](fn: Callable[[Runtime], Awaitable[T]], *, quiet: bool = True) -> T:
    settings = get_settings()
    configure_logging("WARNING" if quiet else settings.log_level, settings.log_format)

    async def main() -> T:
        runtime = Runtime.create(settings)
        try:
            return await fn(runtime)
        finally:
            await runtime.aclose()

    try:
        return asyncio.run(main())
    except ServiceError as exc:
        typer.echo(f"error: {exc.detail}", err=True)
        raise typer.Exit(1) from exc


def print_version(requested: bool) -> None:
    if requested:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def options(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=print_version,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    pass


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def serve(
    host: Annotated[
        str, typer.Option(help="Bind address. Use 0.0.0.0 to listen on every interface.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.", envvar="PORT")] = 8000,
    workers: Annotated[int, typer.Option(help="Worker processes.")] = 1,
    reload: Annotated[bool, typer.Option(help="Reload on code changes (development).")] = False,
    migrate: Annotated[
        bool, typer.Option(help="Apply database migrations before starting.")
    ] = False,
) -> None:
    """Run the HTTP API."""
    import uvicorn

    if migrate:
        db_upgrade()

    uvicorn.run(
        "cue_notify.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        reload=reload,
        proxy_headers=True,
        log_config=None,
    )


@app.command()
def worker(
    concurrency: Annotated[
        int | None, typer.Option(help="Concurrent jobs (default from settings).")
    ] = None,
    once: Annotated[bool, typer.Option(help="Process due jobs, then exit.")] = False,
) -> None:
    """Run the background worker (event processing, deliveries, broadcasts)."""
    from cue_notify.jobs import create_worker

    settings = get_settings()
    if concurrency is not None:
        settings.worker.concurrency = concurrency
    configure_logging(settings.log_level, settings.log_format)

    async def main() -> None:
        runtime = Runtime.create(settings)
        try:
            job_worker = create_worker(runtime)
            if once:
                typer.echo(f"processed {await job_worker.run_until_idle()} job(s)")
                return
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await job_worker.run(stop)
        finally:
            await runtime.aclose()

    asyncio.run(main())


@app.command("mcp")
def mcp_server(
    url: Annotated[
        str, typer.Option(envvar="CUE_URL", help="Base URL of the Cue API.")
    ] = "http://127.0.0.1:8000",
    api_key: Annotated[
        str,
        typer.Option(
            envvar=API_KEY_ENV,
            help=f"API key the agent acts with. Prefer {API_KEY_ENV}: arguments show in ps.",
        ),
    ] = "",
    transport: Annotated[
        str, typer.Option(help="stdio (desktop agents) or streamable-http.")
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option(
            help=f"Bind address for streamable-http. Off loopback it needs {MCP_BEARER_ENV}."
        ),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for streamable-http.")] = 8765,
    auth_token: Annotated[
        str,
        typer.Option(
            envvar=MCP_BEARER_ENV,
            help="Bearer token streamable-http clients must send. Prefer the environment.",
        ),
    ] = "",
) -> None:
    """Run a Model Context Protocol server so AI agents can send notifications."""
    if not api_key:
        raise typer.BadParameter(f"an API key is required (--api-key or {API_KEY_ENV})")
    for value, option, env in (
        (api_key, "--api-key", API_KEY_ENV),
        (auth_token, "--auth-token", MCP_BEARER_ENV),
    ):
        if value and os.environ.get(env) != value:
            typer.echo(
                f"warning: {option} on the command line is visible to other users of this "
                f"machine (ps); set {env} instead",
                err=True,
            )
    from cue_notify import mcp_server  # imported lazily: it is only needed here

    if transport == "stdio":
        mcp_server.run(url, api_key)
        return
    if transport != "streamable-http":
        raise typer.BadParameter("transport must be stdio or streamable-http")
    if not auth_token and not is_loopback_host(host):
        raise typer.BadParameter(
            f"streamable-http on {host} would let anyone who can reach it act with the API "
            f"key; set {MCP_BEARER_ENV} (or --auth-token), or bind to 127.0.0.1",
            param_hint="--host",
        )
    if auth_token and len(auth_token) < MIN_MCP_TOKEN_LENGTH:
        raise typer.BadParameter(
            f"use at least {MIN_MCP_TOKEN_LENGTH} characters, e.g. from "
            "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`",
            param_hint=f"--auth-token / {MCP_BEARER_ENV}",
        )
    from cue_notify import mcp_http

    mcp_http.serve(url, api_key, host=host, port=port, token=auth_token or None)


@app.command("check")
def check_config() -> None:
    """Validate configuration and connectivity, then print a summary."""
    from sqlalchemy import text

    from cue_notify.config_warnings import configuration_warnings

    async def main(runtime: Runtime) -> dict[str, Any]:
        async with runtime.session() as session:
            await session.execute(text("SELECT 1"))
        settings = runtime.settings
        return {
            "version": __version__,
            "environment": settings.environment,
            "database": runtime.engine.url.render_as_string(hide_password=True),
            "channels": {name: type(ch).provider for name, ch in runtime.channels.items()},
            "ai": runtime.generator.model_name if runtime.generator else None,
            "embedded_worker": settings.worker.embedded,
            "warnings": configuration_warnings(settings, runtime.channels),
        }

    summary = _with_runtime(main)
    typer.echo(json.dumps(summary, indent=2))
    for warning in summary["warnings"]:
        typer.echo(f"error: {warning}", err=True)
    if summary["warnings"]:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- db


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument()] = "head") -> None:
    """Apply migrations (default: to the latest)."""
    from alembic import command

    from cue_notify.db.migrations import alembic_config

    command.upgrade(alembic_config(get_settings().database.url), revision)
    typer.echo(f"database upgraded to {revision}")


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument()]) -> None:
    """Revert migrations down to ``revision``."""
    from alembic import command

    from cue_notify.db.migrations import alembic_config

    command.downgrade(alembic_config(get_settings().database.url), revision)


@db_app.command("current")
def db_current() -> None:
    """Show the applied revision."""
    from alembic import command

    from cue_notify.db.migrations import alembic_config

    command.current(alembic_config(get_settings().database.url))


# --------------------------------------------------------------------------- keys


@keys_app.command("create")
def keys_create(
    name: Annotated[str, typer.Argument(help="Who uses the key, e.g. 'backend'.")],
    scope: Annotated[
        list[Scope] | None,
        typer.Option(
            "--scope",
            "-s",
            help="Repeatable. Default: admin, or with --agent: "
            + ", ".join(AGENT_DEFAULT_SCOPES)
            + ".",
        ),
    ] = None,
    agent: Annotated[
        str | None, typer.Option(help="Bind the key to an agent (its budget and approval apply).")
    ] = None,
) -> None:
    """Create an API key and print it once."""
    from cue_notify.services import agents, api_keys

    granted = scope or (list(AGENT_DEFAULT_SCOPES) if agent is not None else [Scope.ADMIN])

    async def main(runtime: Runtime) -> str:
        async with runtime.transaction() as session:
            if agent is not None:
                await agents.get(session, agent)
            _, raw = await api_keys.create(session, name, granted, agent_key=agent)
        return raw

    raw = _with_runtime(main)
    typer.echo(raw)
    typer.echo("Store this key now; it cannot be shown again.", err=True)


@keys_app.command("list")
def keys_list() -> None:
    """List API keys."""
    from cue_notify.db.models import ApiKey

    async def main(runtime: Runtime) -> list[ApiKey]:
        async with runtime.session() as session:
            return list((await session.scalars(select(ApiKey).order_by(ApiKey.created_at))).all())

    for key in _with_runtime(main):
        state = "revoked" if key.revoked_at else "active"
        agent = f"  agent={key.agent_key}" if key.agent_key else ""
        typer.echo(
            f"{key.id}  ck_{key.prefix}_…  {key.name:<20} {state:<8} {','.join(key.scopes)}{agent}"
        )


@keys_app.command("revoke")
def keys_revoke(key_id: Annotated[UUID, typer.Argument()]) -> None:
    """Revoke an API key."""
    from cue_notify.db.models import ApiKey

    async def main(runtime: Runtime) -> int:
        async with runtime.transaction() as session:
            result = await session.execute(
                update(ApiKey)
                .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
                .values(revoked_at=utcnow())
            )
            return int(getattr(result, "rowcount", 0))

    if not _with_runtime(main):
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- jobs & retention


@jobs_app.command("stats")
def jobs_stats() -> None:
    """Count queued and dead jobs by kind."""
    from cue_notify.db.models import Job

    async def main(runtime: Runtime) -> list[tuple[str, bool, int]]:
        async with runtime.session() as session:
            rows = await session.execute(
                select(Job.kind, Job.dead, func.count()).group_by(Job.kind, Job.dead)
            )
            return [(kind, dead, count) for kind, dead, count in rows]

    for kind, dead, count in _with_runtime(main):
        typer.echo(f"{kind:<20} {'dead' if dead else 'queued':<7} {count}")


@jobs_app.command("retry-dead")
def jobs_retry_dead(
    kind: Annotated[str | None, typer.Option(help="Only this kind.")] = None,
) -> None:
    """Requeue jobs that exhausted their retries."""
    from cue_notify import jobs
    from cue_notify.queue import Revival

    async def main(runtime: Runtime) -> Revival:
        async with runtime.transaction() as session:
            return await jobs.revive(session, kind)

    revival = _with_runtime(main)
    typer.echo(f"requeued {revival.jobs} job(s), reopened {revival.rows} row(s)")


@app.command()
def prune(
    older_than: Annotated[int, typer.Option(help="Retention in days.")] = 90,
    ledger_older_than: Annotated[
        int | None,
        typer.Option(
            help="Also delete consent-ledger entries older than this many days. They are "
            "proof of consent, so they are kept unless you ask (US claims run for 4 years)."
        ),
    ] = None,
    batch_size: Annotated[
        int, typer.Option(min=1, help="Rows deleted per transaction, so locks stay short.")
    ] = 1000,
) -> None:
    """Delete events, final messages, closed digests, decided approval requests and dead
    jobs older than the retention period."""
    from cue_notify.core.enums import ApprovalStatus, DigestStatus, EventStatus, MessageStatus
    from cue_notify.db.models import (
        Approval,
        Digest,
        Event,
        FeedbackReport,
        Job,
        Message,
        PreferenceChange,
        ReviewLink,
    )

    async def main(runtime: Runtime) -> dict[str, int]:
        cutoff = utcnow() - timedelta(days=older_than)
        targets: dict[str, tuple[type[Base], InstrumentedAttribute[Any], ColumnElement[bool]]] = {
            "message": (
                Message,
                Message.id,
                and_(
                    Message.created_at < cutoff,
                    Message.status.not_in((MessageStatus.QUEUED, MessageStatus.SENDING)),
                ),
            ),
            "event": (
                Event,
                Event.id,
                and_(Event.received_at < cutoff, Event.status != EventStatus.PENDING),
            ),
            "digest": (
                Digest,
                Digest.id,
                and_(Digest.created_at < cutoff, Digest.status != DigestStatus.OPEN),
            ),
            # Titles, bodies and metadata of requests can hold personal data too.
            "approval request": (
                Approval,
                Approval.id,
                and_(Approval.created_at < cutoff, Approval.status != ApprovalStatus.PENDING),
            ),
            "review link": (ReviewLink, ReviewLink.token_hash, ReviewLink.expires_at < cutoff),
            # Payloads and errors of jobs that gave up.
            "dead job": (Job, Job.id, and_(Job.dead.is_(True), Job.created_at < cutoff)),
            "feedback report id": (
                FeedbackReport,
                FeedbackReport.report_id,
                FeedbackReport.received_at < cutoff,
            ),
        }
        if ledger_older_than is not None:
            targets["consent-ledger entry"] = (
                PreferenceChange,
                PreferenceChange.id,
                PreferenceChange.created_at < utcnow() - timedelta(days=ledger_older_than),
            )
        return {
            name: await delete_in_batches(runtime, model, key, condition, batch_size)
            for name, (model, key, condition) in targets.items()
        }

    counts = _with_runtime(main)
    typer.echo("deleted " + ", ".join(f"{n} {name}(s)" for name, n in counts.items()))


async def delete_in_batches(
    runtime: Runtime,
    model: type[Base],
    key: InstrumentedAttribute[Any],
    condition: ColumnElement[bool],
    batch_size: int,
) -> int:
    deleted = 0
    while True:
        batch = select(key).where(condition).limit(batch_size).scalar_subquery()
        async with runtime.transaction() as session:
            result = await session.execute(delete(model).where(key.in_(batch)))
        removed = int(getattr(result, "rowcount", 0))
        deleted += removed
        if removed < batch_size:
            return deleted


def main() -> None:
    app()
