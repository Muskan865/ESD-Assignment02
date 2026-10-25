"""gRPC OrderService - the second interface to placing an order (A2 Part B).

Runs as its own process and container (`order-grpc`), built from this same
code base: the handler below calls the same `orders.place_order()` as HTTP
`POST /api/orders`, against the same Postgres database and the same payment
service. It is not a wrapper around the HTTP endpoint - no HTTP request to the
storefront is ever made.

    python -m app.grpc_server         # gRPC on :50051, Prometheus on :9102

Observability, kept consistent with the HTTP service:
  * request id: read from the `x-request-id` call metadata (minted if absent),
    echoed in the initial response metadata, written on every log line and
    forwarded to payment-service as the `x-request-id` HTTP header, so one id
    follows a call client -> order-grpc -> payment-service in Kibana;
  * logs: one ECS-shaped JSON line per RPC ("rpc completed") plus the same
    business log lines the HTTP path writes;
  * metrics: grpc_server_* (telemetry.py), recorded by the interceptor.
"""

import contextvars
import re
import signal
import threading
import time
import traceback
import uuid
from concurrent import futures

import grpc
from prometheus_client import REGISTRY, start_http_server
from pydantic import ValidationError

from . import db, telemetry
from .config import GRPC_MAX_WORKERS, GRPC_METRICS_PORT, GRPC_PORT
from .errors import validation_errors
from .grpc_gen import orders_pb2, orders_pb2_grpc
from .orders import IDEMPOTENCY_KEY_PATTERN, OrderError, OrderRequest, place_order
from .structured_logging import log_event

Code = grpc.StatusCode

# Business rejection -> gRPC status. The HTTP equivalent is HTTP_STATUS in
# routes.py. The code itself travels in the `error-code` trailer.
GRPC_STATUS = {
    "invalid_request": Code.INVALID_ARGUMENT,         # HTTP 422
    "idempotency_key_reused": Code.INVALID_ARGUMENT,  # HTTP 422
    "unknown_sku": Code.NOT_FOUND,                    # HTTP 422
    "out_of_stock": Code.FAILED_PRECONDITION,         # HTTP 409
    "payment_declined": Code.FAILED_PRECONDITION,     # HTTP 402
    "payment_error": Code.UNAVAILABLE,                # HTTP 502
    "payment_timeout": Code.DEADLINE_EXCEEDED,        # HTTP 504
    "database_busy": Code.UNAVAILABLE,                # HTTP 503
    "internal_error": Code.INTERNAL,                  # HTTP 500
}

# Codes that mean "the server or its dependencies failed", logged as errors.
# The rest are the caller's problem or a business outcome.
SERVER_FAULT_CODES = {Code.UNKNOWN, Code.INTERNAL, Code.UNAVAILABLE,
                      Code.DEADLINE_EXCEEDED, Code.DATA_LOSS, Code.UNIMPLEMENTED}

_PAYMENT_METHODS = {
    orders_pb2.PAYMENT_METHOD_UNSPECIFIED: "card",  # the HTTP default
    orders_pb2.PAYMENT_METHOD_CARD: "card",
    orders_pb2.PAYMENT_METHOD_WALLET: "wallet",
    orders_pb2.PAYMENT_METHOD_COD: "cod",
}

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def _abort(context: grpc.ServicerContext, code: str, detail: str):
    """Fail the RPC with the mapped status; never returns."""
    context.set_trailing_metadata((("error-code", code),))
    context.abort(GRPC_STATUS[code], detail)


# ------------------------------------------------------------------ handler
class OrderService(orders_pb2_grpc.OrderServiceServicer):

    def PlaceOrder(self, request, context):
        request_id = _REQUEST_ID.get()

        key = request.idempotency_key or None
        if key is not None and not re.fullmatch(IDEMPOTENCY_KEY_PATTERN, key):
            _abort(context, "invalid_request",
                   "idempotency_key must be 8-64 characters [A-Za-z0-9_-]")
        try:
            # The same pydantic model FastAPI validates the HTTP body with.
            order = OrderRequest(
                items=[{"sku": line.sku, "quantity": line.quantity} for line in request.items],
                # proto3 cannot tell an unset string from "": treat both as unset.
                customer=request.customer or "guest",
                payment_method=_PAYMENT_METHODS.get(request.payment_method, "unknown"),
            )
        except ValidationError as exc:
            problems = "; ".join(f"{e['field']}: {e['message']}"
                                 for e in validation_errors(exc.errors()))
            _abort(context, "invalid_request", f"request failed validation: {problems}")

        try:
            placed = place_order(order, request_id=request_id, interface="grpc",
                                 idempotency_key=key)
        except OrderError as exc:
            _abort(context, exc.code, exc.detail)
        except db.PoolTimeout as exc:
            log_event("database busy", level="error",
                      error={"type": "PoolTimeout", "message": str(exc)},
                      request_id=request_id)
            _abort(context, "database_busy", "database busy, retry")

        return orders_pb2.PlaceOrderResponse(
            order_id=placed.order_id,
            status=placed.status,
            total_cents=placed.total_cents,
            item_count=placed.item_count,
            replayed=placed.replayed,
            request_id=request_id,
        )


