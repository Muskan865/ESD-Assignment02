"""Smoke-test storefront-api with a stubbed Postgres and a stubbed payment client.

This exercises the checkout control flow, metric recording and log emission.
It does NOT validate the SQL itself - that is checked against real Postgres.
"""
import contextlib
import io
import json
import sys

from fake_shop import (  # noqa: E402  (stubs psycopg2 and patches the app)
    PAYMENT_MODE, PRODUCT_LOCKS, check, fake_cursor, finish, metric_value,
    real_db_cursor,
)
from fastapi.testclient import TestClient  # noqa: E402
from app import clients, db  # noqa: E402
from app.application import app  # noqa: E402


# silence the JSON log lines during the run, but capture them for inspection
log_buffer = io.StringIO()
real_stdout = sys.stdout

with TestClient(app) as c:
    m0 = c.get("/metrics").text

    r = c.get("/health")
    check("GET /health -> 200", r.status_code == 200)

    r = c.get("/ready")
    check("GET /ready -> 200", r.status_code == 200, r.json())

    r = c.get("/")
    check("GET / serves the storefront page", r.status_code == 200 and "Tiny Shop" in r.text)

    r = c.get("/api/products")
    check("GET /api/products lists products", r.status_code == 200
          and len(r.json()["products"]) == 3)

    # --- happy path ---
    placed0 = metric_value(m0, 'shop_orders_placed_total{payment_method="card"}') or 0
    r = c.post("/api/orders", json={"items": [{"sku": "KB-01", "quantity": 2}],
                                      "customer": "ayesha", "payment_method": "card"})
    check("POST /api/orders -> 201", r.status_code == 201, r.json())
    body = r.json()
    check("checkout returns order_id + total", body.get("total_dollars") == 98.0, body)
    check("checkout returns a request_id", len(body.get("request_id", "")) == 12, body)
    order_id = body["order_id"]

    m = c.get("/metrics").text
    check("shop_orders_placed_total incremented",
          metric_value(m, 'shop_orders_placed_total{payment_method="card"}') == placed0 + 1)
    check("shop_revenue_dollars_total incremented",
          metric_value(m, "shop_revenue_dollars_total") == 98.0)
    check("shop_orders_awaiting_fulfilment == 1",
          metric_value(m, "shop_orders_awaiting_fulfilment") == 1.0)
    check("shop_order_value_dollars (Histogram) recorded",
          metric_value(m, "shop_order_value_dollars_count") == 1.0)
    check("shop_items_per_order (Summary) recorded",
          metric_value(m, "shop_items_per_order_count") == 1.0)
    check("shop_checkout_duration_seconds{outcome=placed} recorded",
          metric_value(m, 'shop_checkout_duration_seconds_count{outcome="placed"}') == 1.0)
    check("stock gauge reflects the sale",
          metric_value(m, 'shop_stock_units{sku="KB-01"}') == 38.0)
    check("Summary type present for payment_call_duration_seconds",
          "payment_call_duration_seconds" in m)

    # --- out of stock ---
    r = c.post("/api/orders", json={"items": [{"sku": "OUT-01", "quantity": 1}]})
    check("out-of-stock checkout -> 409", r.status_code == 409, r.status_code)
    m = c.get("/metrics").text
    check("rejected{reason=out_of_stock} recorded",
          metric_value(m, 'shop_orders_rejected_total{reason="out_of_stock"}') == 1.0)
    check("checkout duration recorded for rejection",
          metric_value(m, 'shop_checkout_duration_seconds_count{outcome="rejected"}') == 1.0)

    # --- unknown sku ---
    r = c.post("/api/orders", json={"items": [{"sku": "NOPE-99", "quantity": 1}]})
    check("unknown sku -> 422 (A2: was 404)", r.status_code == 422, r.status_code)

    # --- payment declined ---
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.DECLINED
    r = c.post("/api/orders", json={"items": [{"sku": "MS-01", "quantity": 1}]})
    check("declined payment -> 402", r.status_code == 402, r.status_code)
    m = c.get("/metrics").text
    check("rejected{reason=payment_declined} recorded",
          metric_value(m, 'shop_orders_rejected_total{reason="payment_declined"}') == 1.0)

    # --- payment error (the Part E failure mode) ---
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.ERROR
    r = c.post("/api/orders", json={"items": [{"sku": "MS-01", "quantity": 1}]})
    check("payment provider error -> 502", r.status_code == 502, r.status_code)
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.TIMEOUT
    r = c.post("/api/orders", json={"items": [{"sku": "MS-01", "quantity": 1}]})
    check("payment timeout -> 504 (A2: was 502)", r.status_code == 504, r.status_code)
    m = c.get("/metrics").text
    check("rejected{reason=payment_error} recorded",
          metric_value(m, 'shop_orders_rejected_total{reason="payment_error"}') == 1.0)
    check("rejected{reason=payment_timeout} recorded",
          metric_value(m, 'shop_orders_rejected_total{reason="payment_timeout"}') == 1.0)
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.APPROVED

    # --- cancel: gauge goes back down ---
    r = c.post(f"/api/orders/{order_id}/cancel")
    check("cancel order -> 200", r.status_code == 200, r.json())
    m = c.get("/metrics").text
    check("shop_orders_cancelled_total incremented",
          metric_value(m, 'shop_orders_cancelled_total{reason="customer_request"}') == 1.0)
    check("rejected checkouts left no order behind (gauge read from DB)",
          metric_value(m, "shop_orders_awaiting_fulfilment") == 0.0)
    check("awaiting_fulfilment gauge back to 0",
          metric_value(m, "shop_orders_awaiting_fulfilment") == 0.0)

    # --- fulfil ---
    r = c.post("/api/orders", json={"items": [{"sku": "MS-01", "quantity": 1}]})
    oid2 = r.json()["order_id"]
    r = c.post(f"/api/orders/{oid2}/fulfil")
    check("fulfil order -> 200", r.status_code == 200, r.json())
    m = c.get("/metrics").text
    check("awaiting_fulfilment gauge back to 0 after fulfil",
          metric_value(m, "shop_orders_awaiting_fulfilment") == 0.0)

    # --- double cancel is rejected ---
    r = c.post(f"/api/orders/{oid2}/cancel")
    check("cancelling a FULFILLED order -> 409", r.status_code == 409, r.status_code)

    # --- route templating: no order id in metric labels ---
    m = c.get("/metrics").text
    check('route template used, not raw id',
          'route="/api/orders/{order_id}/cancel"' in m)
    check("no raw order id leaked into metric labels", f'route="/api/orders/{oid2}' not in m)

    # --- restock ---
    r = c.post("/admin/restock", json={"level": 500})
    check("restock -> 200", r.status_code == 200, r.json())
    m = c.get("/metrics").text
    check("stock gauge updated by restock",
          metric_value(m, 'shop_stock_units{sku="KB-01"}') == 500.0)

    # --- cardinality demo off by default ---
    m = c.get("/metrics").text
    check("demo_requests_total has no series by default",
          'demo_requests_total{request_id=' not in m)

    r = c.post("/admin/chaos/cardinality", json={"active": True})
    check("cardinality toggle on -> 200", r.status_code == 200, r.json())
    for _ in range(5):
        c.get("/api/products")
    m = c.get("/metrics").text
    series = m.count("demo_requests_total{request_id=")
    check("cardinality demo creates one series per request", series >= 5, f"series={series}")

    c.post("/admin/chaos/cardinality", json={"active": False})
    before = m.count("demo_requests_total{request_id=")
    for _ in range(5):
        c.get("/api/products")
    m2 = c.get("/metrics").text
    check("toggling off stops NEW series",
          m2.count("demo_requests_total{request_id=") == before)

    for i in range(3):
        c.post("/admin/demo/safe-counter?tier=" + ("vip" if i == 0 else "standard"))
    m = c.get("/metrics").text
    check("bounded counter stays at 2 series",
          m.count("demo_requests_safe_total{tier=") == 2,
          m.count("demo_requests_safe_total{tier="))

    # --- toggling off and on again must NOT allow another 100 ---
    c.post("/admin/chaos/cardinality", json={"active": True})
    for _ in range(140):
        c.get("/api/products")
    m = c.get("/metrics").text
    capped = m.count("demo_requests_total{request_id=")
    check("cardinality caps at 100 DISTINCT ids across toggles", capped == 100,
          f"series={capped}")
    c.post("/admin/chaos/cardinality", json={"active": False})

    # --- reset clears the leaked label children, making a re-run possible ---
    r = c.post("/admin/chaos/reset")
    check("reset -> 200", r.status_code == 200, r.status_code)
    m = c.get("/metrics").text
    check("reset clears demo series from /metrics",
          m.count("demo_requests_total{request_id=") == 0,
          m.count("demo_requests_total{request_id="))
    c.post("/admin/chaos/cardinality", json={"active": True})
    for _ in range(120):
        c.get("/api/products")
    m = c.get("/metrics").text
    again = m.count("demo_requests_total{request_id=")
    check("experiment is re-runnable after reset (100 again)", again == 100,
          f"series={again}")
    c.post("/admin/chaos/reset")

    # --- step 3 of E.2: the same counter built WITHOUT the label ---
    from prometheus_client import CollectorRegistry, generate_latest
    from app.telemetry import make_demo_counter
    reg = CollectorRegistry()
    unlabelled = make_demo_counter("none", registry=reg)
    for _ in range(100):
        unlabelled.inc()
    text = generate_latest(reg).decode()
    samples = [ln for ln in text.splitlines() if ln.startswith("demo_requests_total")]
    check("label removed -> 100 requests make exactly 1 series",
          samples == ["demo_requests_total 100.0"], samples)

    # --- 404 collapses ---
    c.get("/api/orders/99999/nonexistent-endpoint")
    m = c.get("/metrics").text
    check('unmatched routes collapse', 'route="unmatched"' in m)

