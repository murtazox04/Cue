"""Command-line interface: ``cuectl --help``."""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

import typer
from sqlalchemy import delete, func, select, update

from cue_notify import __version__
from cue_notify.core.enums import Scope
from cue_notify.core.time import utcnow
from cue_notify.observability import configure_logging
from cue_notify.runtime import Runtime
from cue_notify.settings import get_settings

app = typer.Typer(
    name="cuectl",
    help="Cue — the right message, at the right moment. Notification decision engine.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
keys_app = typer.Typer(help="API keys.", no_args_is_help=True)
jobs_app = typer.Typer(help="Background job maintenance.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(keys_app, name="keys")
app.add_typer(jobs_app, name="jobs")


def _with_runtime[T](fn: Callable[[Runtime], Awaitable[T]], *, quiet: bool = True) -> T:
    settings = get_settings()
    configure_logging("WARNING" if quiet else settings.log_level, settings.log_format)

    async def main() -> T:
        runtime = Runtime.create(settings)
        try:
            return await fn(runtime)
        finally:
            await runtime.aclose()

    return asyncio.run(main())


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "0.0.0.0",  # noqa: S104
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
    workers: Annotated[int, typer.Option(help="Worker processes.")] = 1,
    reload: Annotated[bool, typer.Option(help="Reload on code changes (development).")] = False,
) -> None:
    """Run the HTTP API."""
    import uvicorn

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
        str, typer.Option(envvar="CUE_API_KEY", help="API key the agent acts with.")
    ] = "",
    transport: Annotated[
        str, typer.Option(help="stdio (desktop agents) or streamable-http.")
    ] = "stdio",
    host: Annotated[str, typer.Option(help="Bind address for streamable-http.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for streamable-http.")] = 8765,
) -> None:
    """Run a Model Context Protocol server so AI agents can send notifications."""
    if not api_key:
        raise typer.BadParameter("an API key is required (--api-key or CUE_API_KEY)")
    try:
        from cue_notify import mcp_server
    except ImportError as exc:
        raise typer.BadParameter("install the MCP extra: pip install 'cue-notify[mcp]'") from exc
    if transport == "stdio":
        mcp_server.run(url, api_key)
    elif transport == "streamable-http":
        mcp_server.run(url, api_key, "streamable-http", host=host, port=port)
    else:
        raise typer.BadParameter("transport must be stdio or streamable-http")


@app.command("check")
def check_config() -> None:
    """Validate configuration and connectivity, then print a summary."""
    from sqlalchemy import text

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
        }

    typer.echo(json.dumps(_with_runtime(main), indent=2))


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
        list[Scope], typer.Option("--scope", "-s", help="Repeatable. Default: admin.")
    ] = [Scope.ADMIN],  # noqa: B006 — Typer requires a literal default
) -> None:
    """Create an API key and print it once."""
    from cue_notify.services import api_keys

    async def main(runtime: Runtime) -> str:
        async with runtime.transaction() as session:
            _, raw = await api_keys.create(session, name, scope)
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
        typer.echo(f"{key.id}  ck_{key.prefix}_…  {key.name:<20} {state:<8} {','.join(key.scopes)}")


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
    from cue_notify.db.models import Job

    async def main(runtime: Runtime) -> int:
        stmt = update(Job).where(Job.dead.is_(True)).values(dead=False, attempts=0, run_at=utcnow())
        if kind:
            stmt = stmt.where(Job.kind == kind)
        async with runtime.transaction() as session:
            return int(getattr(await session.execute(stmt), "rowcount", 0))

    typer.echo(f"requeued {_with_runtime(main)} job(s)")


@app.command()
def prune(
    older_than: Annotated[int, typer.Option(help="Retention in days.")] = 90,
) -> None:
    """Delete events and final messages older than the retention period."""
    from cue_notify.core.enums import EventStatus, MessageStatus
    from cue_notify.db.models import Event, Message

    async def main(runtime: Runtime) -> tuple[int, int]:
        cutoff = utcnow() - timedelta(days=older_than)
        async with runtime.transaction() as session:
            messages = await session.execute(
                delete(Message).where(
                    Message.created_at < cutoff,
                    Message.status.not_in((MessageStatus.QUEUED, MessageStatus.SENDING)),
                )
            )
            events = await session.execute(
                delete(Event).where(Event.received_at < cutoff, Event.status != EventStatus.PENDING)
            )
        return int(getattr(messages, "rowcount", 0)), int(getattr(events, "rowcount", 0))

    messages, events = _with_runtime(main)
    typer.echo(f"deleted {messages} message(s) and {events} event(s)")


def main() -> None:
    app()
