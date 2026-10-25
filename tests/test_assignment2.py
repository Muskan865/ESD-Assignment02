"""Assignment 2 checks: HTTP API practices, the OpenAPI contract, the Python
caller's reliability logic, and the gRPC OrderService.

Runs offline like the other suites: Postgres and payment-service are the
in-memory fakes from fake_shop.py. The gRPC part starts the REAL gRPC server
(app.grpc_server.build_server) on a free local port and calls it through the
generated stub and the real client in clients/grpc_order_client.py.

    pip install fastapi httpx prometheus-client requests grpcio protobuf pyyaml jsonschema
    python tests/test_assignment2.py
"""
import io
import json
import logging
import sys
import threading
import time

import fake_shop
from fake_shop import (
    CHARGES, ORDERS, PAYMENT_MODE, PRODUCTS, ROOT, check, finish, metric_value,
)

import grpc  # noqa: E402
import jsonschema  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from prometheus_client import REGISTRY, generate_latest  # noqa: E402

from app import clients, db, orders, routes  # noqa: E402
from app.application import app  # noqa: E402
from app.ratelimit import TokenBucketLimiter  # noqa: E402

sys.path.insert(0, str(ROOT / "clients"))
import grpc_order_client  # noqa: E402
import order_caller  # noqa: E402
from grpc_order_client import orders_pb2, orders_pb2_grpc  # noqa: E402

# Capture every JSON log line instead of printing it.
LOGS = io.StringIO()
_logger = logging.getLogger("storefront-api")
_logger.handlers = [logging.StreamHandler(LOGS)]


def logs(message):
    return [json.loads(line) for line in LOGS.getvalue().splitlines()
            if f'"message":"{message}"' in line]


def metrics():
    return generate_latest(REGISTRY).decode()


def sample(text, name, **labels):
    """Value of one series, whatever order the client library prints labels in."""
    for line in text.splitlines():
        if not line.startswith(name + "{"):
            continue
        series, value = line.rsplit(" ", 1)
        found = dict(part.split("=", 1) for part in series[len(name) + 1:-1].split(","))
        if {k: v.strip('"') for k, v in found.items()} == labels:
            return float(value)
    return None


# ------------------------------------------------------------- OpenAPI tools
SPEC = yaml.safe_load((ROOT / "services" / "storefront-api" / "openapi.yaml").read_text("utf-8"))
OBSERVED = set()  # (method, path, status) checked against the spec


def _resolve(node):
    while isinstance(node, dict) and "$ref" in node and node["$ref"].startswith("#/components/"):
        _, _, kind, name = node["$ref"].split("/")
        node = SPEC["components"][kind][name]
    return node


def conforms(method, path, response):
    """Is this real response documented in the spec, with a matching body?"""
    op = SPEC["paths"].get(path, {}).get(method.lower())
    if op is None:
        return False, f"{method} {path} not in spec"
    documented = op["responses"].get(str(response.status_code))
    if documented is None:
        return False, f"status {response.status_code} not documented for {method} {path}"
    documented = _resolve(documented)
    media = response.headers.get("content-type", "").split(";")[0]
    content = documented.get("content", {})
    if media not in content:
        return False, f"media type {media} not documented ({list(content)})"
    schema = {**content[media].get("schema", {}), "components": SPEC["components"]}
    try:
        jsonschema.Draft202012Validator(schema).validate(response.json())
    except jsonschema.ValidationError as exc:
        return False, f"body does not match schema: {exc.message}"
    OBSERVED.add((method, path, response.status_code))
    return True, ""


def check_conforms(label, method, path, response):
    ok, why = conforms(method, path, response)
    check(f"spec: {label}", ok, why)


def is_problem(r, status, code):
    body = r.json()
    return (r.status_code == status
            and r.headers.get("content-type", "").startswith("application/problem+json")
            and body.get("code") == code and body.get("status") == status
            and body.get("type") == f"urn:tiny-shop:problem:{code}"
            and isinstance(body.get("request_id"), str) and body["request_id"] != ""
            and r.headers.get("x-request-id") == body["request_id"])


