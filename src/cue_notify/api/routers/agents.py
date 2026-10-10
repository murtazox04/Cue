from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from cue_notify.api.deps import Admin, SessionDep
from cue_notify.api.schemas import AgentIn, AgentOut, AgentUpdate
from cue_notify.db.models import Agent
from cue_notify.services import agents
from cue_notify.services.errors import ConflictError

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", summary="List agents")
async def list_agents(_: Admin, session: SessionDep) -> list[AgentOut]:
    rows = (await session.scalars(select(Agent).order_by(Agent.key))).all()
    return [AgentOut.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Register an agent")
async def create_agent(body: AgentIn, _: Admin, session: SessionDep) -> AgentOut:
    """An automated sender with its own attention budget. Bind API keys to it
    (`POST /v1/api-keys` with `agent`) and give those keys to the agent."""
    if await session.get(Agent, body.key) is not None:
        raise ConflictError(f"agent {body.key} already exists")
    agent = Agent(**body.model_dump(mode="json"))
    session.add(agent)
    await session.flush()
    return AgentOut.model_validate(agent)


@router.get("/{key}", summary="Get an agent")
async def get_agent(key: str, _: Admin, session: SessionDep) -> AgentOut:
    return AgentOut.model_validate(await agents.get(session, key))


@router.patch("/{key}", summary="Update an agent")
async def update_agent(key: str, body: AgentUpdate, _: Admin, session: SessionDep) -> AgentOut:
    """Set `enabled: false` to stop an agent at once: its keys are refused."""
    agent = await agents.get(session, key)
    for field, value in body.model_dump(mode="json", exclude_unset=True).items():
        setattr(agent, field, value)
    await session.flush()
    return AgentOut.model_validate(agent)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an agent")
async def delete_agent(key: str, _: Admin, session: SessionDep) -> None:
    await agents.delete(session, await agents.get(session, key))
    await session.flush()
