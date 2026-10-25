"""Smoke-test payment-service in-process with FastAPI's TestClient."""
import pathlib
import sys, json, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "services" / "payment-service"))

from fastapi.testclient import TestClient
from app.application import app

c = TestClient(app)
fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


# --- health / metrics ---
r = c.get("/health")
check("GET /health -> 200", r.status_code == 200, r.json())

r = c.get("/metrics")
check("GET /metrics -> 200", r.status_code == 200)
check("/metrics exposes http_requests_total", "http_requests_total" in r.text)
check("/metrics exposes payment_charges_total", "payment_charges_total" in r.text)

# --- a normal charge ---
approved = declined = 0
for i in range(60):
    r = c.post("/payment/charge", json={"order_id": i, "amount_cents": 4900, "method": "card"})
    if r.status_code == 200:
        approved += 1
    elif r.status_code == 402:
        declined += 1
check("charges mostly approved (~3% decline by design)", approved > 45,
      f"approved={approved} declined={declined}")

# --- request id echoed back ---
r = c.post("/payment/charge", json={"order_id": 1, "amount_cents": 100},
           headers={"x-request-id": "abc123def456"})
check("x-request-id echoed on response", r.headers.get("x-request-id") == "abc123def456",
      r.headers.get("x-request-id"))

# --- route template, not raw path, in metrics ---
m = c.get("/metrics").text
check('metric uses route template "/payment/charge"',
      'route="/payment/charge"' in m)

# --- latency fault ---
r = c.post("/chaos/latency", json={"probability": 1.0, "seconds": 0.4})
check("POST /chaos/latency -> 200", r.status_code == 200, r.json())
t0 = time.perf_counter()
c.post("/payment/charge", json={"order_id": 2, "amount_cents": 100})
elapsed = time.perf_counter() - t0
check("latency fault actually delays (>=0.4s)", elapsed >= 0.4, f"{elapsed:.3f}s")

# --- failure fault ---
c.post("/chaos/reset")
r = c.post("/chaos/failure", json={"rate": 1.0})
check("POST /chaos/failure -> 200", r.status_code == 200)
r = c.post("/payment/charge", json={"order_id": 3, "amount_cents": 100})
check("failure fault returns 503", r.status_code == 503, r.status_code)

# --- fail_next: exactly N failures, then normal service (A2 caller demo) ---
c.post("/chaos/reset")
r = c.post("/chaos/failure", json={"rate": 0.0, "fail_next": 2})
check("POST /chaos/failure with fail_next -> 200", r.json().get("fail_next") == 2, r.json())
codes = [c.post("/payment/charge", json={"order_id": 10 + i, "amount_cents": 100}).status_code
         for i in range(3)]
check("fail_next=2 fails exactly the next two charges",
      codes[:2] == [503, 503] and codes[2] in (200, 402), codes)
check("fail_next counts down to 0", c.get("/chaos/status").json()["fail_next"] == 0)

# --- reset clears everything ---
r = c.post("/chaos/reset")
state = r.json()
check("reset clears all faults",
      state["latency_probability"] == 0.0 and state["failure_rate"] == 0.0, state)
r = c.post("/payment/charge", json={"order_id": 4, "amount_cents": 100})
check("charges work again after reset", r.status_code in (200, 402), r.status_code)

# --- validation ---
r = c.post("/payment/charge", json={"order_id": 5, "amount_cents": -5})
check("negative amount rejected (422)", r.status_code == 422, r.status_code)
r = c.post("/payment/charge", json={"order_id": 5, "amount_cents": 100, "method": "bitcoin"})
check("unknown payment method rejected (422)", r.status_code == 422, r.status_code)

# --- 404s collapse to a single bounded label ---
c.get("/definitely/not/a/route/12345")
c.get("/definitely/not/a/route/67890")
m = c.get("/metrics").text
check('unmatched routes collapse to route="unmatched"', 'route="unmatched"' in m)
check("no raw 404 path leaked into metrics", "definitely/not/a/route" not in m)

# --- summary/histogram/gauge presence ---
for name in ["http_request_duration_seconds_bucket", "http_requests_in_flight",
             "payment_charge_amount_dollars_bucket"]:
    check(f"metric present: {name}", name in m)

# --- a crash is ONE structured JSON log with the stack, and a clean 500 ---
import io, logging  # noqa: E402
import app.routes as routes  # noqa: E402

captured = io.StringIO()
capture = logging.StreamHandler(captured)
logging.getLogger("payment-service").addHandler(capture)
real_snapshot = routes.snapshot
def broken_snapshot():
    raise RuntimeError("chaos state unreadable")
routes.snapshot = broken_snapshot
r = c.post("/payment/charge", json={"order_id": 1, "amount_cents": 100, "method": "card"},
           headers={"x-request-id": "pay-crash-1"})
routes.snapshot = real_snapshot
logging.getLogger("payment-service").removeHandler(capture)
check("crash -> 500 with x-request-id", r.status_code == 500
      and r.headers.get("x-request-id") == "pay-crash-1", r.status_code)
crash = [json.loads(ln) for ln in captured.getvalue().splitlines() if '"unhandled exception"' in ln]
check("crash logged once with type, message and full stack",
      len(crash) == 1 and crash[0]["error"]["type"] == "RuntimeError"
      and crash[0]["error"]["message"] == "chaos state unreadable"
      and "broken_snapshot" in crash[0]["error"]["stack_trace"], crash)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
