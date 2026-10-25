"""FastAPI composition root, request-observability middleware, and startup wiring."""

import time
import traceback
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import db, errors
from .config import SERVICE_NAME
from .routes import refresh_open_orders_gauge, refresh_stock_gauge, router
from .state import should_emit
from .structured_logging import log_event
from .telemetry import DEMO_HAS_ID_LABEL, DEMO_REQUESTS, IN_FLIGHT, REQUEST_COUNT, REQUEST_LATENCY

app = FastAPI(
    title="Tiny Shop - Storefront API",
    version="2.0.0",
    description=(
        "A very small e-commerce API. Assignment 2: resource-oriented order "
        "endpoints, idempotent order creation (`Idempotency-Key`), a per-client "
        "rate limit on `POST /api/orders`, and one error format for every "
        "failure: RFC 9457 problem details (`application/problem+json`) with a "
        "stable `code` and the `request_id`."
    ),
)
errors.install(app)

STATIC_DIR = Path(__file__).parent / "static"

# The hand-written OpenAPI document is the API contract (A2 Part A). Serving it
# instead of FastAPI's generated schema means /docs shows exactly what callers
# are promised; tests/test_assignment2.py fails if the routes drift from it.
OPENAPI_FILE = Path(__file__).resolve().parent.parent / "openapi.yaml"


def _openapi() -> dict:
    if app.openapi_schema is None:
        app.openapi_schema = yaml.safe_load(OPENAPI_FILE.read_text(encoding="utf-8"))
    return app.openapi_schema


app.openapi = _openapi

# Scraped or polled constantly. Recording them would bury real traffic in both
# the metrics and the log index.
_UNINSTRUMENTED = {"/metrics", "/health", "/favicon.ico"}


def _route_template(request: Request) -> str:
    """Return the ROUTE TEMPLATE, never the raw URL.

    /api/orders/8231/cancel and /api/orders/8232/cancel must both record as
    /api/orders/{order_id}/cancel. Using the raw path as a metric label is
    precisely the cardinality explosion demonstrated in Part E.2.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    # No route matched (404). Collapse every unknown URL into one bucket so a
    # scanner hitting random paths cannot create unbounded series.
    return "unmatched"


@app.middleware("http")
async def observe(request: Request, call_next):
    if request.url.path in _UNINSTRUMENTED:
        return await call_next(request)

    # One id per request, minted at the edge and forwarded to payment-service.
    # It lives in LOGS ONLY -- never as a metric label (except in the Part E.2
    # demo counter, which exists to show why that is a mistake).
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id

    IN_FLIGHT.inc()
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["x-request-id"] = request_id
        return response
    except Exception as exc:
        # Handled here rather than re-raised: otherwise uvicorn prints the
        # traceback as plain text, and Filebeat ships every line of it as its
        # own unparseable document (10,000+ a minute during Part E.1). Instead
        # the whole stack goes into ONE field of ONE JSON line. It holds code
        # locations and the exception text only - never local variables.
        log_event(
            "unhandled exception",
            level="error",
            error={"type": type(exc).__name__,
                   "message": str(exc).splitlines()[0][:200] if str(exc) else "",
                   "stack_trace": traceback.format_exc()},
            url={"path": request.url.path, "route": _route_template(request)},
            request_id=request_id,
        )
        return errors.problem_response(request, 500, "internal_error", "internal error")
    finally:
        duration = time.perf_counter() - started
        IN_FLIGHT.dec()
        route = _route_template(request)
        REQUEST_COUNT.labels(request.method, route, str(status)).inc()
        REQUEST_LATENCY.labels(request.method, route).observe(duration)

        # --- Part E.2 cardinality experiment, off unless explicitly enabled ---
        # One brand-new label value per request => one brand-new time series.
        # Capped at 100 DISTINCT ids, so toggling off and on cannot create more.
        if should_emit(request_id):
            if DEMO_HAS_ID_LABEL:
                DEMO_REQUESTS.labels(request_id=request_id).inc()
            else:
                DEMO_REQUESTS.inc()

        log_event(
            "request completed",
            level="error" if status >= 500 else "info",
            http={
                "request": {"method": request.method},
                "response": {"status_code": status},
            },
            url={"path": request.url.path, "route": route},
            event={"duration_ms": round(duration * 1000, 2)},
            request_id=request_id,
        )


@app.exception_handler(db.PoolTimeout)
async def database_busy(request: Request, exc: db.PoolTimeout):
    """Every connection stayed busy for DB_POOL_WAIT_SECONDS: shed load cleanly.

    503 tells the client "retry later", unlike the bare 500 an exhausted pool
    used to produce; Retry-After says when.
    """
    log_event(
        "database busy",
        level="error",
        error={"type": "PoolTimeout", "message": str(exc)},
        url={"path": request.url.path, "route": _route_template(request)},
        request_id=getattr(request.state, "request_id", "-"),
    )
    return errors.problem_response(request, 503, "database_busy", "database busy, retry",
                                   headers={"Retry-After": "1"})


# ----------------------------------------------------------------- ops routes
@app.get("/health", tags=["ops"])
def health():
    """Liveness probe. Also drives the Compose healthcheck."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/ready", tags=["ops"])
def ready():
    """Readiness: are our dependencies actually reachable?"""
    try:
        db.ping()
    except Exception as exc:
        return Response(
            content='{"status":"degraded","database":"%s"}' % type(exc).__name__,
            media_type="application/json",
            status_code=503,
        )
    return {"status": "ok", "database": "ok"}


@app.get("/metrics", tags=["ops"])
def metrics():
    """Prometheus exposition endpoint -- the raw text Prometheus scrapes.

    The two business gauges are re-read from Postgres on every scrape. Orders
    placed through the gRPC service change them too, and only the database
    knows about both. A failed read keeps the last value rather than failing
    the scrape: the rest of the metrics matter most when the database is sick.
    """
    try:
        refresh_stock_gauge()
        refresh_open_orders_gauge()
    except Exception as exc:
        log_event("gauge refresh failed", level="warn", error={"type": type(exc).__name__})
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
def storefront_page():
    """The shop itself."""
    return FileResponse(STATIC_DIR / "index.html")


# --------------------------------------------------------------- app lifecycle
@app.on_event("startup")
def startup():
    """Open the pool and seed the gauges from the database.

    Gauges are current state. After a restart the in-process value is 0, which
    would be a lie if orders are already sitting in PLACED -- so both gauges are
    re-read from Postgres before the service accepts traffic.
    """
    db.init_pool()
    # Postgres may still be finishing its own startup even after the healthcheck
    # passes, so retry briefly rather than crash-looping the container.
    last_error = None
    for attempt in range(10):
        try:
            db.ping()
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            time.sleep(1.0)
    if last_error is not None:
        log_event("database unreachable at startup", level="error",
                  error={"type": type(last_error).__name__})
        return

    try:
        db.ensure_schema()
    except Exception as exc:
        # The gRPC service runs the same migration; losing that race is harmless.
        log_event("schema migration skipped", level="warn",
                  error={"type": type(exc).__name__, "message": str(exc)[:200]})
    refresh_stock_gauge()
    refresh_open_orders_gauge()
    log_event("service started", service_version="2.0.0")


@app.on_event("shutdown")
def shutdown():
    log_event("service stopping")
    db.close_pool()


app.include_router(router)
