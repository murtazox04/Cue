"""Throughput benchmark: how many events Cue ingests and decides per second.

Runs the real application in-process (HTTP through ASGI, the real job queue, the full
policy path) with a delivery connector that does nothing, so the numbers measure Cue
itself rather than a provider.

    CUE_BENCH_DATABASE_URL=postgresql+asyncpg://user@host/cue_bench \\
        uv run python benchmarks/throughput.py --events 20000 --workers 4

The database is migrated and must be empty (use a throwaway database). Without
``CUE_BENCH_DATABASE_URL`` a temporary SQLite file is used.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from typing import ClassVar

import httpx
from alembic import command
from sqlalchemy import func, select

from cue_notify import __version__
from cue_notify.api.app import create_app
from cue_notify.channels.base import Channel, ChannelConfig, Delivery, Receipt
from cue_notify.core.enums import MessageStatus, Scope
from cue_notify.db.migrations import alembic_config
from cue_notify.db.models import Message
from cue_notify.jobs import create_worker
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys
from cue_notify.settings import DatabaseSettings, PolicySettings, Settings, WorkerSettings


class NullConfig(ChannelConfig):
    pass


class NullChannel(Channel[NullConfig]):
    """Accepts every delivery instantly."""

    provider: ClassVar[str] = "null"
    config_model: ClassVar[type[ChannelConfig]] = NullConfig

    async def send(self, delivery: Delivery) -> Receipt:
        return Receipt(provider_message_id=str(delivery.message_id))


def _settings(url: str, concurrency: int) -> Settings:
    return Settings(
        environment="production",
        database=DatabaseSettings(url=url),
        worker=WorkerSettings(concurrency=concurrency),
        policy=PolicySettings(quiet_hours=None),
    )


def _runtime(url: str, concurrency: int) -> Runtime:
    null = NullChannel("push", NullConfig(), httpx.AsyncClient())
    return Runtime.create(_settings(url, concurrency), channels={"push": null})


def _work(url: str, concurrency: int) -> int:
    """One worker process: run every due job, then exit."""

    async def main() -> int:
        runtime = _runtime(url, concurrency)
        try:
            return await create_worker(runtime, name=f"bench-{os.getpid()}").run_until_idle()
        finally:
            await runtime.aclose()

    return asyncio.run(main())


async def ingest(url: str, events: int, recipients: int, concurrency: int) -> float:
    """Load templates, rules and recipients, then post ``events``; returns the seconds the
    events took."""
    runtime = _runtime(url, concurrency)
    async with runtime.transaction() as session:
        _, raw = await api_keys.create(session, "benchmark", [Scope.ADMIN])
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cue.bench",
        headers={"Authorization": f"Bearer {raw}"},
        timeout=60,
    ) as client:
        await client.post(
            "/v1/templates",
            json={
                "key": "shipped",
                "locales": {"en": {"title": "Shipped", "body": "Order {{ data.order }} shipped"}},
            },
        )
        await client.post(
            "/v1/rules",
            json={
                "key": "shipped",
                "name": "Order shipped",
                "event": "order.shipped",
                "template": "shipped",
                "category": "transactional",
                "channels": ["push"],
            },
        )
        semaphore = asyncio.Semaphore(concurrency)

        async def put_recipient(n: int) -> None:
            async with semaphore:
                await client.put(
                    f"/v1/recipients/user-{n}",
                    json={"addresses": [{"channel": "push", "value": f"token-{n}"}]},
                )

        await asyncio.gather(*(put_recipient(n) for n in range(recipients)))

        async def send(n: int) -> None:
            async with semaphore:
                response = await client.post(
                    "/v1/events",
                    json={
                        "name": "order.shipped",
                        "recipient": f"user-{n % recipients}",
                        "data": {"order": n},
                        "idempotency_key": f"order-{n}",
                    },
                )
                response.raise_for_status()

        started = time.perf_counter()
        await asyncio.gather(*(send(n) for n in range(events)))
        elapsed = time.perf_counter() - started
    await runtime.aclose()
    return elapsed


async def count_sent(url: str) -> int:
    runtime = _runtime(url, 1)
    async with runtime.session() as session:
        sent = await session.scalar(
            select(func.count(Message.id)).where(Message.status == MessageStatus.SENT)
        )
    await runtime.aclose()
    return int(sent or 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--recipients", type=int, default=1_000)
    parser.add_argument("--concurrency", type=int, default=32, help="Jobs per worker process.")
    parser.add_argument("--workers", type=int, default=1, help="Worker processes.")
    args = parser.parse_args()
    url = os.environ.get("CUE_BENCH_DATABASE_URL")
    if url is None:
        url = f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/bench.db"
    command.upgrade(alembic_config(url), "head")  # runs its own event loop

    ingested = asyncio.run(ingest(url, args.events, args.recipients, args.concurrency))
    started = time.perf_counter()
    with ProcessPoolExecutor(args.workers, mp_context=get_context("spawn")) as pool:
        jobs = sum(pool.map(_work, [url] * args.workers, [args.concurrency] * args.workers))
    processed = time.perf_counter() - started
    sent = asyncio.run(count_sent(url))

    backend = "PostgreSQL" if url.startswith("postgresql") else "SQLite"
    print(
        f"Cue {__version__} on {backend}, Python {platform.python_version()}, {os.cpu_count()} CPUs"
    )
    print(f"events {args.events}, recipients {args.recipients}, ", end="")
    print(f"workers {args.workers} x {args.concurrency} jobs")
    print(f"ingest (1 API process): {args.events / ingested:7.0f} events/s")
    print(f"decide and deliver:     {args.events / processed:7.0f} events/s ({jobs} jobs)")
    print(f"messages sent: {sent}")


if __name__ == "__main__":
    main()
