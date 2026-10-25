"""Shop endpoints: thin HTTP adapters over the business rules.

Placing an order lives in orders.py, shared with the gRPC service; this module
translates HTTP in and out of it. Cancel, fulfil and the read endpoints are
HTTP-only and keep their logic here.
"""

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, Field

from . import clients, db
from .config import ORDERS_RATE_LIMIT_BURST, ORDERS_RATE_LIMIT_PER_SECOND
from .errors import ApiError, problem_responses
from .orders import IDEMPOTENCY_KEY_PATTERN, OrderError, OrderRequest, place_order
from .ratelimit import TokenBucketLimiter
from .state import LOCK, STATE, reset as reset_state, snapshot
from .structured_logging import log_event
from .telemetry import (
    DEMO_HAS_ID_LABEL,
    DEMO_REQUESTS,
    DEMO_REQUESTS_SAFE,
    ORDERS_AWAITING_FULFILMENT,
    ORDERS_CANCELLED,
    STOCK_UNITS,
)

router = APIRouter()

# Business rejection -> HTTP status. The gRPC equivalent is GRPC_STATUS in
# grpc_server.py; REPORT-A2.md explains each pair.
HTTP_STATUS = {
    "unknown_sku": 422,             # the body names a product that does not exist
    "out_of_stock": 409,            # valid request, conflicts with current stock
    "payment_declined": 402,
    "payment_error": 502,           # our upstream (payment-service) failed
    "payment_timeout": 504,         # our upstream did not answer in time
    "idempotency_key_reused": 422,  # same key, different request body
}

# Postgres ids are 32-bit; anything larger cannot exist and would otherwise
# reach the database and fail there as a 500.
MAX_ID = 2_147_483_647


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


# ------------------------------------------------------------------ schemas
class OrderCreated(BaseModel):
    order_id: int
    status: str
    total_dollars: float
    item_count: int
    replayed: bool
    request_id: str


class Order(BaseModel):
    id: int
    status: str
    total_cents: int
    item_count: int
    customer: str
    created_at: str


class OrderList(BaseModel):
    orders: list[Order]


class OrderStatus(BaseModel):
    order_id: int
    status: str


class Product(BaseModel):
    sku: str
    name: str
    price_cents: int
    stock: int


class ProductList(BaseModel):
    products: list[Product]


# ------------------------------------------------------------ gauge helpers
def refresh_stock_gauge() -> None:
    """Re-publish shop_stock_units from the database.

    A Gauge is current state, not an event stream, so the safe way to keep it
    honest is to read the source of truth rather than to increment blindly.
    """
    with db.cursor("list_products") as cur:
        cur.execute("SELECT sku, stock FROM products")
        for row in cur.fetchall():
            STOCK_UNITS.labels(sku=row["sku"]).set(row["stock"])


def refresh_open_orders_gauge() -> None:
    """Re-publish shop_orders_awaiting_fulfilment from the database.

    Read, never incremented: since Assignment 2 the gRPC service also places
    orders, and an in-process +1/-1 here would never see those.
    """
    with db.cursor("count_open") as cur:
        cur.execute("SELECT COUNT(*) AS n FROM orders WHERE status = 'PLACED'")
        ORDERS_AWAITING_FULFILMENT.set(cur.fetchone()["n"])


def _order_row(row) -> dict:
    return {**row, "created_at": str(row["created_at"])}


# ------------------------------------------------------------------- catalog
@router.get("/api/products", tags=["shop"], response_model=ProductList,
            responses=problem_responses(503))
def list_products():
    """The product catalogue shown on the storefront page."""
    with db.cursor("list_products") as cur:
        cur.execute("SELECT sku, name, price_cents, stock FROM products ORDER BY name")
        products = cur.fetchall()
    return {"products": products}


# -------------------------------------------------------------- rate limiting
ORDER_LIMITER = TokenBucketLimiter(ORDERS_RATE_LIMIT_PER_SECOND, ORDERS_RATE_LIMIT_BURST)


def limit_order_creation(request: Request, response: Response) -> None:
    """Spend one token from the caller's bucket, or refuse with 429.

    Keyed on the client IP as the server sees it. Every caller behind one
    address shares a bucket - including, under Docker Desktop, everything on
    the host machine, which reaches the container through one gateway address.
    Runs before the body is parsed, so even an invalid request spends a token.
    """
    client = request.client.host if request.client else "unknown"
    decision = ORDER_LIMITER.take(client)
    headers = {"X-RateLimit-Limit": str(decision.limit),
               "X-RateLimit-Remaining": str(decision.remaining)}
    if not decision.allowed:
        log_event("rate limited", level="warn",
                  event={"action": "checkout", "outcome": "rejected",
                         "reason": "rate_limited"},
                  request_id=_request_id(request))
        raise ApiError(
            429, "rate_limited",
            f"too many order requests from this client: {ORDER_LIMITER.burst} burst, "
            f"{ORDER_LIMITER.rate:g} per second; retry after {decision.retry_after}s",
            headers={"Retry-After": str(decision.retry_after), **headers},
        )
    response.headers.update(headers)


