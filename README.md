# Tiny Shop — Assignment 1: Observability · Assignment 2: HTTP APIs, gRPC, Metrics

**Enterprise Software Development, Fall 2026**

A very small e-commerce application, fully instrumented with Prometheus metrics
and a JSON logging pipeline into Elasticsearch, plus two reproducible
experiments that break it on purpose and measure what happens.

---

## It includes

| | |
| :-- | :-- |
| **The app** | A storefront: browse a catalogue, add to a cart, place an order, cancel or fulfil it. Orders are stored in Postgres. Payment goes through a separate service. |
| **Metrics** | 23 instrument declarations (20 distinct metric names) across both services — all four Prometheus types — scraped by Prometheus, shown on one provisioned Grafana dashboard with 27 panels. |
| **Host metrics** | Node Exporter: CPU, memory, disk, and network for the machine running the containers. |
| **Logs** | One JSON line per event → Docker → Filebeat → Elasticsearch → Kibana. |
| **Experiments** | A scripted slow-dependency incident, and a scripted cardinality explosion. |
| **A2: HTTP API** | `POST /api/orders` (was `POST /api/checkout`) with `Idempotency-Key`, a per-client rate limit, one `application/problem+json` error format, and a hand-written OpenAPI document served at `/docs`. |
| **A2: gRPC** | `OrderService.PlaceOrder` in its own container (`order-grpc`), running the same business rules (`app/orders.py`) on the same Postgres. |
| **A2: metrics** | `grpc_server_*` metrics scraped by Prometheus and a second dashboard comparing HTTP and gRPC side by side. |
| **A2: callers** | A Python HTTP caller with timeout, retry + backoff and a circuit breaker; a Python gRPC client; a repeatable traffic script. |

The Assignment 2 write-up is **[REPORT-A2.md](REPORT-A2.md)** (Assignment 1: [REPORT.md](REPORT.md)).

---

## Prerequisites

- **Docker Engine with the Compose v2 plugin.** 
- **Python 3.8 or newer** 
- **About 6 GB of free RAM and 5 GB of free disk.** 
---

## 1. Start

From the root of this folder:

```bash
docker compose up -d --build
```

### Then verify everything is actually up

```bash
python3 scripts/shopctl.py verify
```

### Import the Kibana data view and saved searches (one command, once)

```bash
python3 scripts/shopctl.py setup
```
---

## 2. Use it

