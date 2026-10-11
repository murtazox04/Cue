from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import text

from cue_notify import __version__
from cue_notify.api.deps import RuntimeDep
from cue_notify.api.problems import problem

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/readyz", summary="Readiness probe (checks the database)", response_model=None)
async def readyz(runtime: RuntimeDep) -> object:
    try:
        async with runtime.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # any database failure means not ready; details go to the log only
        logger.warning("readiness check failed", exc_info=True)
        return problem(503, "Service unavailable", "the database is not reachable")
    return {"status": "ready"}
