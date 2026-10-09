from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, status
from sqlalchemy import select

from cue_notify.api.deps import Admin, RuntimeDep, SessionDep
from cue_notify.api.schemas import EvaluateIn, RuleIn, RuleOut, RuleOutcomeOut, RuleUpdate
from cue_notify.core.ids import uuid7
from cue_notify.db.models import Event, Rule
from cue_notify.services import catalog
from cue_notify.services import events as event_service
from cue_notify.services import recipients as recipient_service
from cue_notify.services.errors import ConflictError, NotFoundError

router = APIRouter(prefix="/rules", tags=["rules"])


async def _get(session: SessionDep, key: str) -> Rule:
    rule = await session.scalar(select(Rule).where(Rule.key == key))
    if rule is None:
        raise NotFoundError("rule", key)
    return rule


@router.get("", summary="List rules (highest priority first)")
async def list_rules(_: Admin, session: SessionDep) -> list[RuleOut]:
    rows = (await session.scalars(select(Rule).order_by(Rule.priority.desc(), Rule.key))).all()
    return [RuleOut.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a rule")
async def create_rule(body: RuleIn, _: Admin, session: SessionDep, runtime: RuntimeDep) -> RuleOut:
    if await session.scalar(select(Rule.id).where(Rule.key == body.key)) is not None:
        raise ConflictError(f"rule {body.key} already exists")
    await catalog.template(session, body.template)
    await catalog.category(session, body.category)
    values = body.model_dump(exclude={"template", "category", "channels"})
    rule = Rule(
        **values,
        template_key=body.template,
        category_key=body.category,
        channels=catalog.channels(runtime, body.channels),
    )
    session.add(rule)
    await session.flush()
    return RuleOut.model_validate(rule)


@router.get("/{key}", summary="Get a rule")
async def get_rule(key: str, _: Admin, session: SessionDep) -> RuleOut:
    return RuleOut.model_validate(await _get(session, key))


@router.patch("/{key}", summary="Update a rule")
async def update_rule(
    key: str, body: RuleUpdate, _: Admin, session: SessionDep, runtime: RuntimeDep
) -> RuleOut:
    rule = await _get(session, key)
    changes = body.model_dump(exclude_unset=True)
    if (template := changes.pop("template", None)) is not None:
        rule.template_key = (await catalog.template(session, template)).key
    if (category := changes.pop("category", None)) is not None:
        rule.category_key = (await catalog.category(session, category)).key
    if (channels := changes.pop("channels", None)) is not None:
        rule.channels = catalog.channels(runtime, channels)
    for field, value in changes.items():
        setattr(rule, field, value)
    await session.flush()
    return RuleOut.model_validate(rule)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a rule")
async def delete_rule(key: str, _: Admin, session: SessionDep) -> None:
    await session.delete(await _get(session, key))


@router.post("/evaluate", summary="Dry-run: which rules would fire for this event?")
async def evaluate_rules(
    body: EvaluateIn, _: Admin, session: SessionDep, runtime: RuntimeDep
) -> list[RuleOutcomeOut]:
    """Evaluates conditions, rollout and delivery policy without sending or storing anything."""
    recipient = await recipient_service.get(session, body.recipient)
    now = datetime.now(UTC)
    event = Event(
        id=uuid7(),
        source="dry-run",
        name=body.name,
        recipient_id=recipient.id,
        data=body.data,
        occurred_at=now,
        received_at=now,
    )
    outcomes = await event_service.evaluate(session, runtime, event, recipient, dry_run=True)
    return [RuleOutcomeOut(**outcome.as_dict()) for outcome in outcomes]