# -------------------------------------------------------------- interceptor
def _split_method(full_method: str) -> tuple[str, str]:
    # "/tinyshop.orders.v1.OrderService/PlaceOrder"
    service, _, method = full_method.lstrip("/").partition("/")
    return service, method


class ObservabilityInterceptor(grpc.ServerInterceptor):
    """Request id, metrics and one log line for every RPC.

    The gRPC counterpart of the `observe` HTTP middleware in application.py.
    Duration is measured here, on the server: from the call reaching the
    interceptor to the handler returning or aborting.
    """

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            # Unknown method: gRPC answers UNIMPLEMENTED. Not recorded, so a
            # client cannot create label values by calling made-up methods.
            return handler

        service, method = _split_method(handler_call_details.method)
        metadata = {m.key: m.value for m in (handler_call_details.invocation_metadata or ())}
        inner = handler.unary_unary

        def observed(request, context):
            # Bounded, so a caller cannot write arbitrary data into our logs.
            request_id = (metadata.get("x-request-id") or uuid.uuid4().hex[:12])[:64]
            token = _REQUEST_ID.set(request_id)
            context.send_initial_metadata((("x-request-id", request_id),))
            telemetry.GRPC_IN_FLIGHT.labels(service, method).inc()
            started = time.perf_counter()
            try:
                return inner(request, context)
            except Exception as exc:
                if context.code() is None:
                    # Not an abort: a bug. One structured line with the whole
                    # stack, then a clean INTERNAL instead of gRPC's UNKNOWN.
                    log_event(
                        "unhandled exception",
                        level="error",
                        error={"type": type(exc).__name__,
                               "message": str(exc).splitlines()[0][:200] if str(exc) else "",
                               "stack_trace": traceback.format_exc()},
                        rpc={"system": "grpc", "service": service, "method": method},
                        request_id=request_id,
                    )
                    _abort(context, "internal_error", "internal error")
                raise
            finally:
                duration = time.perf_counter() - started
                code = context.code() or Code.OK
                telemetry.GRPC_IN_FLIGHT.labels(service, method).dec()
                telemetry.GRPC_HANDLED.labels(service, method, code.name).inc()
                telemetry.GRPC_HANDLING.labels(service, method).observe(duration)
                log_event(
                    "rpc completed",
                    level="error" if code in SERVER_FAULT_CODES else "info",
                    rpc={"system": "grpc", "service": service, "method": method,
                         "grpc": {"status_code": code.name}},
                    event={"duration_ms": round(duration * 1000, 2)},
                    request_id=request_id,
                )
                _REQUEST_ID.reset(token)

        return grpc.unary_unary_rpc_method_handler(
            observed,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


# ------------------------------------------------------------------ process
def build_server(port: int = GRPC_PORT, max_workers: int = GRPC_MAX_WORKERS):
    """Return (server, bound_port). Port 0 picks a free port (used by tests)."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rpc"),
        interceptors=[ObservabilityInterceptor()],
    )
    orders_pb2_grpc.add_OrderServiceServicer_to_server(OrderService(), server)
    bound = server.add_insecure_port(f"[::]:{port}")
    return server, bound


def _expose_only_owned_metrics() -> None:
    """Drop instruments this process never writes from its /metrics.

    telemetry.py declares every storefront metric at import. The HTTP ones
    would sit at 0 here, and the two DB-derived gauges are published by the
    storefront alone (re-read from Postgres at scrape time), so exposing them
    twice would double-count any sum() across jobs.
    """
    for collector in (telemetry.REQUEST_COUNT, telemetry.REQUEST_LATENCY,
                      telemetry.IN_FLIGHT, telemetry.ORDERS_AWAITING_FULFILMENT,
                      telemetry.STOCK_UNITS, telemetry.DEMO_REQUESTS,
                      telemetry.DEMO_REQUESTS_SAFE):
        try:
            REGISTRY.unregister(collector)
        except KeyError:
            pass


def _connect_database() -> None:
    db.init_pool()
    last_error = None
    for _ in range(10):
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
        log_event("schema migration skipped", level="warn",
                  error={"type": type(exc).__name__, "message": str(exc)[:200]})


def main() -> None:
    _expose_only_owned_metrics()
    start_http_server(GRPC_METRICS_PORT)
    _connect_database()

    server, port = build_server()
    server.start()
    log_event("service started", service_version="2.0.0",
              grpc={"port": port, "max_workers": GRPC_MAX_WORKERS},
              metrics={"port": GRPC_METRICS_PORT})

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    log_event("service stopping")
    # Finish in-flight RPCs (up to 5 s), refuse new ones.
    server.stop(grace=5).wait()
    db.close_pool()


if __name__ == "__main__":
    main()
