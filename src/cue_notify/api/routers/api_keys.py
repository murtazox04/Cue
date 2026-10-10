from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import select

from cue_notify.api.deps import Admin, SessionDep
from cue_notify.api.schemas import ApiKeyCreated, ApiKeyIn, ApiKeyOut
from cue_notify.core.time import utcnow
from cue_notify.db.models import ApiKey
from cue_notify.services import agents, api_keys
from cue_notify.services.errors import NotFoundError

router = APIRouter(prefix="/api-keys", tags=["api keys"])


@router.get("", summary="List API keys")
async def list_keys(_: Admin, session: SessionDep) -> list[ApiKeyOut]:
    rows = (await session.scalars(select(ApiKey).order_by(ApiKey.created_at))).all()
    return [ApiKeyOut.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create an API key")
async def create_key(body: ApiKeyIn, _: Admin, session: SessionDep) -> ApiKeyCreated:
    if body.agent is not None:
        await agents.get(session, body.agent)
    key, raw = await api_keys.create(
        session, body.name, body.scopes, body.expires_at, agent_key=body.agent
    )
    return ApiKeyCreated.model_validate({**ApiKeyOut.model_validate(key).model_dump(), "key": raw})


@router.post("/{key_id}/revoke", summary="Revoke an API key")
async def revoke_key(key_id: UUID, _: Admin, session: SessionDep) -> ApiKeyOut:
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise NotFoundError("API key", key_id)
    key.revoked_at = key.revoked_at or utcnow()
    return ApiKeyOut.model_validate(key)