# -------------------------------------------------------------------- orders
@router.post(
    "/api/orders", tags=["orders"], status_code=201, response_model=OrderCreated,
    dependencies=[Depends(limit_order_creation)],
    responses=problem_responses(402, 409, 422, 429, 502, 503, 504),
)
def create_order(
    payload: OrderRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", pattern=IDEMPOTENCY_KEY_PATTERN,
        description="Optional, 8-64 characters [A-Za-z0-9_-], e.g. a UUID. Repeating "
                    "a request with the same key returns the original order (with "
                    "`Idempotent-Replayed: true`) instead of placing and charging a "
                    "second one. Reusing a key for a different body is a 422."),
):
    """Place an order: reserve stock, charge payment, persist it.

    Replaces Assignment 1's `POST /api/checkout`. Rate limited per client IP.
    """
    request_id = _request_id(request)
    try:
        placed = place_order(payload, request_id=request_id, interface="http",
                             idempotency_key=idempotency_key)
    except OrderError as exc:
        raise ApiError(HTTP_STATUS[exc.code], exc.code, exc.detail) from exc

    refresh_stock_gauge()
    refresh_open_orders_gauge()
    response.headers["Location"] = f"/api/orders/{placed.order_id}"
    if placed.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return {
        "order_id": placed.order_id,
        "status": placed.status,
        "total_dollars": placed.total_dollars,
        "item_count": placed.item_count,
        "replayed": placed.replayed,
        "request_id": request_id,
    }


@router.get("/api/orders", tags=["orders"], response_model=OrderList,
            responses=problem_responses(422, 503))