# --- every product row lock is taken in sku order (deadlock fix, Part E.1) ---
check("checkout, cancel and restock all lock products",
      any("sku = ANY" in q for q in PRODUCT_LOCKS)
      and any("JOIN order_items" in q for q in PRODUCT_LOCKS)
      and any(q.startswith("SELECT sku FROM products") for q in PRODUCT_LOCKS),
      len(PRODUCT_LOCKS))
check("every product lock is ORDER BY sku",
      all("ORDER BY sku FOR UPDATE" in q or "ORDER BY p.sku FOR UPDATE" in q
          for q in PRODUCT_LOCKS), [q for q in PRODUCT_LOCKS if "ORDER BY" not in q][:1])

# --- a crash inside a route is logged as structured JSON, not just a 500 ---
import logging  # noqa: E402


class PoolExhausted(Exception):
    pass


@contextlib.contextmanager
def exhausted_cursor(operation, commit=False):
    raise PoolExhausted("connection pool exhausted")
    yield


captured = io.StringIO()
capture = logging.StreamHandler(captured)
logging.getLogger("storefront-api").addHandler(capture)
with TestClient(app, raise_server_exceptions=False) as c:
    db.cursor = exhausted_cursor
    r = c.get("/api/products", headers={"x-request-id": "crash-test-1"})
    db.cursor = fake_cursor
