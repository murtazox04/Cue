"""Self-service preferences, authenticated by the recipient's preference token.

These endpoints need no API key: the token in the path is the credential, exactly like an
unsubscribe link. Build your own preference UI on them, or use the hosted page.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from cue_notify.api.deps import RuntimeDep, SessionDep
from cue_notify.api.pages import provenance
from cue_notify.services import preferences

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.get("/{token}", summary="Read preferences (no API key)")
async def get_preferences(
    token: str, session: SessionDep, runtime: RuntimeDep
) -> preferences.PreferenceCenter:
    """Categories with their subscription state and the routes this person can be
    reached on, ready to render as a preference page."""
    recipient = await preferences.by_token(session, runtime.settings, token, lock=False)
    return await preferences.center(session, runtime, recipient)


@router.put("/{token}", summary="Update preferences (no API key)")
async def update_preferences(
    token: str,
    body: preferences.PreferenceCenterUpdate,
    request: Request,
    session: SessionDep,
    runtime: RuntimeDep,
) -> preferences.PreferenceCenter:
    """Recorded in the consent ledger with source `self_service`. Categories that do not
    allow unsubscribing (such as `transactional`) are refused with 422."""
    recipient = await preferences.by_token(session, runtime.settings, token)
    return await preferences.update(
        session, runtime, recipient, body, provenance=provenance(request, "preference_api")
    )
