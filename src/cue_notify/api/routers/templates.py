from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from cue_notify.api.deps import Admin, PageDep, RuntimeDep, SessionDep, paginate
from cue_notify.api.schemas import (
    Page,
    PreviewIn,
    PreviewOut,
    TemplateFields,
    TemplateIn,
    TemplateOut,
)
from cue_notify.core import policy
from cue_notify.core.content import LocalizedTemplate
from cue_notify.core.templating import RenderError
from cue_notify.db.models import Template
from cue_notify.services import catalog
from cue_notify.services import recipients as recipient_service
from cue_notify.services.errors import ConflictError, UnprocessableError

router = APIRouter(prefix="/templates", tags=["templates"])


def _locales(body: TemplateFields) -> dict[str, object]:
    return {
        locale: content.model_dump(exclude_none=True) for locale, content in body.locales.items()
    }


@router.get("", summary="List templates")
async def list_templates(_: Admin, session: SessionDep, page: PageDep) -> Page[TemplateOut]:
    items, cursor = await paginate(session, select(Template), Template.id, page)
    return Page(items=[TemplateOut.model_validate(item) for item in items], next_cursor=cursor)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a template")
async def create_template(body: TemplateIn, _: Admin, session: SessionDep) -> TemplateOut:
    if await session.scalar(select(Template.id).where(Template.key == body.key)) is not None:
        raise ConflictError(f"template {body.key} already exists")
    template = Template(
        key=body.key,
        description=body.description,
        default_locale=body.default_locale,
        locales=_locales(body),
        ai_instructions=body.ai_instructions,
        ai_attributes=body.ai_attributes,
    )
    session.add(template)
    await session.flush()
    return TemplateOut.model_validate(template)


@router.get("/{key}", summary="Get a template")
async def get_template(key: str, _: Admin, session: SessionDep) -> TemplateOut:
    return TemplateOut.model_validate(await catalog.template(session, key))


@router.put("/{key}", summary="Replace a template (bumps its version)")
async def replace_template(
    key: str, body: TemplateFields, _: Admin, session: SessionDep
) -> TemplateOut:
    template = await catalog.template(session, key)
    template.description = body.description
    template.default_locale = body.default_locale
    template.locales = _locales(body)
    template.ai_instructions = body.ai_instructions
    template.ai_attributes = body.ai_attributes
    await session.flush()
    return TemplateOut.model_validate(template)


@router.delete(
    "/{key}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an unused template"
)
async def delete_template(key: str, _: Admin, session: SessionDep) -> None:
    await session.delete(await catalog.template(session, key))
    await session.flush()


@router.post("/{key}/preview", summary="Render a template with sample data")
async def preview_template(
    key: str, body: PreviewIn, _: Admin, session: SessionDep, runtime: RuntimeDep
) -> PreviewOut:
    template = await catalog.template(session, key)
    recipient = await recipient_service.get(session, body.recipient) if body.recipient else None
    preferred = body.locale or (recipient.locale if recipient else None)
    locale = policy.pick_locale(
        template.locales,
        preferred or runtime.settings.policy.default_locale,
        template.default_locale,
    )
    if locale is None:
        raise UnprocessableError("template has no locales")
    context = {
        "data": body.data,
        "recipient": recipient_service.profile(recipient) if recipient else {},
        "event": None,
        "category": None,
    }
    try:
        variants = LocalizedTemplate.model_validate(template.locales[locale]).render_variants(
            context
        )
    except RenderError as exc:
        raise UnprocessableError(f"render failed: {exc}") from exc
    return PreviewOut(
        locale=locale,
        variants={
            name: content.model_dump(exclude_none=True) for name, content in variants.items()
        },
    )
