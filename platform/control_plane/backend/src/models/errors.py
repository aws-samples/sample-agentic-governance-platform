"""The body every operation may answer with, and the closed set of codes it carries.

``ErrorBody`` is the wire of ``core.errors.AppError.body()``: ``detail`` is the rendered text or dict
and ``code`` is ``domain.kind``, required because every refusal a route decides raises an
``AppError`` kind. ``ERROR_RESPONSES`` declares the body on the application for 400, 403, 404, 409,
502, 503 and 504. 422 is not declared: FastAPI drops ``HTTPValidationError`` from an operation that
declares its own 422. 500 is not declared: the generic handler's body carries no ``code``. 401 is not
declared: the auth dependencies answer it without a ``code``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from core.errors import all_codes
from models.base import WireModel


def _codes_enum(schema: dict[str, Any]) -> None:
    schema["enum"] = all_codes()


ErrorCode = Annotated[str, Field(json_schema_extra=_codes_enum)]


class ErrorBody(WireModel):
    detail: str | dict[str, Any]
    code: ErrorCode


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorBody} for status in (400, 403, 404, 409, 502, 503, 504)
}
