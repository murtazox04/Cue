"""Errors raised by services and translated to HTTP problem responses by the API."""

from __future__ import annotations


class ServiceError(Exception):
    status_code = 400
    title = "Bad request"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(ServiceError):
    status_code = 404
    title = "Not found"

    def __init__(self, resource: str, key: object) -> None:
        super().__init__(f"{resource} {key!s} does not exist")


class ConflictError(ServiceError):
    status_code = 409
    title = "Conflict"


class InvalidStateError(ServiceError):
    status_code = 409
    title = "Invalid state"


class UnprocessableError(ServiceError):
    status_code = 422
    title = "Unprocessable content"


class ForbiddenError(ServiceError):
    status_code = 403
    title = "Forbidden"


class TooManyRequestsError(ServiceError):
    status_code = 429
    title = "Too many requests"


class GoneError(ServiceError):
    status_code = 410
    title = "Gone"
