"""Runtime configuration for the storefront API."""

import os
import socket

SERVICE_NAME = os.getenv("SERVICE_NAME", "storefront-api")
INSTANCE = socket.gethostname()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shop:shop@postgres:5432/shop")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8001")

# Longer than the largest fault we inject (500 ms) plus normal work, short
# enough that a wedged payment service fails fast instead of piling up workers.
PAYMENT_TIMEOUT_SECONDS = float(os.getenv("PAYMENT_TIMEOUT_SECONDS", "5.0"))

# How long a request may wait for a free Postgres connection before giving up
# with a 503. Without a wait, psycopg2's pool raises the instant all
# connections are busy, which Part E.1 showed turning a slow dependency into
# a burst of HTTP 500s.
DB_POOL_WAIT_SECONDS = float(os.getenv("DB_POOL_WAIT_SECONDS", "2.0"))

# Part E.2 - which label demo_requests_total is built with. Read once at start-up
# because a Prometheus metric's label names are fixed when it is created:
# "removing the label" genuinely means changing this and restarting the app.
#   request_id -> one series per request (the mistake being measured)
#   none       -> a single unlabelled series, however many requests arrive
CARDINALITY_DEMO_LABEL = os.getenv("CARDINALITY_DEMO_LABEL", "request_id")
if CARDINALITY_DEMO_LABEL not in ("request_id", "none"):
    raise ValueError(f"CARDINALITY_DEMO_LABEL must be request_id or none, "
                     f"got {CARDINALITY_DEMO_LABEL!r}")

# Same rationale as the payment service: edges placed so a healthy checkout
# (~30-90 ms) and a faulted one (~530-590 ms) fall in visibly different buckets.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0)

# --- Assignment 2 ---------------------------------------------------------
# Rate limit on POST /api/orders: a token bucket per client IP. RATE tokens are
# added per second up to BURST; each request spends one. 20/s sustained is
# double the A1 load generator (10 rps), so the experiments are never limited.
ORDERS_RATE_LIMIT_PER_SECOND = float(os.getenv("ORDERS_RATE_LIMIT_PER_SECOND", "20"))
ORDERS_RATE_LIMIT_BURST = int(os.getenv("ORDERS_RATE_LIMIT_BURST", "40"))

# The gRPC order service (same code base, separate process/container).
GRPC_PORT = int(os.getenv("GRPC_PORT", "50051"))
GRPC_METRICS_PORT = int(os.getenv("GRPC_METRICS_PORT", "9102"))
# Matches the size of the Postgres pool: a handler thread with no connection
# to use would only wait in db.cursor() anyway.
GRPC_MAX_WORKERS = int(os.getenv("GRPC_MAX_WORKERS", "10"))

# Basket value distribution — dollars, coarse edges, it is a business metric.
ORDER_VALUE_BUCKETS = (10, 25, 50, 100, 150, 250, 500, 1000)