def order_count():
    return len(ORDERS)


KB = [{"sku": "KB-01", "quantity": 1}]

# ======================================================================
print("Part A - HTTP API")
with TestClient(app) as c:
    # --- the spec is what the service serves, and matches its routes -----
    served = c.get("/openapi.json").json()
    check("GET /openapi.json serves the hand-written openapi.yaml", served == SPEC)
    check("GET /docs (Swagger UI) is available", c.get("/docs").status_code == 200)
    # The app's own routes plus the shop router's (newer FastAPI versions do not
    # flatten included routers into app.routes).
    app_routes = {(m, r.path) for r in [*app.router.routes, *routes.router.routes]
                  if getattr(r, "include_in_schema", False)
                  for m in getattr(r, "methods", ()) if m != "HEAD"}
    spec_routes = {(m.upper(), p) for p, ops in SPEC["paths"].items() for m in ops}
    check("every app route is in the spec", app_routes <= spec_routes,
          sorted(app_routes - spec_routes))
    check("every spec route exists in the app", spec_routes <= app_routes,
          sorted(spec_routes - app_routes))

    # --- valid request ------------------------------------------------------
    stock0 = PRODUCTS["KB-01"]["stock"]
    r = c.post("/api/orders", json={"items": [{"sku": "KB-01", "quantity": 2}],
                                    "customer": "ayesha"},
               headers={"x-request-id": "a2-valid-1"})
    body = r.json()
    check("valid order -> 201", r.status_code == 201, body)
    check("201 carries Location of the new order",
          r.headers.get("location") == f"/api/orders/{body.get('order_id')}")
    check("201 carries X-RateLimit-Limit/Remaining",
          r.headers.get("x-ratelimit-limit") == "40" and "x-ratelimit-remaining" in r.headers)
    check("request id chosen by the caller is used", body.get("request_id") == "a2-valid-1")
    check("payment-service receives the same request id", CHARGES[-1][1] == "a2-valid-1")
    check_conforms("POST /api/orders 201", "POST", "/api/orders", r)
    first_id = body["order_id"]

    r = c.get(r.headers["location"])
    check("GET Location -> 200 with the order", r.status_code == 200 and r.json()["id"] == first_id)
    check_conforms("GET /api/orders/{order_id} 200", "GET", "/api/orders/{order_id}", r)
    r = c.get("/api/orders?limit=5")
    check_conforms("GET /api/orders 200", "GET", "/api/orders", r)
    r = c.get("/api/products")
    check_conforms("GET /api/products 200", "GET", "/api/products", r)

    # --- invalid input: one error format everywhere -----------------------------
    r = c.post("/api/orders", json={"items": [{"sku": "KB-01", "quantity": 0}]})
    check("quantity 0 -> 422 problem+json invalid_request", is_problem(r, 422, "invalid_request"),
          r.text)
    check("validation problem names the field",
          any(e["field"] == "body.items.0.quantity" for e in r.json().get("errors", [])),
          r.json().get("errors"))
    check_conforms("POST /api/orders 422", "POST", "/api/orders", r)
    r = c.post("/api/orders", json={"items": []})
    check("empty basket -> 422", is_problem(r, 422, "invalid_request"))
    r = c.post("/api/orders", json={"items": KB, "payment_method": "bitcoin"})
    check("unknown payment method -> 422", is_problem(r, 422, "invalid_request"))
    r = c.post("/api/orders", content=b"not json", headers={"content-type": "application/json"})
    check("malformed JSON -> 422", is_problem(r, 422, "invalid_request"), r.status_code)
    r = c.post("/api/orders", json={"items": KB}, headers={"Idempotency-Key": "short"})
    check("malformed Idempotency-Key -> 422", is_problem(r, 422, "invalid_request"))
    r = c.post("/api/orders", json={"items": [{"sku": "NOPE-99", "quantity": 1}]})
    check("unknown sku -> 422 unknown_sku", is_problem(r, 422, "unknown_sku"), r.text)
    r = c.post("/api/orders", json={"items": [{"sku": "OUT-01", "quantity": 1}]})
    check("out of stock -> 409 out_of_stock", is_problem(r, 409, "out_of_stock"))
    check_conforms("POST /api/orders 409", "POST", "/api/orders", r)
    r = c.get("/api/orders?limit=0")
    check("limit=0 -> 422 (A1 silently clamped)", is_problem(r, 422, "invalid_request"))
    check_conforms("GET /api/orders 422", "GET", "/api/orders", r)
    r = c.get("/api/orders/0")
    check("order id 0 -> 422", is_problem(r, 422, "invalid_request"))
    r = c.get("/api/orders/99999999999")
    check("order id beyond int4 -> 422, not a database 500", is_problem(r, 422, "invalid_request"))
    r = c.get("/api/orders/424242")
    check("missing order -> 404 order_not_found", is_problem(r, 404, "order_not_found"))
    check_conforms("GET /api/orders/{order_id} 404", "GET", "/api/orders/{order_id}", r)
    r = c.get("/api/nothing-here")
    check("unknown route -> 404 problem+json", is_problem(r, 404, "not_found"), r.text)
    r = c.delete("/api/products")
    check("wrong method -> 405 problem+json", is_problem(r, 405, "method_not_allowed"), r.text)

    for outcome, status, code in [(clients.PaymentOutcome.DECLINED, 402, "payment_declined"),
                                  (clients.PaymentOutcome.ERROR, 502, "payment_error"),
                                  (clients.PaymentOutcome.TIMEOUT, 504, "payment_timeout")]:
        PAYMENT_MODE["outcome"] = outcome
        n = order_count()
        r = c.post("/api/orders", json={"items": KB})
        check(f"payment {outcome} -> {status} {code}", is_problem(r, status, code), r.text)
        check(f"payment {outcome} leaves no order behind", order_count() == n)
        check_conforms(f"POST /api/orders {status}", "POST", "/api/orders", r)
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.APPROVED

    # --- repeated request: idempotency ------------------------------------
    replays0 = metric_value(metrics(), 'shop_idempotent_replays_total{interface="http"}') or 0
    key = "a2-repeat-key-0001"
    charges0, orders0 = len(CHARGES), order_count()
    stock_before = PRODUCTS["KB-01"]["stock"]
    r1 = c.post("/api/orders", json={"items": KB}, headers={"Idempotency-Key": key})
    r2 = c.post("/api/orders", json={"items": KB}, headers={"Idempotency-Key": key})
    check("first call with a key -> 201, replayed=false",
          r1.status_code == 201 and r1.json()["replayed"] is False)
    check("repeat with the same key -> 201, same order, replayed=true",
          r2.status_code == 201 and r2.json()["order_id"] == r1.json()["order_id"]
          and r2.json()["replayed"] is True, r2.json())
    check("repeat carries Idempotent-Replayed: true",
          r2.headers.get("idempotent-replayed") == "true")
    check("repeat placed no second order", order_count() == orders0 + 1)
    check("repeat charged no second payment", len(CHARGES) == charges0 + 1)
    check("repeat took stock only once", PRODUCTS["KB-01"]["stock"] == stock_before - 1)
    check("shop_idempotent_replays_total{interface=http} +1",
          metric_value(metrics(), 'shop_idempotent_replays_total{interface="http"}')
          == replays0 + 1)
    check("replay recorded as outcome=replayed",
          (metric_value(metrics(), 'shop_checkout_duration_seconds_count{outcome="replayed"}')
           or 0) >= 1)
    check_conforms("POST /api/orders 201 (replay)", "POST", "/api/orders", r2)

    r = c.post("/api/orders", json={"items": [{"sku": "MS-01", "quantity": 1}]},
               headers={"Idempotency-Key": key})
    check("same key, different body -> 422 idempotency_key_reused",
          is_problem(r, 422, "idempotency_key_reused"), r.text)

    # a failed attempt stores nothing, so its retry places the order
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.ERROR
    retry_key = "a2-retry-after-failure"
    r1 = c.post("/api/orders", json={"items": KB}, headers={"Idempotency-Key": retry_key})
    PAYMENT_MODE["outcome"] = clients.PaymentOutcome.APPROVED
    r2 = c.post("/api/orders", json={"items": KB}, headers={"Idempotency-Key": retry_key})
    check("failed attempt (502) then retry with same key -> 201 new order",
          r1.status_code == 502 and r2.status_code == 201 and r2.json()["replayed"] is False)

    # the concurrent-duplicate race: the key is committed by another request
    # after our existence check but before our INSERT
    race_key = "a2-race-key-0001"
    winner = c.post("/api/orders", json={"items": KB}, headers={"Idempotency-Key": race_key})
    real_find = orders._find_by_key
    calls = []
    orders._find_by_key = lambda k: calls.append(k) or (None if len(calls) == 1 else real_find(k))
    charges0 = len(CHARGES)
    loser = c.post("/api/orders", json={"items": KB}, headers={"Idempotency-Key": race_key})
    orders._find_by_key = real_find
    check("lost race (unique violation) -> replay of the winner, no second charge",
          loser.status_code == 201 and loser.json()["order_id"] == winner.json()["order_id"]
          and loser.json()["replayed"] is True and len(CHARGES) == charges0, loser.text)

    # cancel and fulfil are idempotent too
    cancelled0 = metric_value(metrics(),
                              'shop_orders_cancelled_total{reason="customer_request"}') or 0
    r1 = c.post(f"/api/orders/{first_id}/cancel")
    r2 = c.post(f"/api/orders/{first_id}/cancel")
    check("cancel twice -> 200 both times",
          r1.status_code == 200 and r2.status_code == 200 and r2.json()["status"] == "CANCELLED")
    check("second cancel had no effect (counter +1 only, stock restored once)",
          metric_value(metrics(), 'shop_orders_cancelled_total{reason="customer_request"}')
          == cancelled0 + 1)
    check_conforms("POST cancel 200", "POST", "/api/orders/{order_id}/cancel", r2)
    r = c.post(f"/api/orders/{first_id}/fulfil")
    check("fulfil a cancelled order -> 409 order_not_fulfillable",
          is_problem(r, 409, "order_not_fulfillable"))
    check_conforms("POST fulfil 409", "POST", "/api/orders/{order_id}/fulfil", r)
    oid = c.post("/api/orders", json={"items": KB}).json()["order_id"]
    r1, r2 = c.post(f"/api/orders/{oid}/fulfil"), c.post(f"/api/orders/{oid}/fulfil")
    check("fulfil twice -> 200 both times", r1.status_code == r2.status_code == 200)
    r = c.post(f"/api/orders/{oid}/cancel")
    check("cancel a fulfilled order -> 409 order_not_cancellable",
          is_problem(r, 409, "order_not_cancellable"))
    check_conforms("POST cancel 409", "POST", "/api/orders/{order_id}/cancel", r)

    # --- rate limiting ---------------------------------------------------------
    now = [1000.0]
    routes.ORDER_LIMITER = TokenBucketLimiter(rate=1.0, burst=3, clock=lambda: now[0])
    statuses = [c.post("/api/orders", json={"items": KB}).status_code for _ in range(4)]
    check("burst of 3 accepted, 4th refused", statuses == [201, 201, 201, 429], statuses)
    r = c.post("/api/orders", json={"items": KB})
    check("429 is problem+json rate_limited", is_problem(r, 429, "rate_limited"), r.text)
    check("429 carries Retry-After: 1 and X-RateLimit-Remaining: 0",
          r.headers.get("retry-after") == "1" and r.headers.get("x-ratelimit-remaining") == "0",
          dict(r.headers))
    check_conforms("POST /api/orders 429", "POST", "/api/orders", r)
    r = c.post("/api/orders", json={"items": [{"sku": "KB-01", "quantity": 0}]})
    check("an invalid request is limited too (limit runs before validation)",
          r.status_code == 429)
    now[0] += 1.0
    r = c.post("/api/orders", json={"items": KB})
    check("after Retry-After seconds a call is allowed again", r.status_code == 201)
    check("GET /api/orders is not rate limited",
          all(c.get("/api/orders").status_code == 200 for _ in range(5)))
    m = metrics()
    check('429s visible as http_requests_total{route="/api/orders",status="429"}',
          (sample(m, "http_requests_total", method="POST", route="/api/orders", status="429")
           or 0) >= 2)
    routes.ORDER_LIMITER = TokenBucketLimiter(1000, 1000)

