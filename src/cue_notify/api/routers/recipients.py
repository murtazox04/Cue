from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from cue_notify.api.deps import (
    PageDep,
    RecipientsReader,
    RecipientsWriter,
    RuntimeDep,
    SessionDep,
    paginate,
)
from cue_notify.api.schemas import (
    AddressIn,
    Page,
    PreferenceChangeOut,
    RecipientOut,
    RecipientUpsert,
)
from cue_notify.core.enums import PreferenceSource
from cue_notify.core.policy import RecipientPreferences
from cue_notify.db.models import ApiKey, PreferenceChange, Recipient
from cue_notify.services import agents, preferences
from cue_notify.services import recipients as recipient_service
from cue_notify.settings import Settings

router = APIRouter(prefix="/recipients", tags=["recipients"])


def _out(recipient: Recipient, key: ApiKey, settings: Settings) -> RecipientOut:
    """Agent keys never see the preference token: it would let them change preferences."""
    out = RecipientOut.model_validate(recipient)
    if key.agent_key:
        return out
    return out.model_copy(
        update={"preference_token": preferences.preference_token(settings, recipient)}
    )


@router.put("/{external_id}", summary="Create or update a recipient")
async def upsert_recipient(
    external_id: str,
    body: RecipientUpsert,
    key: RecipientsWriter,
    session: SessionDep,
    runtime: RuntimeDep,
) -> RecipientOut:
    # A profile decides how everyone's messages to this person go (time zone, region,
    # attributes in approval conditions); agents refer to people by external id only.
    agents.check_not_agent(key, "change recipient profiles")
    recipient = await recipient_service.upsert(
        session,
        recipient_service.RecipientIn(external_id=external_id, **body.model_dump()),
        revive_addresses=True,
    )
    return _out(recipient, key, runtime.settings)


@router.get("", summary="List recipients")
async def list_recipients(
    key: RecipientsReader, session: SessionDep, runtime: RuntimeDep, page: PageDep
) -> Page[RecipientOut]:
    items, cursor = await paginate(session, select(Recipient), Recipient.id, page)
    return Page(items=[_out(item, key, runtime.settings) for item in items], next_cursor=cursor)


@router.get("/{external_id}", summary="Get a recipient")
async def get_recipient(
    external_id: str, key: RecipientsReader, session: SessionDep, runtime: RuntimeDep
) -> RecipientOut:
    return _out(await recipient_service.get(session, external_id), key, runtime.settings)


@router.delete(
    "/{external_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Erase a recipient and their message history",
)
async def delete_recipient(external_id: str, key: RecipientsWriter, session: SessionDep) -> None:
    agents.check_not_agent(key, "delete recipients")  # it would erase their preferences
    """Permanently deletes the profile, addresses and messages (right to erasure)."""
    await session.delete(await recipient_service.get(session, external_id))


@router.post(
    "/{external_id}/addresses",
    status_code=status.HTTP_201_CREATED,
    summary="Add an address (device token, phone, e-mail…)",
)
async def add_address(
    external_id: str,
    body: AddressIn,
    key: RecipientsWriter,
    session: SessionDep,
    runtime: RuntimeDep,
) -> RecipientOut:
    agents.check_not_agent(key, "change where messages go")
    recipient = await recipient_service.lock(session, external_id)
    await recipient_service.add_address(session, recipient, body)
    return _out(recipient, key, runtime.settings)


@router.delete(
    "/{external_id}/addresses/{channel}/{value:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an address",
)
async def remove_address(
    external_id: str, channel: str, value: str, key: RecipientsWriter, session: SessionDep
) -> None:
    agents.check_not_agent(key, "change where messages go")
    recipient = await recipient_service.get(session, external_id)
    await recipient_service.remove_address(session, recipient, channel, value)


@router.put("/{external_id}/preferences", summary="Replace delivery preferences")
async def set_preferences(
    external_id: str,
    body: RecipientPreferences,
    key: RecipientsWriter,
    session: SessionDep,
    runtime: RuntimeDep,
) -> RecipientOut:
    """Changes are recorded in the consent ledger with source `api`. Agent keys cannot
    change people's preferences."""
    agents.check_not_agent(key, "change people's preferences")
    await recipient_service.get(session, external_id)  # 404 rather than create
    recipient = await recipient_service.lock(session, external_id)
    await preferences.apply(
        session, recipient, body, PreferenceSource.API, {"method": "api", "api_key": key.name}
    )
    return _out(recipient, key, runtime.settings)


@router.post(
    "/{external_id}/consent",
    status_code=201,
    summary="Record an opt-in, with the wording the person agreed to",
)
async def record_consent(
    external_id: str,
    body: preferences.ConsentIn,
    key: RecipientsWriter,
    session: SessionDep,
) -> PreferenceChangeOut:
    """Subscribes the person to `categories` and stores the evidence in the consent ledger:
    the exact disclosure (and its SHA-256), how and where they agreed, and when the
    opt-in expires. After `expires_at`, messages in those categories are suppressed
    (`consent_expired`) until consent is recorded again."""
    agents.check_not_agent(key, "record consent on people's behalf")
    await recipient_service.get(session, external_id)
    recipient = await recipient_service.lock(session, external_id)
    change = await preferences.record_consent(session, recipient, body, key.name)
    return PreferenceChangeOut.model_validate(change)


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
    external_id: str, key: RecipientsWriter, session: SessionDep, runtime: RuntimeDep
) -> RecipientOut:
    """Preference and unsubscribe links sent before stop working."""
    agents.check_not_agent(key, "rotate preference tokens")
    await recipient_service.get(session, external_id)
    recipient = await recipient_service.lock(session, external_id)
    preferences.rotate(recipient)
    return _out(recipient, key, runtime.settings)
