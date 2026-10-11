"""Recipient profiles, addresses and preferences."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.ids import preference_token_id, uuid7
from cue_notify.core.policy import RecipientPreferences
from cue_notify.core.time import utcnow
from cue_notify.db.dml import insert_ignore
from cue_notify.db.models import Address, Recipient
from cue_notify.services.errors import ForbiddenError, NotFoundError


class AddressIn(BaseModel):
    channel: str = Field(min_length=1, max_length=64, examples=["push"])
    value: str = Field(min_length=1, max_length=2048, examples=["fcm-registration-token"])
    label: str | None = Field(default=None, max_length=100, examples=["Pixel 9"])


class RecipientIn(BaseModel):
    """A recipient profile. Omitted fields are left unchanged on update."""

    external_id: str = Field(min_length=1, max_length=255, examples=["user_42"])
    locale: str | None = Field(default=None, max_length=35, examples=["uz-Latn"])
    timezone: str | None = Field(default=None, max_length=64, examples=["Asia/Tashkent"])
    region: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$",
        description="ISO 3166 country or subdivision whose messaging laws apply, e.g. US-FL.",
        examples=["US-FL"],
    )
    attributes: dict[str, Any] | None = Field(
        default=None, description="Merged (shallowly) into the stored attributes."
    )
    addresses: list[AddressIn] | None = Field(
        default=None, description="Added if missing; existing addresses are kept."
    )


async def find(session: AsyncSession, external_id: str) -> Recipient | None:
    return await session.scalar(select(Recipient).where(Recipient.external_id == external_id))


async def get(session: AsyncSession, external_id: str) -> Recipient:
    recipient = await find(session, external_id)
    if recipient is None:
        raise NotFoundError("recipient", external_id)
    return recipient


async def ensure(session: AsyncSession, external_id: str) -> Recipient:
    """Return the recipient, creating an empty profile if needed (safe under concurrency)."""
    recipient = await find(session, external_id)
    if recipient is not None:
        return recipient
    now = utcnow()
    await insert_ignore(
        session,
        Recipient,
        {
            "id": uuid7(),
            "external_id": external_id,
            "attributes": {},
            "preferences": {},
            "preference_token_id": preference_token_id(),
            "created_at": now,
            "updated_at": now,
        },
        conflict=["external_id"],
    )
    return await get(session, external_id)


async def lock(session: AsyncSession, external_id: str) -> Recipient:
    """Ensure the recipient exists and lock its row for the rest of the transaction.

    Profile updates read, merge and write attributes and addresses; the lock (and the
    fresh reload it forces) keeps concurrent updates from losing each other's changes.
    """
    await ensure(session, external_id)
    recipient = await session.scalar(
        select(Recipient)
        .where(Recipient.external_id == external_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert recipient is not None
    return recipient


def overwritten_fields(recipient: Recipient, data: RecipientIn) -> list[str]:
    stored = {"locale": recipient.locale, "timezone": recipient.timezone}
    wanted = {"locale": data.locale, "timezone": data.timezone}
    return [
        name
        for name, value in wanted.items()
        if value is not None and stored[name] is not None and stored[name] != value
    ]


async def upsert(
    session: AsyncSession,
    data: RecipientIn,
    *,
    revive_addresses: bool = False,
    may_change_profile: bool = True,
) -> Recipient:
    recipient = await lock(session, data.external_id)
    overwritten = [] if may_change_profile else overwritten_fields(recipient, data)
    if overwritten:
        raise ForbiddenError(
            f"changing the {' and '.join(overwritten)} of an existing recipient requires "
            "the recipients:write scope"
        )
    if data.locale is not None:
        recipient.locale = data.locale
    if data.timezone is not None:
        recipient.timezone = data.timezone
    if data.region is not None:
        recipient.region = data.region
    if data.attributes:
        recipient.attributes = {**recipient.attributes, **data.attributes}
    for address in data.addresses or []:
        await add_address(session, recipient, address, revive=revive_addresses)
    recipient.updated_at = utcnow()
    await session.flush()
    return recipient


async def add_address(
    session: AsyncSession, recipient: Recipient, data: AddressIn, *, revive: bool = True
) -> Address:
    """Register an address; re-enables it if it exists but was disabled."""
    for existing in recipient.addresses:
        if existing.channel == data.channel and existing.value == data.value:
            if revive:
                existing.enabled = True
                existing.disabled_reason = None
            if data.label is not None:
                existing.label = data.label
            return existing
    address = Address(channel=data.channel, value=data.value, label=data.label, enabled=True)
    recipient.addresses.append(address)
    await session.flush()
    return address


async def remove_address(
    session: AsyncSession, recipient: Recipient, channel: str, value: str
) -> None:
    for address in list(recipient.addresses):
        if address.channel == channel and address.value == value:
            recipient.addresses.remove(address)
            await session.flush()
            return
    raise NotFoundError("address", f"{channel}:{value}")


def preferences(recipient: Recipient) -> RecipientPreferences:
    return RecipientPreferences.model_validate(recipient.preferences or {})


def set_preferences(recipient: Recipient, value: RecipientPreferences) -> None:
    recipient.preferences = value.model_dump(mode="json")
    recipient.updated_at = utcnow()


def profile(recipient: Recipient) -> dict[str, Any]:
    """The recipient as seen by rule conditions and templates."""
    return {
        "external_id": recipient.external_id,
        "locale": recipient.locale,
        "timezone": recipient.timezone,
        "region": recipient.region,
        "attributes": recipient.attributes,
    }
