"""Every Prometheus instrument the storefront owns.

Grouped the way Part B asks for them: application metrics describe the service,
business metrics describe the shop. All four Prometheus types appear across the
two groups (Counter, Gauge, Histogram, Summary).

Label discipline: every label below has a small, bounded set of values
(route templates, HTTP status, a fixed reason enum, five SKUs). Nothing
unbounded — no order id, no customer, no request id. See `DEMO_REQUESTS` at the
bottom for the deliberate counter-example used in the Part E experiment.
"""

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, Summary

from .config import CARDINALITY_DEMO_LABEL, LATENCY_BUCKETS, ORDER_VALUE_BUCKETS

# ============================================================== APPLICATION ==

# COUNTER — how much traffic, and how much of it failed.
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests handled, by route and response status.",
    ["method", "route", "status"],
)

# HISTOGRAM — request latency distribution; source of p95/p99.
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Wall-clock duration of inbound HTTP requests, in seconds.",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)

# GAUGE — concurrency right now; goes up on arrival, down on completion.
IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "Requests currently being processed.",
)

# SUMMARY — average duration of the outbound call to payment-service.
# prometheus_client's Summary exposes _sum and _count only (no quantiles), so
# this answers "what is the average?" and the histogram below answers "what is
# p95?". That split is deliberate and is explained in the report.
PAYMENT_CALL_SUMMARY = Summary(
    "payment_call_duration_seconds",
    "Duration of the outbound HTTP call to payment-service, in seconds.",
    ["outcome"],
)

# HISTOGRAM — same outbound call, bucketed, so p95/p99 are computable.
PAYMENT_CALL_LATENCY = Histogram(
    "payment_call_duration_seconds_hist",
    "Duration of the outbound call to payment-service, bucketed for percentiles.",
    ["outcome"],
    buckets=LATENCY_BUCKETS,
)

# COUNTER — outbound dependency health, independent of our own status codes.
PAYMENT_CALLS = Counter(
    "payment_calls_total",
    "Outbound calls to payment-service, by outcome.",
    ["outcome"],  # approved | declined | error | timeout
)

