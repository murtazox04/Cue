"""Reference checks for configuration objects (templates, categories, rules, broadcasts)."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.db.models import Category, Template
from cue_notify.runtime import Runtime
from cue_notify.services.errors import NotFoundError, UnprocessableError


async def template(session: AsyncSession, key: str) -> Template:
    found = await session.scalar(select(Template).where(Template.key == key))
    if found is None:
        raise NotFoundError("template", key)
    return found


async def category(session: AsyncSession, key: str) -> Category:
    found = await session.get(Category, key)
    if found is None:
        raise NotFoundError("category", key)
    return found


def channels(runtime: Runtime, names: Iterable[str]) -> list[str]:
    """Validate channel names against the configured channels, keeping order."""
    ordered = list(dict.fromkeys(names))
    unknown = [name for name in ordered if name not in runtime.channels]
    if unknown:
        configured = ", ".join(sorted(runtime.channels)) or "none"
        raise UnprocessableError(
            f"unknown channel(s) {', '.join(unknown)}; configured channels: {configured}"
        )
    return ordered
