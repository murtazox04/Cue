from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from typer.testing import CliRunner

from cue_notify.cli import delete_in_batches
from cue_notify.core.enums import MessageStatus
from cue_notify.db.models import Message
from cue_notify.runtime import Runtime
from tests.helpers import create_recipient, create_template, drain, run_cli

SEND = {
    "recipient": "user-1",
    "template": "order-shipped",
    "channels": ["push"],
    "data": {"order_id": "1"},
}


async def sent_messages(client: httpx.AsyncClient, runtime: Runtime, count: int) -> None:
    await create_template(client)
    await create_recipient(client)
    for _ in range(count):
        assert (await client.post("/v1/messages", json=SEND)).status_code == 202
    await drain(runtime)


async def test_prune_deletes_in_bounded_batches(
    client: httpx.AsyncClient, runtime: Runtime, cli_runner: CliRunner
) -> None:
    await sent_messages(client, runtime, 5)
    long_ago = datetime.now(UTC) - timedelta(days=365)
    async with runtime.transaction() as session:
        await session.execute(update(Message).values(created_at=long_ago))

    output = await run_cli(cli_runner, "prune", "--older-than", "30", "--batch-size", "2")
    assert "deleted 5 message(s)" in output
    async with runtime.session() as session:
        assert await session.scalar(select(func.count(Message.id))) == 0


async def test_batched_delete_runs_one_transaction_per_batch(
    client: httpx.AsyncClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sent_messages(client, runtime, 5)
    transactions = 0
    original = Runtime.transaction

    def counting(self: Runtime) -> Any:
        nonlocal transactions
        transactions += 1
        return original(self)

    monkeypatch.setattr(Runtime, "transaction", counting)
    condition = Message.status == MessageStatus.SENT
    assert await delete_in_batches(runtime, Message, Message.id, condition, 2) == 5
    assert transactions == 3
