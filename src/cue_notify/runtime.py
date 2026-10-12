"""The runtime: long-lived resources shared by the API, the worker and the CLI."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from cue_notify import __version__
from cue_notify.ai import ContentGenerator, create_generator
from cue_notify.ai.screening import Screener, create_screener
from cue_notify.channels.registry import AnyChannel, ChannelRegistry
from cue_notify.db.session import SessionFactory, create_engine, create_session_factory
from cue_notify.observability import Metrics
from cue_notify.services.health import ChannelHealth
from cue_notify.settings import Settings


@dataclass(slots=True)
class Runtime:
    settings: Settings
    engine: AsyncEngine
    session: SessionFactory
    http: httpx.AsyncClient
    channels: ChannelRegistry
    generator: ContentGenerator | None
    metrics: Metrics
    screener: Screener | None = None
    health: ChannelHealth = field(default_factory=lambda: ChannelHealth(0, 60.0))

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        channels: dict[str, AnyChannel] | None = None,
        generator: ContentGenerator | None = None,
        screener: Screener | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> Runtime:
        """Build the runtime.

        ``channels``, ``generator`` and ``screener`` override configuration (tests, embedding).
        """
        # Each running job holds a connection; a smaller pool would open and close
        # connections constantly under load.
        engine = create_engine(settings.database, default_pool_size=settings.worker.concurrency + 4)
        client = http or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"User-Agent": f"cue/{__version__}"},
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        registry = (
            ChannelRegistry(channels)
            if channels is not None
            else ChannelRegistry.from_settings(settings.channels, client)
        )
        return cls(
            settings=settings,
            engine=engine,
            session=create_session_factory(engine),
            http=client,
            channels=registry,
            generator=generator if generator is not None else create_generator(settings.ai),
            metrics=Metrics(settings.metrics),
            screener=screener
            if screener is not None
            else create_screener(settings.screening, client),
            health=ChannelHealth(
                settings.delivery.breaker_failures, settings.delivery.breaker_cooldown_seconds
            ),
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncSession]:
        """A session inside a transaction that commits on success and rolls back on error."""
        async with self.session() as session, session.begin():
            yield session

    async def aclose(self) -> None:
        await self.channels.aclose()
        if self.generator is not None:
            await self.generator.aclose()
        await self.http.aclose()
        await self.engine.dispose()
