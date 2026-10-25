"""Placing an order: the business rules, shared by HTTP and gRPC.

`POST /api/orders` (routes.py) and `OrderService.PlaceOrder` (grpc_server.py)
are both thin adapters over `place_order()` below. Validation, the stock
check, the payment call, the idempotency check, the business metrics and the
business log line all live here, against the one Postgres database, so the two
interfaces cannot drift apart. Each adapter only translates the request in and
an `OrderError` out into its own status codes.
"""

import hashlib
import json
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

from . import clients, db
from .structured_logging import log_event
from .telemetry import (
    CHECKOUT_DURATION,
    IDEMPOTENT_REPLAYS,
    ITEMS_PER_ORDER,
    ORDERS_PLACED,
    ORDERS_REJECTED,
    ORDER_VALUE,
    REVENUE,
)


# ------------------------------------------------------------------ request
# The validation rules. The HTTP adapter receives these models from FastAPI;
# the gRPC adapter builds the same models from the protobuf message, so a bad
# request is rejected by exactly the same code on both paths.
class CartLine(BaseModel):
    sku: str = Field(min_length=1, max_length=16)
    quantity: int = Field(gt=0, le=20)


class OrderRequest(BaseModel):
    items: list[CartLine] = Field(min_length=1, max_length=20)
    # A display name only. Never an email, phone number or address -- those are
    # personal data and this value reaches the log index.
    customer: str = Field(default="guest", min_length=1, max_length=40)
    payment_method: str = Field(default="card", pattern="^(card|wallet|cod)$")


# Idempotency keys: opaque, caller-chosen, e.g. a UUID. Bounded in size so a
# caller cannot store arbitrary data in our database through them.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_\-]{8,64}$"


# ----------------------------------------------------------------- outcomes
@dataclass
class PlacedOrder:
    order_id: int
    status: str
    total_cents: int
    item_count: int
    replayed: bool
    request_id: str

    @property
    def total_dollars(self) -> float:
        return round(self.total_cents / 100.0, 2)