# --- limiter unit behaviour ---------------------------------------------------
t = [0.0]
lim = TokenBucketLimiter(rate=2.0, burst=2, clock=lambda: t[0])
check("limiter: separate buckets per client",
      [lim.take("a").allowed, lim.take("a").allowed, lim.take("a").allowed,
       lim.take("b").allowed] == [True, True, False, True])
t[0] += 0.25
d = lim.take("a")
check("limiter: half a token after 0.25 s at 2/s -> refused, Retry-After 1",
      not d.allowed and d.retry_after == 1)
t[0] += 0.25
check("limiter: one token after 0.5 s -> allowed", lim.take("a").allowed)
for i in range(TokenBucketLimiter._PRUNE_EVERY):
    lim.take(f"client-{i}")
t[0] += 10
for i in range(TokenBucketLimiter._PRUNE_EVERY):
    lim.take("late")
check("limiter: idle full buckets are pruned (bounded memory)", len(lim._buckets) < 10,
      len(lim._buckets))

# ======================================================================
print("\nPart A - Python caller (timeout, retry, circuit breaker)")


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self): return self._body


class ScriptedSession:
    """Plays back responses/exceptions; records the headers of every attempt."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def post(self, url, json, headers, timeout):
        self.sent.append(dict(headers))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


OK = FakeResponse(201, {"order_id": 7, "status": "PLACED", "total_dollars": 9.0})
BAD_GATEWAY = FakeResponse(502, {"code": "payment_error"})
clock = [0.0]
quiet = lambda *a: None  # noqa: E731


def make_caller(script, threshold=4, recovery=5.0):
    breaker = order_caller.CircuitBreaker(threshold, recovery, clock=lambda: clock[0], log=quiet)
    session = ScriptedSession(script)
    caller = order_caller.OrderCaller(breaker=breaker, session=session, log=quiet,
                                      sleep=lambda s: None)
    return caller, session


caller, session = make_caller([BAD_GATEWAY, OK])
result = caller.place_order(KB)
check("502 then 201 -> placed after 2 attempts", result.outcome == "placed" and result.attempts == 2)
check("every attempt reuses ONE Idempotency-Key and x-request-id",
      len({h["Idempotency-Key"] for h in session.sent}) == 1
      and len({h["x-request-id"] for h in session.sent}) == 1)
caller, session = make_caller([order_caller.requests.Timeout(), OK])
check("timeout then 201 -> placed after 2 attempts", caller.place_order(KB).attempts == 2)
caller, session = make_caller([FakeResponse(409, {"code": "out_of_stock"})])
result = caller.place_order(KB)
check("409 is a business outcome: not retried",
      result.outcome == "rejected" and len(session.sent) == 1)
caller, session = make_caller([FakeResponse(429, {"code": "rate_limited"},
                                            {"Retry-After": "1"})])
result = caller.place_order(KB)
check("429 not retried and not counted by the breaker",
      result.outcome == "rate_limited" and caller.breaker.consecutive_failures == 0)
check("backoff doubles from 0.2 s and is capped",
      0.2 <= caller.backoff(1) < 0.26 and 0.4 <= caller.backoff(2) < 0.46
      and caller.backoff(10) < 2.06)

caller, session = make_caller([BAD_GATEWAY] * 4 + [OK, OK])
r1 = caller.place_order(KB)
check("3 failed attempts -> call fails, breaker still closed",
      r1.outcome == "failed" and caller.breaker.state == "CLOSED")
r2 = caller.place_order(KB)
check("4th consecutive failure opens the breaker; that call stops retrying at once",
      r2.outcome == "failed" and r2.detail == "breaker opened" and r2.attempts == 1
      and caller.breaker.state == "OPEN" and len(session.sent) == 4)
r3 = caller.place_order(KB)
check("while open, calls are blocked without a request", r3.outcome == "blocked"
      and len(session.sent) == 4)
clock[0] += 5.0
r4 = caller.place_order(KB)
check("after the recovery period one trial goes through and closes the breaker",
      r4.outcome == "placed" and caller.breaker.state == "CLOSED" and len(session.sent) == 5)
caller, session = make_caller([BAD_GATEWAY] * 4 + [BAD_GATEWAY], threshold=4)
caller.place_order(KB); caller.place_order(KB)
clock[0] += 5.0
caller.place_order(KB)
check("a failed trial re-opens the breaker", caller.breaker.state == "OPEN")

# the caller against the real app: retry of a 502 places exactly one order
with TestClient(app) as c:
    class AppSession:
        def post(self, url, json, headers, timeout):
            return c.post(url.replace("http://localhost:8000", ""), json=json, headers=headers)

    flaky = {"left": 1}
    real_charge = clients.charge

    def charge_failing_once(**kw):
        if flaky["left"]:
            flaky["left"] -= 1
            return clients.PaymentOutcome.ERROR, {"status_code": 503, "duration_ms": 1.0}
        return real_charge(**kw)

    clients.charge = charge_failing_once
    caller = order_caller.OrderCaller(session=AppSession(), log=quiet, sleep=lambda s: None,
                                      breaker=order_caller.CircuitBreaker(log=quiet))
    n = order_count()
    result = caller.place_order(KB)
    clients.charge = real_charge
    check("caller vs real app: 502 retried -> 201, exactly one order",
          result.outcome == "placed" and result.attempts == 2 and order_count() == n + 1)

# ======================================================================
print("\nPart B - gRPC OrderService (real server, in process)")
from app import grpc_server  # noqa: E402

server, port = grpc_server.build_server(port=0, max_workers=4)
server.start()
channel = grpc.insecure_channel(f"localhost:{port}")
stub = orders_pb2_grpc.OrderServiceStub(channel)


def rpc(items, **kw):
    return grpc_order_client.place_order(stub, items, **kw)


res = rpc([{"sku": "KB-01", "quantity": 2}], customer="bilal", request_id="grpc-ok-1")
check("PlaceOrder valid -> OK", res.ok, grpc_order_client.describe(res))
check("response: PLACED, total in integer cents, item count",
      res.ok and res.response.status == "PLACED" and res.response.total_cents == 9800
      and res.response.item_count == 2 and not res.response.replayed)
check("x-request-id echoed in initial metadata", res.request_id == "grpc-ok-1")
check("x-request-id forwarded to payment-service", CHARGES[-1][1] == "grpc-ok-1")
grpc_order_id = res.response.order_id if res.ok else None
check("order is in the SAME database the HTTP API reads", grpc_order_id in ORDERS)

cases = [
    ("unknown sku -> NOT_FOUND", [{"sku": "NOPE-99", "quantity": 1}], {},
     grpc.StatusCode.NOT_FOUND, "unknown_sku"),
    ("quantity 0 -> INVALID_ARGUMENT", [{"sku": "KB-01", "quantity": 0}], {},
     grpc.StatusCode.INVALID_ARGUMENT, "invalid_request"),
    ("no items -> INVALID_ARGUMENT", [], {}, grpc.StatusCode.INVALID_ARGUMENT, "invalid_request"),
    ("bad idempotency key -> INVALID_ARGUMENT", KB, {"idempotency_key": "x"},
     grpc.StatusCode.INVALID_ARGUMENT, "invalid_request"),
    ("out of stock -> FAILED_PRECONDITION", [{"sku": "OUT-01", "quantity": 1}], {},
     grpc.StatusCode.FAILED_PRECONDITION, "out_of_stock"),
]
for label, items, kw, code, error_code in cases:
    res = rpc(items, **kw)
    check(label, res.code == code and res.error_code == error_code, grpc_order_client.describe(res))
res = rpc([{"sku": "KB-01", "quantity": 0}])
check("validation detail names the field (same rules as HTTP)",
      "items.0.quantity" in res.details, res.details)
bad_enum = stub.PlaceOrder
try:
    stub.PlaceOrder(orders_pb2.PlaceOrderRequest(
        items=[orders_pb2.CartLine(sku="KB-01", quantity=1)], payment_method=99), timeout=2)
    enum_code = grpc.StatusCode.OK
except grpc.RpcError as exc:
    enum_code = exc.code()
check("unknown payment enum value -> INVALID_ARGUMENT", enum_code == grpc.StatusCode.INVALID_ARGUMENT)

for outcome, code, error_code in [
        (clients.PaymentOutcome.DECLINED, grpc.StatusCode.FAILED_PRECONDITION, "payment_declined"),
        (clients.PaymentOutcome.ERROR, grpc.StatusCode.UNAVAILABLE, "payment_error"),
        (clients.PaymentOutcome.TIMEOUT, grpc.StatusCode.DEADLINE_EXCEEDED, "payment_timeout")]:
    PAYMENT_MODE["outcome"] = outcome
    n = order_count()
    res = rpc(KB)
    check(f"payment {outcome} -> {code.name} ({error_code})",
          res.code == code and res.error_code == error_code, grpc_order_client.describe(res))
    check(f"payment {outcome} over gRPC leaves no order behind", order_count() == n)
PAYMENT_MODE["outcome"] = clients.PaymentOutcome.APPROVED

# same idempotency key space as HTTP: one source of truth
with TestClient(app) as c:
    shared_key = "shared-key-http-grpc"
    r = c.post("/api/orders", json={"items": KB, "customer": "guest"},
               headers={"Idempotency-Key": shared_key})
    charges0 = len(CHARGES)
    res = rpc(KB, idempotency_key=shared_key)
    check("key used over HTTP, repeated over gRPC -> same order, replayed, no charge",
          res.ok and res.response.replayed and res.response.order_id == r.json()["order_id"]
          and len(CHARGES) == charges0, grpc_order_client.describe(res))
    res = rpc([{"sku": "MS-01", "quantity": 1}], idempotency_key=shared_key)
    check("same key, different order over gRPC -> INVALID_ARGUMENT idempotency_key_reused",
          res.code == grpc.StatusCode.INVALID_ARGUMENT
          and res.error_code == "idempotency_key_reused")
    r = c.get(f"/api/orders/{grpc_order_id}")
    check("order placed over gRPC is visible over HTTP", r.status_code == 200
          and r.json()["status"] == "PLACED")
    m = c.get("/metrics").text
    placed_open = sum(1 for o in ORDERS.values() if o["status"] == "PLACED")
    check("storefront's awaiting-fulfilment gauge counts gRPC orders (read from DB at scrape)",
          metric_value(m, "shop_orders_awaiting_fulfilment") == placed_open)

# client deadline
slow = threading.Event()
real_charge = clients.charge


def slow_charge(**kw):
    time.sleep(0.5)
    return real_charge(**kw)


clients.charge = slow_charge
res = rpc(KB, deadline=0.1)
clients.charge = real_charge
check("client deadline 0.1 s on a 0.5 s call -> DEADLINE_EXCEEDED at the client",
      res.code == grpc.StatusCode.DEADLINE_EXCEEDED and res.elapsed_ms < 400, res.elapsed_ms)
time.sleep(0.6)  # let the abandoned server-side call finish


# database busy and an unexpected crash
@fake_shop.contextlib.contextmanager
def busy_cursor(operation, commit=False):
    raise db.PoolTimeout("no free database connection within 2.0s")
    yield


db.cursor = busy_cursor
res = rpc(KB)
check("pool timeout -> UNAVAILABLE database_busy",
      res.code == grpc.StatusCode.UNAVAILABLE and res.error_code == "database_busy")


@fake_shop.contextlib.contextmanager
def broken_cursor(operation, commit=False):
    raise RuntimeError("disk on fire")
    yield


db.cursor = broken_cursor
res = rpc(KB, request_id="grpc-crash-1")
db.cursor = fake_shop.fake_cursor
check("unexpected exception -> INTERNAL (not UNKNOWN)", res.code == grpc.StatusCode.INTERNAL)
crash = [d for d in logs("unhandled exception") if d.get("request_id") == "grpc-crash-1"]
check("crash logged once as structured JSON with stack and request id",
      len(crash) == 1 and crash[0]["error"]["type"] == "RuntimeError"
      and "Traceback" in crash[0]["error"]["stack_trace"])

# logs and metrics
rpc_logs = [d for d in logs("rpc completed") if d.get("request_id") == "grpc-ok-1"]
check("one 'rpc completed' log per call, with request id, method and status",
      len(rpc_logs) == 1 and rpc_logs[0]["rpc"]["method"] == "PlaceOrder"
      and rpc_logs[0]["rpc"]["grpc"]["status_code"] == "OK"
      and "duration_ms" in rpc_logs[0]["event"], rpc_logs)
placed = [d for d in logs("order placed") if d.get("request_id") == "grpc-ok-1"]
check("business log 'order placed' carries the gRPC request id and interface=grpc",
      len(placed) == 1 and placed[0]["interface"] == "grpc")
m = metrics()
svc = {"grpc_service": "tinyshop.orders.v1.OrderService", "grpc_method": "PlaceOrder"}
for code, at_least in [("OK", 2), ("NOT_FOUND", 1), ("INVALID_ARGUMENT", 5),
                       ("FAILED_PRECONDITION", 2), ("UNAVAILABLE", 2),
                       ("DEADLINE_EXCEEDED", 1), ("INTERNAL", 1)]:
    v = sample(m, "grpc_server_handled_total", **svc, grpc_code=code) or 0
    check(f"grpc_server_handled_total{{grpc_code={code}}} >= {at_least}", v >= at_least, v)
total = sum(float(line.rsplit(" ", 1)[1]) for line in m.splitlines()
            if line.startswith("grpc_server_handled_total{"))
check("grpc_server_handling_seconds counts every call",
      sample(m, "grpc_server_handling_seconds_count", **svc) == total)
check("grpc_server_in_flight back to 0", sample(m, "grpc_server_in_flight", **svc) == 0)
check("shop_idempotent_replays_total{interface=grpc} recorded",
      (sample(m, "shop_idempotent_replays_total", interface="grpc") or 0) >= 1)
check("no request/trace/user ids in any gRPC metric label",
      not any(s in m for s in ("grpc-ok-1", "bilal", "shared-key")))
try:
    channel.unary_unary("/tinyshop.orders.v1.OrderService/Nope")(b"", timeout=2)
    code = grpc.StatusCode.OK
except grpc.RpcError as exc:
    code = exc.code()
check("unknown method -> UNIMPLEMENTED and not recorded as a label",
      code == grpc.StatusCode.UNIMPLEMENTED and 'grpc_method="Nope"' not in metrics())

check("db_pool_connections_in_use is exported",
      "db_pool_connections_in_use" in metrics())

channel.close()
server.stop(grace=1).wait()

# the gRPC process exposes only what it writes
grpc_server._expose_only_owned_metrics()
m = metrics()
check("gRPC process drops the HTTP-only and DB-gauge metrics from /metrics",
      "http_requests_total" not in m and "shop_orders_awaiting_fulfilment" not in m
      and "grpc_server_handled_total" in m and "shop_orders_placed_total" in m)

# ======================================================================
print("\nOpenAPI coverage")
documented = {(m.upper(), p, int(s)) for p, ops in SPEC["paths"].items()
              for m, op in ops.items() for s in op["responses"]
              if p.startswith("/api/")}
missing = sorted(documented - OBSERVED)
print(f"  {len(OBSERVED)} real responses validated against the spec; "
      f"not exercised here: {missing}")
check("every documented POST /api/orders status except 503 was exercised",
      {s for m, p, s in documented - OBSERVED if p == "/api/orders" and m == "POST"} <= {503})

finish()
