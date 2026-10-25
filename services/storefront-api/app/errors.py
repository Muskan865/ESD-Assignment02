"""One error format for every HTTP error: RFC 9457 "problem details".

Every non-2xx response from this service - a business rejection, a validation
failure, an unknown route, a rate limit, a crash - has the same JSON shape and
the media type `application/problem+json`:

    {
      "type": "urn:tiny-shop:problem:out_of_stock",
      "title": "Conflict",
      "status": 409,
      "detail": "out of stock: KB-01",
      "code": "out_of_stock",
      "request_id": "3f9a1c0b7d2e"
    }

`code` is the stable, machine-readable part callers should branch on; it is
the same string the gRPC service sends in its `error-code` trailer. `detail`
is for humans and may change. Validation failures add an `errors` list.
"""

from http import HTTPStatus

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROBLEM_JSON = "application/problem+json"

# Default code for errors raised by the framework rather than by our own code.
_STATUS_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "unavailable",
    504: "gateway_timeout",
}


class ApiError(Exception):
    """Raise from a route to answer with a problem+json response."""

    def __init__(self, status: int, code: str, detail: str,
                 headers: dict | None = None, errors: list | None = None):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers or {}
        self.errors = errors


class Problem(BaseModel):
    """The error body, declared so the OpenAPI document describes it."""
    type: str
    title: str
    status: int
    detail: str
    code: str
    request_id: str
    errors: list[dict] | None = None


def problem_response(request: Request, status: int, code: str, detail: str,
                     headers: dict | None = None, errors: list | None = None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "-")
    body = {
        "type": f"urn:tiny-shop:problem:{code}",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "code": code,
        "request_id": request_id,
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON,
                        headers={**(headers or {}), "x-request-id": request_id})


def validation_errors(errors) -> list[dict]:
    """Pydantic's error list, trimmed to what a caller needs to fix the request.

    The raw `input` and `ctx` are dropped: they can echo back whatever the
    caller sent, and are not always JSON-serialisable.
    """
    return [{"field": ".".join(str(part) for part in e.get("loc", ())),
             "message": e.get("msg", ""), "type": e.get("type", "")}
            for e in errors]


async def api_error_handler(request: Request, exc: ApiError):
    return problem_response(request, exc.status, exc.code, exc.detail, exc.headers, exc.errors)


async def http_exception_handler(request: Request, exc: HTTPException):
    # 404 for an unknown URL and 405 for a wrong method come from the router as
    # plain HTTPExceptions; convert them so they look like every other error.
    return problem_response(request, exc.status_code,
                            _STATUS_CODES.get(exc.status_code, "error"),
                            str(exc.detail), getattr(exc, "headers", None))


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return problem_response(request, 422, "invalid_request", "request failed validation",
                            errors=validation_errors(exc.errors()))


def install(app) -> None:
    from starlette.exceptions import HTTPException as StarletteHTTPException
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)


# Reusable OpenAPI `responses=` entries, so the spec lists every error a route
# can return with the Problem schema.
def problem_responses(*statuses: int) -> dict:
    return {status: {"model": Problem, "description": HTTPStatus(status).phrase,
                     "content": {PROBLEM_JSON: {}}}
            for status in statuses}