def list_orders(limit: int = Query(20, ge=1, le=100, description="1-100, newest first")):
    """Most recent orders, for the storefront page."""
    with db.cursor("list_orders") as cur:
        cur.execute(
            "SELECT id, status, total_cents, item_count, customer, created_at "
            "FROM orders ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        orders = [_order_row(row) for row in cur.fetchall()]
    return {"orders": orders}


@router.get("/api/orders/{order_id}", tags=["orders"], response_model=Order,
            responses=problem_responses(404, 422, 503))
def get_order(order_id: int = Path(gt=0, le=MAX_ID)):
    """One order. The `Location` returned by `POST /api/orders` points here."""
    with db.cursor("get_order") as cur:
        cur.execute(
            "SELECT id, status, total_cents, item_count, customer, created_at "
            "FROM orders WHERE id = %s",
            (order_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ApiError(404, "order_not_found", f"no order {order_id}")
    return _order_row(row)


@router.post("/api/orders/{order_id}/cancel", tags=["orders"], response_model=OrderStatus,
             responses=problem_responses(404, 409, 422, 503))
def cancel_order(request: Request, order_id: int = Path(gt=0, le=MAX_ID)):
    """Cancel a PLACED order and return its stock to the shelf.

    Idempotent: cancelling an already-cancelled order answers 200 and changes
    nothing, so a retry after a lost response is safe. A FULFILLED order
    cannot be cancelled (409).
    """
    request_id = _request_id(request)
    with db.cursor("cancel_order", commit=True) as cur:
        # The row lock serialises concurrent cancels: only the first sees PLACED.
        cur.execute(
            "SELECT status, total_cents FROM orders WHERE id = %s FOR UPDATE",
            (order_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ApiError(404, "order_not_found", f"no order {order_id}")
        if row["status"] == "CANCELLED":
            return {"order_id": order_id, "status": "CANCELLED"}
        if row["status"] != "PLACED":
            raise ApiError(409, "order_not_cancellable",
                           f"order {order_id} is {row['status']} and cannot be cancelled")

        # Lock the product rows in sku order first, the same order checkout
        # uses, so a cancel and a checkout cannot deadlock each other.
        cur.execute(
            "SELECT p.sku FROM products p JOIN order_items oi ON oi.sku = p.sku "
            "WHERE oi.order_id = %s ORDER BY p.sku FOR UPDATE OF p",
            (order_id,),
        )
        cur.execute(
            "UPDATE products p SET stock = p.stock + oi.quantity "
            "FROM order_items oi WHERE oi.order_id = %s AND oi.sku = p.sku",
            (order_id,),
        )
        cur.execute(
            "UPDATE orders SET status = 'CANCELLED', updated_at = now() WHERE id = %s",
            (order_id,),
        )
        total_cents = row["total_cents"]

    ORDERS_CANCELLED.labels(reason="customer_request").inc()
    refresh_open_orders_gauge()
    refresh_stock_gauge()
    log_event(
        "order cancelled",
        level="warn",
        event={"action": "cancel", "outcome": "cancelled",
               "reason": "customer_request"},
        order={"id": order_id, "value_dollars": round(total_cents / 100.0, 2)},
        request_id=request_id,
    )
    return {"order_id": order_id, "status": "CANCELLED"}


@router.post("/api/orders/{order_id}/fulfil", tags=["orders"], response_model=OrderStatus,
             responses=problem_responses(404, 409, 422, 503))
def fulfil_order(request: Request, order_id: int = Path(gt=0, le=MAX_ID)):
    """Mark a PLACED order shipped. Idempotent, like cancel."""
    request_id = _request_id(request)
    with db.cursor("fulfil_order", commit=True) as cur:
        cur.execute("SELECT status FROM orders WHERE id = %s FOR UPDATE", (order_id,))
        row = cur.fetchone()
        if row is None:
            raise ApiError(404, "order_not_found", f"no order {order_id}")
        if row["status"] == "FULFILLED":
            return {"order_id": order_id, "status": "FULFILLED"}
        if row["status"] != "PLACED":
            raise ApiError(409, "order_not_fulfillable",
                           f"order {order_id} is {row['status']} and cannot be fulfilled")
        cur.execute(
            "UPDATE orders SET status = 'FULFILLED', updated_at = now() WHERE id = %s",
            (order_id,),
        )

    refresh_open_orders_gauge()
    log_event(
        "order fulfilled",
        event={"action": "fulfil", "outcome": "fulfilled"},
        order={"id": order_id},
        request_id=request_id,
    )
    return {"order_id": order_id, "status": "FULFILLED"}


# ------------------------------------------------------- experiment controls
# Operator-only, unchanged in Assignment 2 apart from `fail_next`.
class LatencyFault(BaseModel):
    probability: float = Field(ge=0.0, le=1.0)
    seconds: float = Field(default=0.5, ge=0.0, le=10.0)


class FailureFault(BaseModel):
    rate: float = Field(ge=0.0, le=1.0)
    # Fail exactly the next N charges, then behave normally again. Used by the
    # Assignment 2 caller demo to show one deterministic retry.
    fail_next: int = Field(default=0, ge=0, le=1000)


class CardinalityToggle(BaseModel):
    active: bool


@router.post("/admin/chaos/payment-latency", tags=["experiments"])
def payment_latency(fault: LatencyFault):
    """Part E.1 -- delay a fraction of payment calls (forwarded downstream)."""
    result = clients.post_chaos("/chaos/latency", fault.model_dump())
    log_event("experiment: payment latency fault set", level="warn",
              fault=fault.model_dump())
    return result


@router.post("/admin/chaos/payment-failure", tags=["experiments"])
def payment_failure(fault: FailureFault):
    """Part E.1 -- fail a fraction (or the next N) of payment calls with HTTP 503."""
    result = clients.post_chaos("/chaos/failure", fault.model_dump())
    log_event("experiment: payment failure fault set", level="warn",
              fault=fault.model_dump())
    return result


@router.post("/admin/chaos/cardinality", tags=["experiments"])
def cardinality(toggle: CardinalityToggle):
    """Part E.2 -- start/stop writing the unbounded request_id metric label.

    Switching it OFF stops new series being created but deliberately does not
    delete the ones already made: that asymmetry is the lesson. Use
    /admin/chaos/reset to actually clear them.
    """
    with LOCK:
        STATE["cardinality_demo"] = toggle.active
    log_event("experiment: cardinality demo toggled", level="warn",
              active=toggle.active)
    return snapshot()


@router.post("/admin/chaos/reset", tags=["experiments"])
def reset_everything():
    """Clear every injected fault in both services. Always safe to call.

    This also drops the label children the cardinality demo created, so the
    experiment can be re-run. Note that removing them from /metrics does NOT
    delete what Prometheus already stored -- those series stay until they go
    stale and retention removes them.
    """
    payment = clients.post_chaos("/chaos/reset", {})
    local = reset_state()
    if DEMO_HAS_ID_LABEL:
        DEMO_REQUESTS.clear()
    log_event("experiment: reset", level="warn")
    return {"storefront": local, "payment_service": payment}


@router.get("/admin/chaos/status", tags=["experiments"])
def chaos_status():
    return {"storefront": snapshot(), "payment_service": clients.get_chaos_status()}


class RestockRequest(BaseModel):
    level: int = Field(default=500, ge=0, le=100000)


@router.post("/admin/restock", tags=["experiments"])
def restock(payload: RestockRequest, request: Request):
    """Set every SKU back to `level` units.

    A load test places hundreds of orders in a minute and would otherwise empty
    the shelves, after which every checkout fails with out_of_stock and the
    experiment measures the wrong thing. The traffic generator calls this
    periodically so the Part E fault is the only variable that changes.
    """
    request_id = _request_id(request)
    with db.cursor("restock", commit=True) as cur:
        # Same sku lock order as checkout and cancel.
        cur.execute("SELECT sku FROM products ORDER BY sku FOR UPDATE")
        cur.execute("UPDATE products SET stock = %s", (payload.level,))
    refresh_stock_gauge()
    log_event(
        "inventory restocked",
        event={"action": "restock", "outcome": "ok"},
        inventory={"level": payload.level},
        request_id=request_id,
    )
    return {"status": "ok", "level": payload.level}


@router.post("/admin/demo/safe-counter", tags=["experiments"])
def safe_counter(tier: str = "standard"):
    """Part E.2 step 3 -- the bounded replacement for the demo counter."""
    tier = tier if tier in ("standard", "vip") else "standard"
    DEMO_REQUESTS_SAFE.labels(tier=tier).inc()
    return {"tier": tier}
