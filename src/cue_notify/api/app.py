"""ASGI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from cue_notify import __version__
from cue_notify.api import problems
from cue_notify.api.deps import RuntimeDep
from cue_notify.api.routers import (
    api_keys,
    broadcasts,
    categories,
    digests,
    events,
    health,
    messages,
    recipients,
    rules,
    templates,
)
from cue_notify.jobs import create_worker
from cue_notify.observability import configure_logging
from cue_notify.runtime import Runtime
from cue_notify.settings import Settings, get_settings

logger = logging.getLogger(__name__)

DESCRIPTION = """
Cue turns **events** from your product into **ready-to-send notifications** that respect
each person's preferences, time zone and attention, and hands them to the delivery you
plug in — your own service through a signed webhook, or a bundled connector.

Authenticate with `Authorization: Bearer <api key>`.
"""

TAGS = [
    {"name": "events", "description": "Tell Cue what happened; rules decide what to send."},
    {"name": "messages", "description": "Direct sends, message history and engagement."},
    {"name": "digests", "description": "Events grouped into one message per window."},
    {"name": "recipients", "description": "Profiles, addresses and preferences."},
    {"name": "templates", "description": "Localised, channel-aware message content."},
    {"name": "rules", "description": "Which event sends which template, to which channels."},
    {"name": "categories", "description": "Delivery policy: caps, quiet hours, opt-outs."},
    {"name": "broadcasts", "description": "One message to many recipients, in batches."},
    {"name": "api keys", "description": "Scoped credentials for the API."},
    {"name": "health", "description": "Probes for orchestrators and load balancers."},
]


def _report_worker_exit(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.critical("embedded worker crashed", exc_info=task.exception())


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    """Build the application. Pass ``runtime`` to share resources (tests, embedding)."""
    settings = runtime.settings if runtime else (settings or get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        owned = runtime is None
        current = runtime or Runtime.create(settings)
        app.state.runtime = current
        stop = asyncio.Event()
        worker_task: asyncio.Task[None] | None = None
        if settings.worker.embedded:
            worker_task = asyncio.create_task(create_worker(current).run(stop), name="worker")
            worker_task.add_done_callback(_report_worker_exit)
        logger.info(
            "cue %s ready",
            __version__,
            extra={
                "channels": ",".join(current.channels) or "-",
                "embedded_worker": bool(worker_task),
            },
        )
        try:
            yield
        finally:
            stop.set()
            if worker_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
            if owned:
                await current.aclose()

    if runtime is None:
        configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Cue",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        root_path=settings.api.root_path,
        docs_url="/docs" if settings.api.docs else None,
        redoc_url="/redoc" if settings.api.docs else None,
        openapi_url="/openapi.json" if settings.api.docs else None,
        license_info={"name": "MIT", "identifier": "MIT"},
    )
    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
    problems.install(app)

    v1 = APIRouter(prefix="/v1")
    for module in (
        events,
        messages,
        digests,
        recipients,
        templates,
        rules,
        categories,
        broadcasts,
        api_keys,
    ):
        v1.include_router(module.router)
    v1.include_router(messages.tracking)
    app.include_router(v1)
    app.include_router(health.router)

    if settings.metrics:

        @app.get("/metrics", include_in_schema=False)
        async def metrics(runtime: RuntimeDep) -> Response:  # pyright: ignore[reportUnusedFunction]
            if not runtime.metrics.enabled:
                return Response(status_code=404)
            body, content_type = runtime.metrics.render()
            return Response(body, media_type=content_type)

    return app
