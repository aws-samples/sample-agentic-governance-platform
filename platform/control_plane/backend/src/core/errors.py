"""Typed service errors and the wire each one produces.

A service raises a subclass of `AppError` with a `kind`; the subclass's `kinds` table says what the
client sees for that kind, and `app_error_handler`, which `install_error_handlers` registers on an
app, renders it. Routes never translate.

A subclass sets two class attributes: `domain`, the service's name, and `kinds: ClassVar = {...}`, a
mapping from kind to `Outcome(status, detail)`. `detail` is the fixed client text, `None` (the
exception's message is the detail), or a dict template whose values equal to `MESSAGE` become the
message and over which the exception's `extra` is merged; every rendering is a fresh copy. A `None`
detail or a `MESSAGE` value sends the exception's message to the client verbatim, so that message is
client copy: never an upstream response body, a caught exception's text or a secret. With a one-row
table the kind may be omitted. A kind outside the table, a missing kind over a multi-row table, and a
non-empty `extra` over a string or `None` detail are `TypeError`s at construction, so a wrong raise
fails in the test that exercises it.

`body()` is `{"detail": <rendered detail>, "code": "<domain>.<kind>"}` and nothing else.
`all_codes()` is the sorted `domain.kind` of every subclass under `src` the process has imported, judged
by the subclass's dotted module name mapped to a file under `src`, which trusts a forged `__module__`.
`tests/test_error_layer.py` holds every subclass to this contract.
Decision: docs/adr/backend-service-errors-one-handler.md
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Final, NamedTuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class _Message:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MESSAGE"


MESSAGE: Final = _Message()
_SRC: Final = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)


class Outcome(NamedTuple):
    status: int
    detail: str | Mapping[str, Any] | None


class AppError(Exception):
    domain: ClassVar[str]
    kinds: ClassVar[Mapping[str, Outcome]]

    message: str
    kind: str
    extra: Mapping[str, Any]

    def __init__(
        self, message: str, kind: str | None = None, *, extra: Mapping[str, Any] | None = None
    ) -> None:
        kinds = type(self).kinds
        if kind is None and len(kinds) == 1:
            kind = next(iter(kinds))
        if kind is None or kind not in kinds:
            raise TypeError(f"{type(self).__name__}: unknown kind {kind!r}; the table is {sorted(kinds)}")
        if extra and not isinstance(kinds[kind].detail, Mapping):
            raise TypeError(
                f"{type(self).__name__}: kind {kind!r} has no dict template, so extra={dict(extra)!r} "
                "has nowhere to land"
            )
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.extra = dict(extra) if extra else {}

    @property
    def status_code(self) -> int:
        return type(self).kinds[self.kind].status

    @property
    def detail(self) -> str | dict[str, Any]:
        template = type(self).kinds[self.kind].detail
        if template is None:
            return self.message
        if isinstance(template, str):
            return template
        rendered = {
            key: self.message if value is MESSAGE else copy.deepcopy(value) for key, value in template.items()
        }
        rendered.update(copy.deepcopy(self.extra))
        return rendered

    @property
    def code(self) -> str:
        return f"{type(self).domain}.{self.kind}"

    def body(self) -> dict[str, Any]:
        return {"detail": self.detail, "code": self.code}


def _defined_under_src(cls: type) -> bool:
    path = _SRC.joinpath(*cls.__module__.split("."))
    return path != _SRC and (path.with_suffix(".py").is_file() or (path / "__init__.py").is_file())


def all_codes() -> list[str]:
    codes: set[str] = set()
    pending = list(AppError.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if _defined_under_src(cls):
            codes.update(f"{cls.domain}.{kind}" for kind in cls.kinds)
    return sorted(codes)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.exception("%s", exc.code)
    else:
        logger.warning("%s", exc.code)
    return JSONResponse(status_code=exc.status_code, content=exc.body())


def install_error_handlers(app: FastAPI) -> None:
    app.exception_handler(AppError)(app_error_handler)