class OrderError(Exception):
    """A business reason the order was not placed.

    `code` is stable and machine-readable; each adapter maps it to a status
    (see HTTP_STATUS in routes.py and GRPC_STATUS in grpc_server.py).
    """

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def request_fingerprint(order: OrderRequest) -> str:
    """Hash of what was asked for, stored next to the idempotency key.

    Lets a replay tell "the same request, retried" (return the original order)
    from "a different request reusing a key" (refuse: the caller has a bug).
    """
    canonical = json.dumps(order.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _find_by_key(key: str):
    with db.cursor("find_idempotent") as cur:
        cur.execute(
            "SELECT id, status, total_cents, item_count, request_hash "
            "FROM orders WHERE idempotency_key = %s",
            (key,),
        )
        return cur.fetchone()


def _replay(row, fingerprint: str, interface: str, request_id: str) -> PlacedOrder:
    if row["request_hash"] != fingerprint:
        ORDERS_REJECTED.labels(reason="idempotency_key_reused").inc()
        log_event(
            "checkout rejected: idempotency key reused",
            level="warn",
            event={"action": "checkout", "outcome": "rejected",
                   "reason": "idempotency_key_reused"},
            order={"id": row["id"]},
            interface=interface,
            request_id=request_id,
        )
        raise OrderError("idempotency_key_reused",
                         "this Idempotency-Key was already used for a different order request")
    IDEMPOTENT_REPLAYS.labels(interface=interface).inc()
    log_event(
        "order replayed",
        event={"action": "checkout", "outcome": "replayed"},
        order={"id": row["id"]},
        interface=interface,
        request_id=request_id,
    )
    return PlacedOrder(order_id=row["id"], status=row["status"],
                       total_cents=row["total_cents"], item_count=row["item_count"],
                       replayed=True, request_id=request_id)


def _reject(reason: str, detail: str, interface: str, request_id: str,
            level: str = "warn", **fields) -> OrderError:
    ORDERS_REJECTED.labels(reason=reason).inc()
    # Same messages as Assignment 1, so the saved Kibana searches still match.
    message = ("checkout rejected: payment not approved" if reason.startswith("payment_")
               else f"checkout rejected: {reason.replace('_', ' ')}")
    log_event(
        message,
        level=level,
        event={"action": "checkout", "outcome": "rejected", "reason": reason},
        interface=interface,
        request_id=request_id,
        **fields,
    )
    return OrderError(reason, detail)


# -------------------------------------------------------------------- place
def place_order(order: OrderRequest, *, request_id: str, interface: str,
                idempotency_key: str | None = None) -> PlacedOrder:
    """Reserve stock, charge payment and persist one order - or explain why not.

    Every exit records shop_checkout_duration_seconds{outcome=...} and exactly
    one business log line, so the funnel adds up:
        placed + replayed + rejected == checkout attempts.

    Idempotency: with a key, the order row stores the key and a fingerprint of
    the request. A repeat finds that row and returns it without touching stock
    or payment. Two concurrent calls with one key cannot both succeed: the
    unique index makes the second INSERT wait for the first transaction, then
    fail, and that failure becomes a replay. Payment is only called after the
    INSERT, so the loser never charges. A failed attempt rolls back and leaves
    no row, so retrying it places the order normally.

    Raises OrderError for business rejections; db.PoolTimeout and unexpected
    exceptions propagate to the adapter.
    """
    started = time.perf_counter()
    outcome = "error"
    fingerprint = request_fingerprint(order) if idempotency_key else None

    try:
        if idempotency_key:
            existing = _find_by_key(idempotency_key)
            if existing is not None:
                result = _replay(existing, fingerprint, interface, request_id)
                outcome = "replayed"
                return result

        skus = [line.sku for line in order.items]
        try:
            with db.cursor("create_order", commit=False) as cur:
                # FOR UPDATE takes a row lock so two concurrent checkouts cannot
                # both sell the last unit. ORDER BY sku makes every transaction
                # lock rows in the same order; without it two baskets holding
                # the same SKUs in a different order deadlock (A1 Part E.1).
                cur.execute(
                    "SELECT sku, name, price_cents, stock FROM products "
                    "WHERE sku = ANY(%s) ORDER BY sku FOR UPDATE",
                    (skus,),
                )
                found = {row["sku"]: row for row in cur.fetchall()}

                missing = [sku for sku in skus if sku not in found]
                if missing:
                    outcome = "rejected"
                    raise _reject("unknown_sku", f"unknown sku: {', '.join(missing)}",
                                  interface, request_id, order={"skus": missing})

                short = [line.sku for line in order.items
                         if found[line.sku]["stock"] < line.quantity]
                if short:
                    outcome = "rejected"
                    raise _reject("out_of_stock", f"out of stock: {', '.join(short)}",
                                  interface, request_id, order={"skus": short})

                total_cents = sum(found[line.sku]["price_cents"] * line.quantity
                                  for line in order.items)
                item_count = sum(line.quantity for line in order.items)

                cur.execute(
                    "INSERT INTO orders (status, total_cents, item_count, customer, "
                    "idempotency_key, request_hash) "
                    "VALUES ('PLACED', %s, %s, %s, %s, %s) RETURNING id",
                    (total_cents, item_count, order.customer, idempotency_key, fingerprint),
                )
                order_id = cur.fetchone()["id"]
                for line in order.items:
                    cur.execute(
                        "INSERT INTO order_items (order_id, sku, quantity, price_cents) "
                        "VALUES (%s, %s, %s, %s)",
                        (order_id, line.sku, line.quantity, found[line.sku]["price_cents"]),
                    )
                    cur.execute(
                        "UPDATE products SET stock = stock - %s WHERE sku = %s",
                        (line.quantity, line.sku),
                    )

                # Charge. Still inside the transaction: if payment fails the
                # whole thing rolls back, so stock is never silently lost.
                pay_outcome, pay_detail = clients.charge(
                    order_id=order_id,
                    amount_cents=total_cents,
                    method=order.payment_method,
                    request_id=request_id,
                )

                if pay_outcome != clients.PaymentOutcome.APPROVED:
                    reason = {
                        clients.PaymentOutcome.DECLINED: "payment_declined",
                        clients.PaymentOutcome.TIMEOUT: "payment_timeout",
                    }.get(pay_outcome, "payment_error")
                    outcome = "rejected"
                    # Rolled back by the context manager (commit=False).
                    raise _reject(
                        reason, reason.replace("_", " "), interface, request_id,
                        level="warn" if reason == "payment_declined" else "error",
                        order={"id": order_id,
                               "value_dollars": round(total_cents / 100.0, 2)},
                        payment={"outcome": pay_outcome, **pay_detail},
                    )

                cur.connection.commit()
        except db.IntegrityError:
            # Lost the race for this idempotency key (see docstring).
            if not idempotency_key:
                raise
            existing = _find_by_key(idempotency_key)
            if existing is None:
                raise
            result = _replay(existing, fingerprint, interface, request_id)
            outcome = "replayed"
            return result

        outcome = "placed"
        value_dollars = total_cents / 100.0
        ORDERS_PLACED.labels(payment_method=order.payment_method).inc()
        ORDER_VALUE.observe(value_dollars)
        ITEMS_PER_ORDER.observe(item_count)
        REVENUE.inc(value_dollars)
        log_event(
            "order placed",
            event={"action": "checkout", "outcome": "placed"},
            order={"id": order_id, "value_dollars": round(value_dollars, 2),
                   "item_count": item_count},
            payment={"method": order.payment_method, **pay_detail},
            interface=interface,
            request_id=request_id,
        )
        return PlacedOrder(order_id=order_id, status="PLACED", total_cents=total_cents,
                           item_count=item_count, replayed=False, request_id=request_id)
    finally:
        CHECKOUT_DURATION.labels(outcome=outcome).observe(time.perf_counter() - started)
