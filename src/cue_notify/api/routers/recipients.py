from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from cue_notify.api.deps import PageDep, RecipientsReader, RecipientsWriter, SessionDep, paginate
from cue_notify.api.schemas import (
    AddressIn,
    Page,
    PreferenceChangeOut,
    RecipientOut,
    RecipientUpsert,
)
from cue_notify.core.enums import PreferenceSource
from cue_notify.core.policy import RecipientPreferences
from cue_notify.db.models import PreferenceChange, Recipient
from cue_notify.services import agents, preferences
from cue_notify.services import recipients as recipient_service

router = APIRouter(prefix="/recipients", tags=["recipients"])


@router.put("/{external_id}", summary="Create or update a recipient")
async def upsert_recipient(
    external_id: str, body: RecipientUpsert, _: RecipientsWriter, session: SessionDep
) -> RecipientOut:
    recipient = await recipient_service.upsert(
        session, recipient_service.RecipientIn(external_id=external_id, **body.model_dump())
    )
    return RecipientOut.model_validate(recipient)


@router.get("", summary="List recipients")
async def list_recipients(
    _: RecipientsReader, session: SessionDep, page: PageDep
) -> Page[RecipientOut]:
    items, cursor = await paginate(session, select(Recipient), Recipient.id, page)
    return Page(items=[RecipientOut.model_validate(item) for item in items], next_cursor=cursor)


@router.get("/{external_id}", summary="Get a recipient")
async def get_recipient(external_id: str, _: RecipientsReader, session: SessionDep) -> RecipientOut:
    return RecipientOut.model_validate(await recipient_service.get(session, external_id))


@router.delete(
    "/{external_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Erase a recipient and their message history",
)
async def delete_recipient(external_id: str, _: RecipientsWriter, session: SessionDep) -> None:
    """Permanently deletes the profile, addresses and messages (right to erasure)."""
    await session.delete(await recipient_service.get(session, external_id))


@router.post(
    "/{external_id}/addresses",
    status_code=status.HTTP_201_CREATED,
    summary="Add an address (device token, phone, e-mail…)",
)
async def add_address(
    external_id: str, body: AddressIn, _: RecipientsWriter, session: SessionDep
) -> RecipientOut:
    recipient = await recipient_service.lock(session, external_id)
    await recipient_service.add_address(session, recipient, body)
    return RecipientOut.model_validate(recipient)


@router.delete(
    "/{external_id}/addresses/{channel}/{value:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an address",
)
async def remove_address(
    external_id: str, channel: str, value: str, _: RecipientsWriter, session: SessionDep
) -> None:
    recipient = await recipient_service.get(session, external_id)
    await recipient_service.remove_address(session, recipient, channel, value)


@router.put("/{external_id}/preferences", summary="Replace delivery preferences")
async def set_preferences(
    external_id: str, body: RecipientPreferences, key: RecipientsWriter, session: SessionDep
) -> RecipientOut:
    """Changes are recorded in the consent ledger with source `api`. Agent keys cannot
    change people's preferences."""
    agents.check_not_agent(key, "change people's preferences")
    await recipient_service.get(session, external_id)  # 404 rather than create
    recipient = await recipient_service.lock(session, external_id)
    await preferences.apply(session, recipient, body, PreferenceSource.API)
    return RecipientOut.model_validate(recipient)


@router.get("/{external_id}/preferences/history", summary="Consent ledger: every preference change")
async def preference_history(
    external_id: str, _: RecipientsReader, session: SessionDep, page: PageDep
) -> Page[PreferenceChangeOut]:
    """Newest first, with the source of each change: your backend (`api`), the person
    themselves (`self_service`) or their mail client's one-click unsubscribe (`one_click`)."""
    recipient = await recipient_service.get(session, external_id)
    stmt = select(PreferenceChange).where(PreferenceChange.recipient_id == recipient.id)
    items, cursor = await paginate(session, stmt, PreferenceChange.id, page)
    return Page(
        items=[PreferenceChangeOut.model_validate(item) for item in items], next_cursor=cursor
    )


@router.post("/{external_id}/preference-token", summary="Issue a new preference token")
async def rotate_preference_token(
    external_id: str, key: RecipientsWriter, session: SessionDep
) -> RecipientOut:
    """Preference and unsubscribe links sent before stop working."""
    agents.check_not_agent(key, "rotate preference tokens")
    await recipient_service.get(session, external_id)
    recipient = await recipient_service.lock(session, external_id)
    preferences.rotate(recipient)
    return RecipientOut.model_validate(recipient)
