"""Self-service preferences: the preference centre, one-click unsubscribe, consent ledger.

Every recipient has a random ``preference_token``. Holding it lets someone see and change
that person's delivery preferences and nothing else, which is the trust model of an
unsubscribe link in an e-mail. Every change, whatever its source, is recorded in the
consent ledger (``preference_changes``) so you can show when and how someone opted out.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.enums import PreferenceSource
from cue_notify.core.ids import token
from cue_notify.core.policy import QuietHours, RecipientPreferences
from cue_notify.db.models import Category, PreferenceChange, Recipient
from cue_notify.runtime import Runtime
from cue_notify.services import recipients as recipient_service
from cue_notify.services.errors import NotFoundError, UnprocessableError
from cue_notify.settings import Settings

# --------------------------------------------------------------------------- links


def links(settings: Settings, recipient: Recipient, category: Category | None) -> dict[str, str]:
    """Absolute preference and unsubscribe URLs, when ``api.public_url`` is configured.

    ``unsubscribe`` is only present for categories people may unsubscribe from; it is the
    RFC 8058 one-click target (POST) and shows a confirmation page on GET, so link
    scanners that prefetch URLs never unsubscribe anyone.
    """
    base = settings.api.public_url
    if base is None:
        return {}
    page = f"{base}/preferences/{recipient.preference_token}"
    result = {"preferences": page}
    if category is not None and category.allow_unsubscribe:
        result["unsubscribe"] = f"{page}/unsubscribe?category={quote(category.key)}"
    return result


def template_links(settings: Settings, recipient: Recipient, category: Category) -> dict[str, Any]:
    """What templates see as ``links``: the URLs plus the token for your own preference UI."""
    return (
        {"preferences": None, "unsubscribe": None}
        | links(settings, recipient, category)
        | {"token": recipient.preference_token}
    )


# --------------------------------------------------------------------------- ledger


def diff(old: RecipientPreferences, new: RecipientPreferences) -> dict[str, Any]:
    """The ledger entry for a change from ``old`` to ``new``; empty when nothing changed."""
    changes: dict[str, Any] = {}
    pairs = {
        "unsubscribed": new.unsubscribed_categories - old.unsubscribed_categories,
        "resubscribed": old.unsubscribed_categories - new.unsubscribed_categories,
        "muted": new.muted_channels - old.muted_channels,
        "unmuted": old.muted_channels - new.muted_channels,
    }
    changes |= {key: sorted(values) for key, values in pairs.items() if values}
    if new.quiet_hours != old.quiet_hours:
        changes["quiet_hours"] = (
            new.quiet_hours.model_dump(mode="json") if new.quiet_hours else None
        )
    return changes


async def apply(
    session: AsyncSession,
    recipient: Recipient,
    new: RecipientPreferences,
    source: PreferenceSource,
    provenance: dict[str, Any] | None = None,
) -> PreferenceChange | None:
    """Store ``new`` and record what changed. The caller holds the recipient's row lock."""
    changes = diff(recipient_service.preferences(recipient), new)
    recipient_service.set_preferences(recipient, new)
    if not changes:
        return None
    change = PreferenceChange(
        recipient_id=recipient.id, source=source, changes=changes, provenance=provenance
    )
    session.add(change)
    await session.flush()
    return change


# --------------------------------------------------------------------------- tokens


async def by_token(session: AsyncSession, preference_token: str, *, lock: bool = True) -> Recipient:
    """The recipient owning ``preference_token``; row-locked unless only reading."""
    stmt = select(Recipient).where(Recipient.preference_token == preference_token)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    recipient = await session.scalar(stmt)
    if recipient is None:
        raise NotFoundError("preference page", "for this link")
    return recipient


def rotate(recipient: Recipient) -> None:
    """Issue a new token; links sent earlier stop working."""
    recipient.preference_token = token()


# --------------------------------------------------------------------------- centre


class CategoryChoice(BaseModel):
    key: str
    description: str | None
    subscribed: bool
    can_unsubscribe: bool = Field(description="False for messages people cannot opt out of.")


class ChannelChoice(BaseModel):
    name: str
    muted: bool


class PreferenceCenter(BaseModel):
    """Everything a preference page needs to render, in one response."""

    locale: str | None
    categories: list[CategoryChoice]
    channels: list[ChannelChoice] = Field(description="Routes this person has an address on.")
    quiet_hours: QuietHours | None


class PreferenceCenterUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    unsubscribed_categories: set[str] | None = None
    muted_channels: set[str] | None = None
    quiet_hours: QuietHours | None = Field(
        default=None, description="Send null to fall back to the default quiet hours."
    )


async def _categories(session: AsyncSession) -> list[Category]:
    return list((await session.scalars(select(Category).order_by(Category.key))).all())


