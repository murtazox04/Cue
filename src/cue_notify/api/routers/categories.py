from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from cue_notify.api.deps import Admin, SessionDep
from cue_notify.api.schemas import CategoryIn, CategoryOut, CategoryUpdate
from cue_notify.db.models import Category
from cue_notify.services import catalog
from cue_notify.services.errors import ConflictError

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", summary="List categories")
async def list_categories(_: Admin, session: SessionDep) -> list[CategoryOut]:
    rows = (await session.scalars(select(Category).order_by(Category.key))).all()
    return [CategoryOut.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a category")
async def create_category(body: CategoryIn, _: Admin, session: SessionDep) -> CategoryOut:
    if await session.get(Category, body.key) is not None:
        raise ConflictError(f"category {body.key} already exists")
    category = Category(**body.model_dump(mode="json"))
    session.add(category)
    await session.flush()
    return CategoryOut.model_validate(category)


@router.get("/{key}", summary="Get a category")
async def get_category(key: str, _: Admin, session: SessionDep) -> CategoryOut:
    return CategoryOut.model_validate(await catalog.category(session, key))


@router.patch("/{key}", summary="Update a category")
async def update_category(
    key: str, body: CategoryUpdate, _: Admin, session: SessionDep
) -> CategoryOut:
    category = await catalog.category(session, key)
    for field, value in body.model_dump(mode="json", exclude_unset=True).items():
        setattr(category, field, value)
    await session.flush()
    return CategoryOut.model_validate(category)


@router.delete(
    "/{key}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an unused category"
)
async def delete_category(key: str, _: Admin, session: SessionDep) -> None:
    await session.delete(await catalog.category(session, key))
    await session.flush()