# HISTOGRAM — how long the database is taking, split by logical operation.
DB_QUERY_LATENCY = Histogram(
    "db_query_duration_seconds",
    "Duration of database operations, in seconds.",
    ["operation"],  # bounded: list_products | create_order | cancel_order | count_open
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# ================================================================= BUSINESS ==

# COUNTER — the headline business number. 20 -> 21 when an order is placed.
ORDERS_PLACED = Counter(
    "shop_orders_placed_total",
    "Orders successfully placed and paid for.",
    ["payment_method"],  # bounded: card | wallet | cod
)

# COUNTER — the other side of the funnel.
ORDERS_CANCELLED = Counter(
    "shop_orders_cancelled_total",
    "Orders cancelled after being placed.",
    ["reason"],  # bounded: customer_request
)

# COUNTER — checkouts that never became orders, and why they died.
ORDERS_REJECTED = Counter(
    "shop_orders_rejected_total",
    "Checkout attempts that did not become an order.",
    # bounded: unknown_sku | out_of_stock | payment_declined | payment_error |
    #          payment_timeout | idempotency_key_reused
    ["reason"],
)

# GAUGE — orders sitting in PLACED, waiting to be shipped. Up on placement,
# down on fulfilment or cancellation. Re-synced from the database on startup so
# a container restart does not lose the true value.
ORDERS_AWAITING_FULFILMENT = Gauge(
    "shop_orders_awaiting_fulfilment",
    "Orders in PLACED state, waiting to be fulfilled.",
)

# GAUGE — stock on hand per product. Five SKUs, so the label is safely bounded.
STOCK_UNITS = Gauge(
    "shop_stock_units",
    "Units currently in stock, per product.",
    ["sku"],
)

# COUNTER — money. Monotonic, so rate() gives revenue per second.
REVENUE = Counter(
    "shop_revenue_dollars_total",
    "Gross value of placed orders, in dollars.",
)

# HISTOGRAM — basket value distribution. This is the metric explored beyond the
# class examples: it answers "are the orders we lose during an incident the
# big ones?", which a counter of orders alone cannot.
ORDER_VALUE = Histogram(
    "shop_order_value_dollars",
    "Value of each placed order, in dollars.",
    buckets=ORDER_VALUE_BUCKETS,
)

# SUMMARY — average basket size in items.
ITEMS_PER_ORDER = Summary(
    "shop_items_per_order",
    "Number of line items per placed order.",
)

# HISTOGRAM — end-to-end checkout, the transaction customers actually feel.
# Separate from http_request_duration_seconds because it measures the business
# operation (stock check + payment + write), not just "a request to a URL".
CHECKOUT_DURATION = Histogram(
    "shop_checkout_duration_seconds",
    "End-to-end duration of a checkout attempt, in seconds.",
    ["outcome"],  # bounded: placed | replayed | rejected | error
    buckets=LATENCY_BUCKETS,
)

# ============================================================ ASSIGNMENT 2 ==
# gRPC server metrics for the order service (written by app/grpc_server.py).
# Names follow the de-facto convention of go-grpc-prometheus / py-grpc-prometheus
# so standard gRPC dashboards and alerts work unchanged. Labels are bounded:
# grpc_service and grpc_method come from the .proto (one service, one method),
# grpc_code is one of the 17 canonical gRPC status codes. Calls to methods the
# server does not implement are never recorded, so a client cannot invent label
# values.

# COUNTER — calls and errors, by method and final status code.
GRPC_HANDLED = Counter(
    "grpc_server_handled_total",
    "RPCs completed on the server, by method and gRPC status code.",
    ["grpc_service", "grpc_method", "grpc_code"],
)

# HISTOGRAM — server-side handling time, from the interceptor receiving the
# call to the handler returning or aborting. Same buckets as the HTTP
# histogram, so p50/p95 of the two interfaces are directly comparable.
GRPC_HANDLING = Histogram(
    "grpc_server_handling_seconds",
    "Server-side duration of an RPC, in seconds.",
    ["grpc_service", "grpc_method"],
    buckets=LATENCY_BUCKETS,
)

# GAUGE — RPCs being handled right now. The server has a fixed thread pool
# (GRPC_MAX_WORKERS); when this reaches that number new calls queue.
GRPC_IN_FLIGHT = Gauge(
    "grpc_server_in_flight",
    "RPCs currently being handled by the server.",
    ["grpc_service", "grpc_method"],
)

# COUNTER — orders answered from an earlier call with the same idempotency
# key instead of being placed (and charged) again. Non-zero means callers are
# retrying, and that the protection is doing its job.
IDEMPOTENT_REPLAYS = Counter(
    "shop_idempotent_replays_total",
    "Order requests answered from an earlier call with the same idempotency key.",
    ["interface"],  # bounded: http | grpc
)

# GAUGES — Postgres pool saturation. Part E.1 of A1 was a pool exhaustion
# incident that no metric showed directly; with two processes now sharing the
# database, each reports its own pool.
DB_POOL_IN_USE = Gauge(
    "db_pool_connections_in_use",
    "Postgres connections currently borrowed from this process's pool.",
)
DB_POOL_MAX = Gauge(
    "db_pool_connections_max",
    "Size of this process's Postgres connection pool.",
)

# ======================================================= CARDINALITY DEMO ====
# Part E.2 only. `request_id` is unbounded — one new time series per request.
# This is the wrong thing to do and exists purely to be measured and removed.
# It is only written to while the /admin/chaos/cardinality toggle is on.
# CARDINALITY_DEMO_LABEL=none builds the same metric without the label, which
# is step 3 of the experiment: remove the label, restart, repeat.
def make_demo_counter(label_mode, registry=REGISTRY):
    return Counter(
        "demo_requests_total",
        "DEMO ONLY: request counter, optionally labelled with an unbounded id.",
        ["request_id"] if label_mode == "request_id" else [],
        registry=registry,
    )


DEMO_REQUESTS = make_demo_counter(CARDINALITY_DEMO_LABEL)
DEMO_HAS_ID_LABEL = CARDINALITY_DEMO_LABEL == "request_id"

# Safe replacement used in step 3 of the experiment: same question, bounded label.
DEMO_REQUESTS_SAFE = Counter(
    "demo_requests_safe_total",
    "DEMO: the same counter with a bounded label instead of a request id.",
    ["tier"],  # bounded: standard | vip
)
