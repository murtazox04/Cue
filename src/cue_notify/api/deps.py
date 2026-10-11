"""FastAPI dependencies: runtime, transactional session, authentication, pagination."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.enums import Scope
from cue_notify.db.base import IdMixin
from cue_notify.db.models import Agent, ApiKey
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ApiKey",
    description="`Authorization: Bearer ck_…` — create keys with `cuectl keys create`.",
)


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime  # type: ignore[no-any-return]


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


async def get_session(runtime: RuntimeDep) -> AsyncIterator[AsyncSession]:
    """One transaction per request, committed before the response is sent."""
    async with runtime.transaction() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def require(scope: Scope) -> Callable[..., Awaitable[ApiKey]]:
    async def dependency(
        runtime: RuntimeDep,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> ApiKey:
        unauthorized = HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
        if credentials is None:
            raise unauthorized
        async with runtime.transaction() as session:
            key = await api_keys.authenticate(session, credentials.credentials)
            if key is not None and key.agent_key is not None:
                agent = await session.get(Agent, key.agent_key)
                if agent is None or not agent.enabled:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "the key's agent is disabled")
        if key is None:
            raise unauthorized
        if not api_keys.allows(key, scope):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"API key lacks the {scope} scope")
        return key

    return dependency


def check_inline_profile(key: ApiKey, recipient: object) -> None:
    """Inline profiles on events and sends may update a recipient, but adding addresses
    decides where messages go — that needs ``recipients:write``."""
    from cue_notify.services.recipients import RecipientIn

    if isinstance(recipient, RecipientIn) and key.agent_key is not None:
        # A profile changes how everyone's messages to this person are decided (time zone,
        # attributes used by approval conditions); agents may not rewrite it.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "agent keys refer to recipients by external id only"
        )
    if (
        isinstance(recipient, RecipientIn)
        and recipient.addresses
        and not api_keys.allows(key, Scope.RECIPIENTS_WRITE)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "adding addresses inline requires the recipients:write scope",
        )


# Authenticated API key holding the named scope.
EventsWriter = Annotated[ApiKey, Depends(require(Scope.EVENTS_WRITE))]
MessagesWriter = Annotated[ApiKey, Depends(require(Scope.MESSAGES_WRITE))]
MessagesReader = Annotated[ApiKey, Depends(require(Scope.MESSAGES_READ))]
RecipientsWriter = Annotated[ApiKey, Depends(require(Scope.RECIPIENTS_WRITE))]
RecipientsReader = Annotated[ApiKey, Depends(require(Scope.RECIPIENTS_READ))]
Admin = Annotated[ApiKey, Depends(require(Scope.ADMIN))]


class PageParams:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        cursor: Annotated[UUID | None, Query(description="From a previous `next_cursor`.")] = None,
    ) -> None:
        self.limit = limit
        self.cursor = cursor


PageDep = Annotated[PageParams, Depends()]


async def paginate[T: IdMixin](
    session: AsyncSession, stmt: Select[T], id_column: Any, page: PageParams
) -> tuple[list[T], str | None]:
    """Keyset pagination over a UUIDv7 primary key, newest first."""
    if page.cursor is not None:
        stmt = stmt.where(id_column < page.cursor)
    rows = list(
        (await session.scalars(stmt.order_by(id_column.desc()).limit(page.limit + 1))).all()
    )
    if len(rows) > page.limit:
        rows = rows[: page.limit]
        return rows, str(rows[-1].id)
    return rows, None
