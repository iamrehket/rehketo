from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

ERROR_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_failed",
    429: "rate_limited",
}


def envelope(
    code: str,
    message: str,
    status: int,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    inner: dict[str, object] = {"code": code, "message": message}
    if extra:
        inner.update(extra)
    return JSONResponse(status_code=status, content={"error": inner})


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = ERROR_CODE_BY_STATUS.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else code
    return envelope(code=code, message=message, status=exc.status_code)


def _safe_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    # pydantic v2 field_validator puts the raw exception in ctx['error'],
    # which is not JSON-serializable. Stringify it so JSONResponse can encode.
    out = []
    for err in exc.errors():
        ctx = err.get("ctx")
        if ctx and "error" in ctx and isinstance(ctx["error"], Exception):
            err = {**err, "ctx": {**ctx, "error": str(ctx["error"])}}
        out.append(err)
    return out


async def _validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return envelope(
        "validation_failed",
        "request validation failed",
        422,
        extra={"details": _safe_errors(exc)},
    )


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internal detail on 500s.
    return envelope("internal_error", "something went wrong", 500)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_handler)
