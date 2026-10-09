"""RFC 9457 problem details for every error response."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from cue_notify.services.errors import ServiceError

logger = logging.getLogger(__name__)

MEDIA_TYPE = "application/problem+json"


def problem(
    status: int,
    title: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service(_: Request, exc: ServiceError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return problem(exc.status_code, exc.title, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        title = HTTPStatus(exc.status_code).phrase
        headers = dict(exc.headers) if exc.headers else None
        return problem(exc.status_code, title, exc.detail or None, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        errors = [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return problem(422, "Unprocessable content", "request validation failed", errors=errors)

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        logger.info("integrity error: %s", exc.orig)
        return problem(409, "Conflict", "the request conflicts with existing data")

    @app.exception_handler(StaleDataError)
    async def _stale(_: Request, __: StaleDataError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return problem(409, "Conflict", "the resource was modified concurrently; retry")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        logger.error("unhandled error", exc_info=exc, extra={"path": request.url.path})
        return problem(500, "Internal server error")
