# Offline tests

Three suites that run the services **in-process**, with no Docker and no
database, using FastAPI's `TestClient` (and, for gRPC, a real gRPC server on a
free local port). They exist to check the application
logic quickly — the real end-to-end verification is `shopctl verify` plus the
two experiments in `README.md`.

```bash
pip install fastapi httpx prometheus-client requests grpcio protobuf pyyaml jsonschema
python tests/test_payment_service.py     # 25 checks
python tests/test_storefront_api.py      # 58 checks
python tests/test_assignment2.py         # 128 checks (Assignment 2)
```

`fake_shop.py` is shared by the storefront and Assignment 2 suites: it stubs
`psycopg2`, provides an in-memory Postgres that understands exactly the SQL the
app issues (with a per-transaction undo journal, so a rolled-back checkout
really leaves nothing behind), and stubs the payment client.

Each prints one `PASS`/`FAIL` line per check and exits non-zero on failure.
(These are the only things in this project that need `pip install`; the
`shopctl` control script is standard-library only.)

### What they cover

**`test_payment_service.py`** — health and `/metrics`; the approve/decline
split; `x-request-id` propagation; both fault injectors actually taking effect;
`/chaos/reset` restoring normal service; input validation; and that unmatched
URLs collapse to `route="unmatched"` instead of leaking raw paths into metric
labels; and that a crash is answered with a clean 500 and logged as one JSON
line carrying `error.type`, `error.message` and the whole `error.stack_trace`.

**`test_storefront_api.py`** — stubs Postgres with an in-memory fake and stubs
the payment client, then checks the whole checkout control flow: the happy path
and each failure path (out of stock, unknown SKU, declined, provider error,
timeout) with the right status code *and* the right
`shop_orders_rejected_total{reason=...}`; that every metric type is recorded
(Counter, Gauge, Histogram, Summary); that the "awaiting fulfilment" gauge goes
up on placement and back down on both cancel and fulfil; that route templates
rather than order ids reach metric labels; and the Part E.2 cardinality
behaviour — the cap holds at 100 **distinct** ids even across toggling, the
bounded counter stays at 2 series, `reset` clears the label children so the
experiment can be re-run, and the same counter built with the label removed
(`CARDINALITY_DEMO_LABEL=none`) gives exactly one series for 100 requests.

It also covers the fixes that came out of Part E.1: every statement that locks
product rows (checkout, cancel, restock) locks them `ORDER BY sku`; a full
connection pool makes a request wait and then raise `PoolTimeout` rather than
fail instantly, and releases its slot afterwards; `PoolTimeout` becomes a 503
with a `database busy` log; and an unhandled exception becomes a 500 with one
structured `unhandled exception` log that keeps the request id.

**`test_assignment2.py`** — Part A: a valid order (201, `Location`,
rate-limit headers, request id forwarded to payment); invalid input of every
kind answered as `application/problem+json` with a stable `code`; the
idempotent repeat (same order, `Idempotent-Replayed`, one charge, stock taken
once), key reuse with a different body, retry after a failed attempt, and the
concurrent-duplicate race (unique violation turned into a replay); idempotent
cancel and fulfil; the rate limit (burst, `429` + `Retry-After`, recovery)
and the token bucket itself. The OpenAPI document is checked against the app:
the served spec is the file, every route is in it and vice versa, and every
real response the suite sees is validated against the documented schema with
`jsonschema`. The Python caller's retry/backoff/breaker logic is tested with a
scripted session and once against the real app. Part B: the real gRPC server
through the generated stub and `clients/grpc_order_client.py` - success, every
status-code mapping, the shared idempotency key space with HTTP, client
deadlines, database busy, a crash becoming `INTERNAL` with one structured log,
request-id echo and propagation, `rpc completed` logs, and the
`grpc_server_*` metrics (bounded labels, unknown methods not recorded).

### What they do not cover

The SQL itself is stubbed, so these tests cannot catch a syntax error or a bad
query plan - nor real row locking or unique-index waits, which the idempotency
race relies on (the race test simulates the unique violation instead). Prometheus scraping, Filebeat parsing, Elasticsearch indexing and
the Grafana dashboard are all out of scope here — those are verified by running
the stack.