| What | Where |
| :-- | :-- |
| **The shop** | **<http://localhost:8000>** — browse, add to cart, place, cancel, fulfil |
| **The dashboard** | **<http://localhost:3000/d/tiny-shop>** — login `admin` / `admin` |
| **A2 dashboard: HTTP vs gRPC** | **<http://localhost:3000/d/order-interfaces>** |
| **gRPC OrderService** | `localhost:50051` (metrics: <http://localhost:9102/metrics>) |
| **Logs** | **<http://localhost:5601/app/discover>** — pick "Tiny Shop Logs" |
| API docs (Swagger) | <http://localhost:8000/docs> (renders `services/storefront-api/openapi.yaml`) and <http://localhost:8001/docs> |
| Raw metrics | <http://localhost:8000/metrics>, <http://localhost:8001/metrics> |
| Prometheus targets | <http://localhost:9090/targets> |
| PromQL console | <http://localhost:9090/graph> |
| Node Exporter raw | <http://localhost:9100/metrics> |
| Elasticsearch indices | <http://localhost:9200/_cat/indices?v> |

### Generate some traffic

```bash
# 10 checkouts per second for 30 seconds
python3 scripts/shopctl.py load --rps 10 --duration 30
```

The generator places orders with random baskets, cancels about 10% of them and
fulfils about 35%, so the "awaiting fulfilment" gauge moves in both directions
rather than climbing forever. It also restocks the shelves every 15 seconds —
otherwise 227 units of starting inventory would sell out in half a minute and
every later checkout would fail with `out_of_stock`, which would contaminate the
experiments.

### Look at what happened

```bash
# live metrics + which faults are active
python3 scripts/shopctl.py status

# any PromQL expression
python3 scripts/shopctl.py query 'sum by (reason) (rate(shop_orders_rejected_total[1m])) * 60'

# any Elasticsearch/Kibana query
python3 scripts/shopctl.py logs 'message:"order placed"' --size 2
```

---

## 3. Test — the two experiments

Both are fully scripted, print their results to the terminal, and write a
timestamped JSON record into `results/`. Both are safe and both undo themselves.

### Part E.1 — a slow payment provider

```bash
python3 scripts/shopctl.py experiment
```

Runs three stages — **baseline → fault injected → fault removed** — each with
60 seconds of traffic (about 12 Prometheus scrapes). Every metric is read with
the PromQL query evaluated at the moment that stage's traffic ended, so each
`rate(...[1m])` window covers exactly that stage. Takes about 4–5 minutes. The
fault is a 500 ms delay on every fifth payment call. Add `--tag <name>` to label
the result file.

To drive it by hand instead:

```bash
python3 scripts/shopctl.py fault latency --probability 0.2 --seconds 0.5
python3 scripts/shopctl.py load --rps 10 --duration 60
python3 scripts/shopctl.py fault off          # always safe to run
```

### Part E.2 — cardinality explosion

```bash
python3 scripts/shopctl.py cardinality
```

Sends exactly 100 requests to a counter
labelled with the request id and measures the series growth; then **removes the
label and restarts `storefront-api`** (it recreates the container with
`CARDINALITY_DEMO_LABEL=none`), repeats the 100 requests and compares after
another scrape; then, as an extra, sends 100 requests through a bounded label.
Finally it restarts the storefront in its default configuration. Takes about a
minute.

### Undoing a fault

`python3 scripts/shopctl.py fault off` clears every injected fault in both
services. So does restarting the containers — all fault state is in-process and
defaults to off:

```bash
docker compose restart storefront-api payment-service
```

If the cardinality experiment is interrupted while the label is removed, put the
storefront back in its default configuration with:

```bash
docker compose up -d --force-recreate storefront-api
```

---

## 4. Assignment 2 — run and test

The stack now has a tenth container, `order-grpc`. `docker compose up -d --build`
starts it; `shopctl verify` checks it. An existing Assignment 1 database is
upgraded in place on start-up (two new columns), so `down -v` is not needed.

The Python callers need three packages:

```bash
pip install -r clients/requirements.txt
```

| Part | What | Command |
| :-- | :-- | :-- |
| A | Rate limit: a burst over the limit, the `429` + `Retry-After`, recovery | `python3 scripts/shopctl.py ratelimit` |
| A | Caller reliability demo: success, retry, timeout + replay, breaker open, recovery | `python3 clients/order_caller.py --demo` |
| A | The caller as a plain client | `python3 clients/order_caller.py --calls 3 --item KB-01:1` |
| A | Try the API from its OpenAPI document | <http://localhost:8000/docs> → `POST /api/orders` → *Try it out* |
| B | gRPC: one success, then failures | `python3 clients/grpc_order_client.py --item KB-01:2 --customer ayesha`<br>`python3 clients/grpc_order_client.py --item NOPE-01:1`<br>`python3 clients/grpc_order_client.py --item KB-01:0` |
| B | gRPC idempotency (run twice: same order, `replayed=True`) | `python3 clients/grpc_order_client.py --item CB-01:1 --idempotency-key demo-key-0001` |
| C | Repeatable success + failure traffic through both interfaces | `python3 clients/traffic.py` |

Raw HTTP, for comparison:

```bash
curl -i -X POST localhost:8000/api/orders -H 'content-type: application/json' \
     -H 'Idempotency-Key: demo-key-http-01' -d '{"items":[{"sku":"KB-01","quantity":2}]}'
```

**Regenerating the gRPC code** after editing `proto/orders.proto` (the output is
committed, so this is only needed after a change):

```bash
pip install grpcio-tools==1.84.1
python3 scripts/generate_grpc.py
```

**Offline tests** (no Docker) — see `tests/README.md`:

```bash
pip install fastapi httpx prometheus-client requests grpcio protobuf pyyaml jsonschema
python3 tests/test_assignment2.py      # A2: API, OpenAPI contract, caller, gRPC server
python3 tests/test_storefront_api.py
python3 tests/test_payment_service.py
```

---

## 5. Clean up

```bash
# stop the containers, keep the data (orders, metrics history, log indices)
docker compose down

# stop and delete everything, including all volumes — a full factory reset
docker compose down -v
```
To also reclaim the built images:

```bash
docker compose down -v --rmi local
```

---

## Layout

```
docker-compose.yml              the whole stack, 10 containers
proto/orders.proto              A2: the gRPC contract (OrderService.PlaceOrder)
db/init.sql                     schema + seed products

services/storefront-api/        the shop (FastAPI, port 8000) - and, as order-grpc, the gRPC service
  openapi.yaml                  A2: the HTTP contract, served at /openapi.json and /docs
  app/orders.py                 A2: placing an order - business rules shared by HTTP and gRPC
  app/grpc_server.py            A2: gRPC handler, interceptor (metrics, logs, request id)
  app/grpc_gen/                 A2: generated from proto/orders.proto (scripts/generate_grpc.py)
  app/errors.py                 A2: the problem+json error format
  app/ratelimit.py              A2: token-bucket rate limiter
  app/telemetry.py              EVERY metric is declared here
  app/routes.py                 business metrics + business logs recorded here
  app/application.py            middleware: app metrics + request logs
  app/structured_logging.py     the JSON log format
  app/clients.py                outbound calls to payment, instrumented
  app/db.py                     Postgres pool, query timing
  app/static/index.html         the storefront page

services/payment-service/       mock payment provider (FastAPI, port 8001)
  app/routes.py                 charges, and the fault-injection endpoints

telemetry/prometheus/           scrape config
telemetry/grafana/              datasource + dashboard provisioning
telemetry/grafana/dashboards/   tiny-shop.json (A1, 27 panels), order-interfaces.json (A2)
telemetry/filebeat/             log shipping config (daily indices, no ILM)
telemetry/kibana/               data view + saved searches (imported by `setup`)

scripts/shopctl.py              traffic, faults, experiments, verification, rate-limit demo
scripts/generate_grpc.py        A2: protoc command for the gRPC code
clients/                        A2: Python HTTP caller, gRPC client, traffic generator
tests/                          offline tests (no Docker): A1 suites + test_assignment2.py
results/                        experiment records (JSON + terminal output) behind REPORT.md Part E
docs/architecture.md            architecture diagram + metric/log walkthroughs
REPORT.md                       the written answers, Parts A-E
REPORT-A2.md                    Assignment 2 report
```

---