def _channels(runtime: Runtime, recipient: Recipient, prefs: RecipientPreferences) -> list[str]:
    reachable = {
        a.channel for a in recipient.addresses if a.enabled and a.channel in runtime.channels
    }
    return sorted(reachable | (prefs.muted_channels & set(runtime.channels)))


async def center(session: AsyncSession, runtime: Runtime, recipient: Recipient) -> PreferenceCenter:
    prefs = recipient_service.preferences(recipient)
    return PreferenceCenter(
        locale=recipient.locale,
        categories=[
            CategoryChoice(
                key=category.key,
                description=category.description,
                subscribed=not (
                    category.allow_unsubscribe and category.key in prefs.unsubscribed_categories
                ),
                can_unsubscribe=category.allow_unsubscribe,
            )
            for category in await _categories(session)
        ],
        channels=[
            ChannelChoice(name=name, muted=name in prefs.muted_channels)
            for name in _channels(runtime, recipient, prefs)
        ],
        quiet_hours=prefs.quiet_hours,
    )


async def update(
    session: AsyncSession,
    runtime: Runtime,
    recipient: Recipient,
    body: PreferenceCenterUpdate,
    source: PreferenceSource = PreferenceSource.SELF_SERVICE,
    provenance: dict[str, Any] | None = None,
) -> PreferenceCenter:
    prefs = recipient_service.preferences(recipient)
    changes: dict[str, Any] = {}
    if body.unsubscribed_categories is not None:
        categories = {category.key: category for category in await _categories(session)}
        if unknown := body.unsubscribed_categories - categories.keys():
            raise UnprocessableError(f"unknown categories: {', '.join(sorted(unknown))}")
        locked = {
            key for key in body.unsubscribed_categories if not categories[key].allow_unsubscribe
        }
        if locked:
            raise UnprocessableError(f"cannot unsubscribe from: {', '.join(sorted(locked))}")
        changes["unsubscribed_categories"] = body.unsubscribed_categories
    if body.muted_channels is not None:
        if unknown := body.muted_channels - set(runtime.channels):
            raise UnprocessableError(f"unknown channels: {', '.join(sorted(unknown))}")
        changes["muted_channels"] = body.muted_channels
    if "quiet_hours" in body.model_fields_set:
        changes["quiet_hours"] = body.quiet_hours
    await apply(session, recipient, prefs.model_copy(update=changes), source, provenance)
    return await center(session, runtime, recipient)


async def unsubscribable(session: AsyncSession, category_key: str) -> Category:
    """The category, if people may leave it."""
    category = await session.get(Category, category_key)
    if category is None:
        raise NotFoundError("category", category_key)
    if not category.allow_unsubscribe:
        raise UnprocessableError(f"cannot unsubscribe from {category_key}")
    return category


async def unsubscribe(
    session: AsyncSession,
    recipient: Recipient,
    category_key: str,
    source: PreferenceSource = PreferenceSource.ONE_CLICK,
    provenance: dict[str, Any] | None = None,
) -> Category:
    category = await unsubscribable(session, category_key)
    prefs = recipient_service.preferences(recipient)
    updated = prefs.model_copy(
        update={"unsubscribed_categories": prefs.unsubscribed_categories | {category_key}}
    )
    await apply(session, recipient, updated, source, provenance)
    return category


async def unsubscribe_all(
    session: AsyncSession,
    recipient: Recipient,
    source: PreferenceSource = PreferenceSource.SELF_SERVICE,
    provenance: dict[str, Any] | None = None,
) -> None:
    """Opt out of every category that allows it (transactional messages still arrive)."""
    keys = {c.key for c in await _categories(session) if c.allow_unsubscribe}
    prefs = recipient_service.preferences(recipient)
    updated = prefs.model_copy(
        update={"unsubscribed_categories": prefs.unsubscribed_categories | keys}
    )
    await apply(session, recipient, updated, source, provenance)


async def save_choices(
    session: AsyncSession,
    runtime: Runtime,
    recipient: Recipient,
    subscribed: set[str],
    enabled_channels: set[str],
    provenance: dict[str, Any] | None = None,
) -> None:
    """Apply a submitted preference form: the boxes left ticked.

    Only categories that allow unsubscribing and channels shown on the page are affected,
    so choices the page did not offer (set through the API) are kept as they are.
    """
    prefs = recipient_service.preferences(recipient)
    optional = {c.key for c in await _categories(session) if c.allow_unsubscribe}
    shown = set(_channels(runtime, recipient, prefs))
    updated = prefs.model_copy(
        update={
            "unsubscribed_categories": (prefs.unsubscribed_categories - optional)
            | (optional - subscribed),
            "muted_channels": (prefs.muted_channels - shown) | (shown - enabled_channels),
        }
    )
    await apply(session, recipient, updated, PreferenceSource.SELF_SERVICE, provenance)