logging.getLogger("storefront-api").removeHandler(capture)

check("crash -> 500", r.status_code == 500, r.status_code)
crash_logs = [json.loads(ln) for ln in captured.getvalue().splitlines()
              if '"unhandled exception"' in ln]
check("crash emits one 'unhandled exception' log", len(crash_logs) == 1, len(crash_logs))
check("crash response still carries x-request-id", r.headers.get("x-request-id") == "crash-test-1")
if crash_logs:
    doc = crash_logs[0]
    check("crash log carries error.type, message, request_id and route",
          doc.get("error", {}).get("type") == "PoolExhausted"
          and doc["error"]["message"] == "connection pool exhausted"
          and doc.get("request_id") == "crash-test-1"
          and doc.get("url", {}).get("route") == "/api/products"
          and doc["log"]["level"] == "error", doc)
    check("crash log holds the whole stack in ONE field",
          "Traceback" in doc["error"].get("stack_trace", "")
          and "exhausted_cursor" in doc["error"]["stack_trace"])

# --- the pool waits for a free connection, then times out as PoolTimeout ---
import threading  # noqa: E402
import time  # noqa: E402


class _Conn:
    def cursor(self, cursor_factory=None):
        return contextlib.nullcontext(object())
    def commit(self): pass
    def rollback(self): pass


class _OnePool:
    def getconn(self): return _Conn()
    def putconn(self, conn): pass


db._POOL, db._SLOTS = _OnePool(), threading.BoundedSemaphore(1)
db.DB_POOL_WAIT_SECONDS = 0.2
holding, release = threading.Event(), threading.Event()


def hold_connection():
    with real_db_cursor("hold"):
        holding.set()
        release.wait(2)


t = threading.Thread(target=hold_connection)
t.start()
holding.wait(2)
t0 = time.perf_counter()
try:
    with real_db_cursor("second"):
        pass
    timed_out = False
except db.PoolTimeout:
    timed_out = True
waited = time.perf_counter() - t0
check("pool full -> waits, then PoolTimeout", timed_out and waited >= 0.15,
      f"waited={waited:.2f}s")
release.set()
t.join()
with real_db_cursor("after-release"):
    pass
check("slot is returned after use", db._SLOTS.acquire(blocking=False))
db._SLOTS.release()
db._POOL = db._SLOTS = None

# --- PoolTimeout surfaces as a clean 503 with a structured log ---


@contextlib.contextmanager
def busy_cursor(operation, commit=False):
    raise db.PoolTimeout("no free database connection within 2.0s")
    yield


captured = io.StringIO()
capture = logging.StreamHandler(captured)
logging.getLogger("storefront-api").addHandler(capture)
with TestClient(app, raise_server_exceptions=False) as c:
    db.cursor = busy_cursor
    r = c.get("/api/products", headers={"x-request-id": "busy-test-1"})
    db.cursor = fake_cursor
logging.getLogger("storefront-api").removeHandler(capture)
check("pool timeout -> 503", r.status_code == 503, r.status_code)
busy = [json.loads(ln) for ln in captured.getvalue().splitlines() if '"database busy"' in ln]
check("pool timeout logs 'database busy' with error.type and request_id",
      len(busy) == 1 and busy[0]["error"]["type"] == "PoolTimeout"
      and busy[0]["request_id"] == "busy-test-1", busy)

finish()
