#!/usr/bin/env python3
"""shopctl - drive the Tiny Shop stack: traffic, faults, and experiments.

Standard library only. No `pip install` needed; any Python 3.8+ will do.

    python3 scripts/shopctl.py load --rps 10 --duration 60
    python3 scripts/shopctl.py fault latency --probability 0.2 --seconds 0.5
    python3 scripts/shopctl.py fault off
    python3 scripts/shopctl.py status
    python3 scripts/shopctl.py query 'sum(rate(http_requests_total[1m]))'
    python3 scripts/shopctl.py experiment          # full Part E.1 run
    python3 scripts/shopctl.py cardinality         # full Part E.2 run
    python3 scripts/shopctl.py ratelimit           # A2 Part A: 201s, then 429
"""

import argparse
import json
import math
import os
import pathlib
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

SHOP = os.getenv("SHOP_URL", "http://localhost:8000")
PROM = os.getenv("PROM_URL", "http://localhost:9090")
ES = os.getenv("ES_URL", "http://localhost:9200")

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"

SKUS = [
    ("KB-01", 4900),
    ("MS-01", 1900),
    ("HP-01", 8900),
    ("MN-01", 21900),
    ("CB-01", 900),
]


# --------------------------------------------------------------------- http
def request(method, url, payload=None, timeout=15.0, extra_headers=None):
    """Return (status_code, parsed_body_or_text, elapsed_seconds)."""
    data = None
    headers = {"accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:
        return 0, {"error": type(exc).__name__, "detail": str(exc)}, time.perf_counter() - started
    elapsed = time.perf_counter() - started
    try:
        return status, json.loads(body), elapsed
    except ValueError:
        return status, body, elapsed


def promql(query, quiet=False, at=None):
    """Run an instant PromQL query, evaluated now or at unix time `at`."""
    params = {"query": query}
    if at is not None:
        params["time"] = f"{at:.3f}"
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode(params)
    status, body, _ = request("GET", url)
    if status != 200 or not isinstance(body, dict) or body.get("status") != "success":
        if not quiet:
            print(f"  ! query failed ({status}): {query}", file=sys.stderr)
        return []
    return body["data"]["result"]


def prom_scalar(query, default=None, at=None):
    """First value of an instant query, as a float. NaN (no data) -> default."""
    results = promql(query, at=at)
    if not results:
        return default
    try:
        value = float(results[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return default
    return default if math.isnan(value) else value


def fmt(value, digits=3, suffix=""):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


# ------------------------------------------------------------------ traffic
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.codes = Counter()
        self.latencies = []
        self.submitted = 0

    def record(self, status, elapsed):
        with self.lock:
            self.codes[status] += 1
            self.latencies.append(elapsed)

    def summary(self):
        with self.lock:
            lat = sorted(self.latencies)
            total = len(lat)
            ok = sum(n for code, n in self.codes.items() if 200 <= code < 300)
            return {
                "requests": total,
                "successful": ok,
                "failed": total - ok,
                "status_codes": dict(sorted(self.codes.items())),
                "latency_ms": {
                    "mean": round(statistics.mean(lat) * 1000, 1) if lat else None,
                    "p50": round(lat[int(total * 0.50)] * 1000, 1) if lat else None,
                    "p95": round(lat[min(int(total * 0.95), total - 1)] * 1000, 1) if lat else None,
                    "p99": round(lat[min(int(total * 0.99), total - 1)] * 1000, 1) if lat else None,
                    "max": round(lat[-1] * 1000, 1) if lat else None,
                },
            }


def random_basket():
    lines = random.sample(SKUS, k=random.randint(1, 3))
    return [{"sku": sku, "quantity": random.randint(1, 3)} for sku, _ in lines]


def one_checkout(stats, cancel_rate, fulfil_rate):
    payload = {
        "items": random_basket(),
        "customer": random.choice(["ayesha", "bilal", "chen", "dina", "omar", "guest"]),
        "payment_method": random.choices(["card", "wallet", "cod"], weights=[7, 2, 1])[0],
    }
    # Idempotency-Key so a checkout is never doubled; Assignment 2 renamed the
    # endpoint from POST /api/checkout to POST /api/orders.
    status, body, elapsed = request("POST", f"{SHOP}/api/orders", payload,
                                    extra_headers={"Idempotency-Key": uuid.uuid4().hex})
    stats.record(status, elapsed)

    # Some customers change their mind; some orders ship. Both keep the
    # "awaiting fulfilment" gauge moving in both directions.
    if status == 201 and isinstance(body, dict) and "order_id" in body:
        roll = random.random()
        if roll < cancel_rate:
            request("POST", f"{SHOP}/api/orders/{body['order_id']}/cancel")
        elif roll < cancel_rate + fulfil_rate:
            request("POST", f"{SHOP}/api/orders/{body['order_id']}/fulfil")


def generate_load(rps, duration, cancel_rate=0.10, fulfil_rate=0.35, label=""):
    """Open-loop traffic: requests are submitted on a wall-clock schedule.

    Open-loop matters. A closed-loop generator waits for each response, so when
    the system slows down it quietly sends less traffic and hides the very
    queueing you are trying to observe.
    """
    stats = Stats()
    total = int(rps * duration)
    interval = 1.0 / rps if rps > 0 else 0

    print(f"  -> {label or 'load'}: {rps} rps for {duration}s ({total} checkouts)")
    request("POST", f"{SHOP}/admin/restock", {"level": 2000})

    stop_restock = threading.Event()

    def keep_stocked():
        # Refill every 15s so out_of_stock never becomes the dominant failure
        # and contaminates the experiment.
        while not stop_restock.wait(15.0):
            request("POST", f"{SHOP}/admin/restock", {"level": 2000})

    restocker = threading.Thread(target=keep_stocked, daemon=True)
    restocker.start()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(200, max(8, rps * 4))) as pool:
        for i in range(total):
            due = started + i * interval
            now = time.perf_counter()
            if due > now:
                time.sleep(due - now)
            pool.submit(one_checkout, stats, cancel_rate, fulfil_rate)
            stats.submitted += 1
    stop_restock.set()

    wall = time.perf_counter() - started
    summary = stats.summary()
    summary["target_rps"] = rps
    summary["achieved_rps"] = round(summary["requests"] / wall, 1) if wall else 0
    summary["wall_seconds"] = round(wall, 1)
    return summary


def print_summary(summary):
    lat = summary["latency_ms"]
    print(f"     requests  : {summary['requests']}  "
          f"({summary['successful']} ok, {summary['failed']} failed)")
    print(f"     achieved  : {summary['achieved_rps']} rps vs {summary['target_rps']} target")
    print(f"     status    : {summary['status_codes']}")
    print(f"     client ms : mean {lat['mean']}  p50 {lat['p50']}  "
          f"p95 {lat['p95']}  p99 {lat['p99']}  max {lat['max']}")


# ------------------------------------------------------------- observations
OBSERVATIONS = [
    ("http_p95_seconds",
     'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="storefront-api",route="/api/orders",method="POST"}[1m])))'),
    ("http_p99_seconds",
     'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="storefront-api",route="/api/orders",method="POST"}[1m])))'),
    ("checkout_p95_seconds",
     'histogram_quantile(0.95, sum by (le) (rate(shop_checkout_duration_seconds_bucket[1m])))'),
    ("payment_mean_seconds",
     'sum(rate(payment_call_duration_seconds_sum[1m])) / clamp_min(sum(rate(payment_call_duration_seconds_count[1m])), 0.001)'),
    ("payment_p95_seconds",
     'histogram_quantile(0.95, sum by (le) (rate(payment_call_duration_seconds_hist_bucket[1m])))'),
    ("requests_per_second",
     'sum(rate(http_requests_total{job="storefront-api",route="/api/orders",method="POST"}[1m]))'),
    ("error_rate_5xx_percent",
     '100 * (sum(rate(http_requests_total{job="storefront-api",status=~"5.."}[1m])) or vector(0)) / clamp_min(sum(rate(http_requests_total{job="storefront-api"}[1m])), 0.001)'),
    ("orders_placed_per_min",
     'sum(rate(shop_orders_placed_total[1m])) * 60'),
    ("rejected_payment_error_per_min",
     '(sum(rate(shop_orders_rejected_total{reason=~"payment_error|payment_timeout"}[1m])) or vector(0)) * 60'),
    ("requests_in_flight_max",
     'max_over_time(http_requests_in_flight{job="storefront-api"}[1m])'),
    ("errors_5xx_per_min",
     '(sum(rate(http_requests_total{job="storefront-api",status=~"5.."}[1m])) or vector(0)) * 60'),
    ("db_create_order_p95_seconds",
     'histogram_quantile(0.95, sum by (le) (rate(db_query_duration_seconds_bucket{operation="create_order"}[1m])))'),
    ("db_list_products_p95_seconds",
     'histogram_quantile(0.95, sum by (le) (rate(db_query_duration_seconds_bucket{operation="list_products"}[1m])))'),
    ("orders_awaiting_fulfilment",
     'shop_orders_awaiting_fulfilment'),
    ("orders_placed_total",
     'sum(shop_orders_placed_total)'),
]


def observe(stage, at):
    """Snapshot every headline metric, evaluated at unix time `at`.

    `at` is the end of the stage's traffic, so every rate(...[1m]) window
    covers the last minute of load rather than the idle time after it.
    """
    print(f"  -> reading Prometheus ({stage}, evaluated at end of traffic)")
    values = {}
    for name, query in OBSERVATIONS:
        values[name] = prom_scalar(query, at=at)
    return {"stage": stage, "evaluated_at": iso(at), "metrics": values}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def print_observation(obs):
    m = obs["metrics"]
    print(f"     checkout p95      : {fmt(m['http_p95_seconds'], 3, ' s')}")
    print(f"     checkout p99      : {fmt(m['http_p99_seconds'], 3, ' s')}")
    print(f"     payment mean      : {fmt(m['payment_mean_seconds'], 3, ' s')}  (Summary)")
    print(f"     payment p95       : {fmt(m['payment_p95_seconds'], 3, ' s')}  (Histogram)")
    print(f"     5xx error rate    : {fmt(m['error_rate_5xx_percent'], 2, ' %')}")
    print(f"     orders placed/min : {fmt(m['orders_placed_per_min'], 1)}")
    print(f"     payment failures  : {fmt(m['rejected_payment_error_per_min'], 1)} /min")
    print(f"     5xx errors / min  : {fmt(m['errors_5xx_per_min'], 1)}")
    print(f"     max in flight     : {fmt(m['requests_in_flight_max'], 0)}")
    print(f"     db create_order p95 : {fmt(m['db_create_order_p95_seconds'], 3, ' s')}")
    print(f"     db list_products p95: {fmt(m['db_list_products_p95_seconds'], 3, ' s')}")


# ----------------------------------------------------------- elasticsearch
def es_search(body, index="tiny-shop-logs-*"):
    url = f"{ES}/{index}/_search"
    status, result, _ = request("POST", url, body)
    if status != 200:
        return None
    return result


def sample_logs(query_string, size=3):
    """Fetch a few matching log documents, newest first."""
    body = {
        "size": size,
        "sort": [{"@timestamp": "desc"}],
        "query": {"query_string": {"query": query_string}},
    }
    result = es_search(body)
    if not result:
        return []
    return [hit["_source"] for hit in result.get("hits", {}).get("hits", [])]


def window_query(query_string, start=None, end=None):
    query = {"query_string": {"query": query_string}}
    if start is None:
        return query
    return {"bool": {"must": [query], "filter": [
        {"range": {"@timestamp": {"gte": iso(start), "lte": iso(end)}}}]}}


def count_logs(query_string, start=None, end=None):
    body = {"size": 0, "track_total_hits": True,
            "query": window_query(query_string, start, end)}
    result = es_search(body)
    if not result:
        return None
    return result.get("hits", {}).get("total", {}).get("value")


def wait_for_log_shipping(query_string, start, end, timeout=60):
    """Block until the matching document count stops growing.

    Filebeat ships in batches, so a count taken the moment traffic stops can
    be short by the last few seconds of lines.
    """
    last, deadline = None, time.time() + timeout
    while time.time() < deadline:
        now = count_logs(query_string, start, end)
        if now is not None and now == last:
            return now
        last = now
        time.sleep(3)
    return last


def error_types(start, end):
    """error.type -> count, for 'unhandled exception' logs inside a window."""
    body = {"size": 0,
            "query": window_query('message:"unhandled exception"', start, end),
            "aggs": {"types": {"terms": {"field": "error.type", "size": 10}}}}
    result = es_search(body)
    if not result:
        # An index created before error.type was in the template maps it as
        # text, which cannot be aggregated; its .keyword sub-field can.
        body["aggs"]["types"]["terms"]["field"] = "error.type.keyword"
        result = es_search(body)
    if not result:
        return None
    return {b["key"]: b["doc_count"]
            for b in result.get("aggregations", {}).get("types", {}).get("buckets", [])}


# -------------------------------------------------------------- experiments
def wait_for_scrapes(seconds, why):
    """Let Prometheus collect several samples before reading anything back.

    Every dashboard query uses rate(...[1m]). A 1-minute window needs at least
    two samples inside it to return anything at all, and the value only settles
    once the whole window is filled with post-change data.
    """
    print(f"  -> waiting {seconds}s ({why})")
    time.sleep(seconds)


def set_fault(kind, **kwargs):
    if kind == "latency":
        return request("POST", f"{SHOP}/admin/chaos/payment-latency", kwargs)
    if kind == "failure":
        return request("POST", f"{SHOP}/admin/chaos/payment-failure", kwargs)
    raise ValueError(kind)


def cmd_experiment(args):
    """Part E.1 - reproduce a problem, in five stages."""
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    record = {"experiment": "payment-latency-fault", "started": stamp, "tag": args.tag,
              "fault": {"probability": args.probability, "seconds": args.seconds},
              "stages": []}

    print("=" * 72)
    print("PART E.1 - REPRODUCING A SLOW PAYMENT PROVIDER")
    print("=" * 72)
    print(f"Fault: delay {args.probability:.0%} of payment calls by "
          f"{args.seconds}s (every ~{round(1 / args.probability) if args.probability else 0}th request)")
    print(f"Each stage: {args.duration}s of traffic at {args.rps} rps "
          f"(~{args.duration // 5} Prometheus scrapes at 5s), then a "
          f"{args.scrape_wait}s wait so the last scrapes and logs land.\n")

    request("POST", f"{SHOP}/admin/chaos/reset")

    for stage, apply_fault in (("1-baseline", False), ("2-faulted", True), ("3-recovered", False)):
        print(f"[{stage}]")
        if stage == "2-faulted":
            print(f"  -> INJECTING FAULT: latency {args.probability} x {args.seconds}s")
            set_fault("latency", probability=args.probability, seconds=args.seconds)
        elif stage == "3-recovered":
            print("  -> REMOVING FAULT")
            request("POST", f"{SHOP}/admin/chaos/reset")

        start = time.time()
        load = generate_load(args.rps, args.duration, label=stage)
        end = time.time()
        print_summary(load)
        wait_for_scrapes(args.scrape_wait, "let the final scrapes and log shipping land")
        obs = observe(stage, end)
        print_observation(obs)

        # Elasticsearch query_string syntax: boolean operators are UPPERCASE.
        # (Kibana's KQL, used in the saved searches, takes lowercase `and`.)
        checkout = ('url.route:"/api/orders" AND http.request.method:"POST" '
                    'AND message:"request completed"')
        wait_for_log_shipping(checkout, start, end)
        log_counts = {
            "checkout_requests": count_logs(checkout, start, end),
            "checkout_slow_over_450ms": count_logs(
                checkout + " AND event.duration_ms:>450", start, end),
            "checkout_http_5xx": count_logs(
                checkout + " AND http.response.status_code:[500 TO 599]", start, end),
            "order_placed": count_logs('message:"order placed"', start, end),
            "unhandled_exceptions_by_type": error_types(start, end),
        }
        print(f"     logs (this stage only): {log_counts}")
        record["stages"].append({
            "stage": stage,
            "fault_active": apply_fault,
            "window": {"start": iso(start), "end": iso(end)},
            "client": load,
            "prometheus": obs,
            "log_counts": log_counts,
        })
        print()

    request("POST", f"{SHOP}/admin/chaos/reset")

    # ------------------------------------------------------------- verdict
    def p95(name):
        return record["stages"][["1-baseline", "2-faulted", "3-recovered"].index(name)]["prometheus"]["metrics"]["http_p95_seconds"]

    print("=" * 72)
    print("RESULT")
    print("=" * 72)
    base, faulted, recovered = p95("1-baseline"), p95("2-faulted"), p95("3-recovered")
    print(f"  checkout p95  baseline {fmt(base, 3, ' s')} -> "
          f"faulted {fmt(faulted, 3, ' s')} -> recovered {fmt(recovered, 3, ' s')}")
    if base and faulted:
        print(f"  the fault multiplied p95 by {faulted / base:.1f}x")

    suffix = f"-{args.tag}" if args.tag else ""
    path = RESULTS / f"experiment-{stamp}{suffix}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"\n  full record written to {path.relative_to(ROOT)}")
    return 0


def restart_storefront(label_mode):
    """Recreate storefront-api with CARDINALITY_DEMO_LABEL set, and wait for it.

    A metric's label names are fixed when the process creates it, so removing
    the label genuinely requires a restart - a runtime toggle cannot do it.
    """
    env = dict(os.environ, CARDINALITY_DEMO_LABEL=label_mode)
    print(f"  -> restarting storefront-api with CARDINALITY_DEMO_LABEL={label_mode}")
    subprocess.run(["docker", "compose", "up", "-d", "--no-deps", "--force-recreate",
                    "storefront-api"], cwd=ROOT, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 90
    while time.time() < deadline:
        status, body, _ = request("GET", f"{SHOP}/admin/chaos/status", timeout=3)
        if status == 200 and body["storefront"].get("cardinality_label") == label_mode:
            print(f"     storefront-api is back up (label mode: {label_mode})")
            return
        time.sleep(1)
    raise SystemExit(f"storefront-api did not come back with label mode {label_mode}")


def cmd_cardinality(args):
    """Part E.2 - cardinality explosion, measured, then the label removed."""
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    record = {"experiment": "cardinality", "started": stamp, "steps": []}

    def snapshot(label):
        time.sleep(11)  # at least two 5s scrapes
        values = {
            "demo_requests_series": prom_scalar("count(demo_requests_total)", 0.0),
            "demo_series_seen_last_10m": prom_scalar(
                "count(count_over_time(demo_requests_total[10m]))", 0.0),
            "demo_safe_series": prom_scalar("count(demo_requests_safe_total)", 0.0),
            "tsdb_head_series": prom_scalar("prometheus_tsdb_head_series"),
            "samples_per_second": prom_scalar(
                "rate(prometheus_tsdb_head_samples_appended_total[1m])"),
        }
        print(f"     count(demo_requests_total)          : {values['demo_requests_series']:.0f}")
        print(f"     series stored in the last 10 minutes : {values['demo_series_seen_last_10m']:.0f}")
        print(f"     count(demo_requests_safe_total)      : {values['demo_safe_series']:.0f}")
        print(f"     TSDB head series (whole Prometheus)  : {fmt(values['tsdb_head_series'], 0)}")
        record["steps"].append({"step": label, "at": datetime.now(timezone.utc).isoformat(),
                                "values": values})
        return values

    def send_100():
        print("  -> sending 100 requests, each with a fresh request id")
        request("POST", f"{SHOP}/admin/chaos/cardinality", {"active": True})
        for _ in range(100):
            request("GET", f"{SHOP}/api/products")
        request("POST", f"{SHOP}/admin/chaos/cardinality", {"active": False})

    print("=" * 72)
    print("PART E.2 - CARDINALITY EXPLOSION")
    print("=" * 72)
    print("A counter labelled with request_id creates ONE NEW TIME SERIES PER")
    print("REQUEST. Capped at 100 distinct ids, as the assignment requires.\n")

    try:
        # Start from a known state: default label mode, no demo series.
        print("[0] preparing")
        restart_storefront("request_id")

        print("\n[1] before - demo counter not yet written")
        before = snapshot("1-before")

        print("\n[2] demo_requests_total{request_id=...} - the unbounded label")
        send_100()
        during = snapshot("2-request_id-label")

        print("\n[3] label removed from the code, app restarted, same test repeated")
        restart_storefront("none")
        send_100()
        removed = snapshot("3-label-removed-after-restart")

        print("\n[4] extra: a bounded label (tier = standard | vip), 100 requests")
        for i in range(100):
            tier = "vip" if i % 10 == 0 else "standard"
            request("POST", f"{SHOP}/admin/demo/safe-counter?tier={tier}")
        bounded = snapshot("4-bounded-label")
    finally:
        print("\n[5] restoring the default configuration")
        restart_storefront("request_id")

    print("\n" + "=" * 72)
    print("RESULT")
    print("=" * 72)
    grew = during["demo_requests_series"] - before["demo_requests_series"]
    print(f"  100 requests, request_id label  -> +{grew:.0f} series")
    print(f"  100 requests, label removed     -> {removed['demo_requests_series']:.0f} series")
    print(f"  100 requests, bounded tier label -> {bounded['demo_safe_series']:.0f} series")
    print(f"  but Prometheus still stored {removed['demo_series_seen_last_10m']:.0f} "
          f"distinct demo series in the last 10 minutes")
    print("\n  NOTE: removing the label stops NEW series being created, and the old")
    print("  ones drop out of instant queries once they go stale. Their samples")
    print("  stay on disk until retention deletes them.")

    path = RESULTS / f"cardinality-{stamp}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"\n  full record written to {path.relative_to(ROOT)}")
    return 0


# ------------------------------------------------------------------ commands
def cmd_load(args):
    summary = generate_load(args.rps, args.duration, args.cancel_rate, args.fulfil_rate)
    print_summary(summary)


def cmd_fault(args):
    if args.kind == "off":
        status, body, _ = request("POST", f"{SHOP}/admin/chaos/reset")
        print(json.dumps(body, indent=2))
        return
    if args.kind == "latency":
        status, body, _ = set_fault("latency", probability=args.probability, seconds=args.seconds)
    else:
        status, body, _ = set_fault("failure", rate=args.rate)
    print(json.dumps(body, indent=2))


def cmd_status(args):
    status, body, _ = request("GET", f"{SHOP}/admin/chaos/status")
    print("faults:")
    print(json.dumps(body, indent=2))
    print("\nlive metrics:")
    print_observation(observe("status", time.time()))
    total = count_logs("*")
    print(f"\nlog documents in tiny-shop-logs-*: {total if total is not None else 'unreachable'}")


def cmd_query(args):
    for series in promql(args.expr):
        labels = series.get("metric", {})
        name = labels.pop("__name__", "")
        rendered = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        print(f"{name}{{{rendered}}}  {series['value'][1]}")


def cmd_logs(args):
    docs = sample_logs(args.query, args.size)
    if not docs:
        print("no matching documents (is Filebeat up, and has traffic been sent?)")
        return
    for doc in docs:
        print(json.dumps(doc, indent=2))
        print("-" * 60)


def cmd_setup(args):
    """Import the Kibana data view and the saved searches from
    telemetry/kibana/saved-searches.ndjson, so Discover works without clicking
    through the setup wizard. Safe to re-run: objects are overwritten in place.
    (The Elasticsearch index template is installed by the filebeat container
    itself on start-up; see docker-compose.yml.)"""
    kibana = os.getenv("KIBANA_URL", "http://localhost:5601")
    saved = ROOT / "telemetry" / "kibana" / "saved-searches.ndjson"

    print("  -> waiting for Kibana to be available")
    for attempt in range(60):
        status, body, _ = request("GET", f"{kibana}/api/status", timeout=10.0)
        if status == 200 and isinstance(body, dict) and                 body.get("status", {}).get("overall", {}).get("level") == "available":
            break
        time.sleep(5)
    else:
        print("  ! Kibana did not become available. Is the stack up?")
        return 1

    # The saved-objects import API only accepts a multipart file upload.
    boundary = "tinyshop" + os.urandom(8).hex()
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{saved.name}"\r\n'
            "Content-Type: application/ndjson\r\n\r\n")
    data = head.encode() + saved.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{kibana}/api/saved_objects/_import?overwrite=true", data=data, method="POST",
        # Kibana rejects state-changing calls without kbn-xsrf (CSRF guard).
        headers={"kbn-xsrf": "true",
                 "content-type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"  ! import failed ({exc.code}): {exc.read().decode('utf-8', 'replace')}")
        return 1

    if not body.get("success"):
        print(f"  ! import reported errors: {body.get('errors')}")
        return 1
    for obj in body.get("successResults", []):
        title = obj.get("meta", {}).get("title", obj.get("id"))
        print(f"  OK    {obj.get('type'):<14} {title}")
    print(f"        open {kibana}/app/discover  (Open -> choose a saved search)")
    return 0


def cmd_verify(args):
    """Check every component is up before you trust anything it says."""
    checks = [
        ("storefront-api", f"{SHOP}/health"),
        ("storefront ready (db)", f"{SHOP}/ready"),
        ("payment-service", "http://localhost:8001/health"),
        ("prometheus", f"{PROM}/-/healthy"),
        ("grafana", "http://localhost:3000/api/health"),
        ("elasticsearch", f"{ES}/_cluster/health"),
        ("kibana", "http://localhost:5601/api/status"),
        ("node-exporter", "http://localhost:9100/metrics"),
        ("order-grpc (metrics)", "http://localhost:9102/metrics"),
    ]
    failures = 0
    for name, url in checks:
        status, _, elapsed = request("GET", url, timeout=10.0)
        ok = 200 <= status < 300
        failures += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'}  {name:<24} {status}  ({elapsed * 1000:.0f} ms)")

    print("\n  Prometheus scrape targets:")
    for series in promql("up"):
        job = series["metric"].get("job", "?")
        state = "UP" if series["value"][1] == "1" else "DOWN"
        if state != "UP":
            failures += 1
        print(f"    {state:<5} {job}")

    total = count_logs("*")
    print(f"\n  log documents indexed: {total if total is not None else 'ES unreachable'}")
    print("\n  " + ("all checks passed" if failures == 0
                    else f"{failures} check(s) failed - see README troubleshooting"))
    return 0 if failures == 0 else 1


def _lower(headers):
    return {k.lower(): v for k, v in headers.items()}


def _post_order(payload):
    """POST /api/orders; return (status, headers, body) - the headers matter here."""
    req = urllib.request.Request(
        f"{SHOP}/api/orders", data=json.dumps(payload).encode(), method="POST",
        headers={"content-type": "application/json", "Idempotency-Key": uuid.uuid4().hex})
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            return resp.status, _lower(resp.headers), json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, _lower(exc.headers), json.loads(exc.read() or b"null")


def cmd_ratelimit(args):
    """A2 Part A - a burst over the limit, the 429s, and recovery after Retry-After."""
    request("POST", f"{SHOP}/admin/restock", {"level": 500})
    payload = {"items": [{"sku": "CB-01", "quantity": 1}], "customer": "ratelimit-demo"}
    print(f"  sending {args.requests} order requests from {args.concurrency} threads at once")
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda _: (_post_order(payload), time.perf_counter() - started),
                                range(args.requests)))
    took = time.perf_counter() - started
    counts = Counter(status for (status, _, _), _ in results)
    print(f"  finished in {took:.2f} s")
    for status, n in sorted(counts.items()):
        print(f"    HTTP {status} x {n}")

    limited = [(h, b, t) for (s, h, b), t in results if s == 429]
    accepted = [(h, t) for (s, h, _), t in results if s == 201]
    if accepted:
        h, t = accepted[-1]
        print(f"  last accepted : X-RateLimit-Limit={h.get('x-ratelimit-limit')} "
              f"X-RateLimit-Remaining={h.get('x-ratelimit-remaining')}")
    if not limited:
        print("  no 429 seen - raise --requests or --concurrency")
        return 1
    h, body, t = limited[0]
    print(f"  first 429 at +{t:.2f} s: Retry-After={h.get('retry-after')} "
          f"X-RateLimit-Remaining={h.get('x-ratelimit-remaining')}")
    print("    " + json.dumps(body))
    wait = int(h.get("retry-after", "1"))
    print(f"  waiting Retry-After = {wait} s ...")
    time.sleep(wait)
    status, h, body = _post_order(payload)
    print(f"  next request : HTTP {status} X-RateLimit-Remaining={h.get('x-ratelimit-remaining')}"
          f" order_id={body.get('order_id') if isinstance(body, dict) else '-'}")
    return 0 if status == 201 else 1


def main():
    parser = argparse.ArgumentParser(
        prog="shopctl", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load", help="generate checkout traffic")
    p.add_argument("--rps", type=int, default=10)
    p.add_argument("--duration", type=int, default=30, help="seconds")
    p.add_argument("--cancel-rate", type=float, default=0.10)
    p.add_argument("--fulfil-rate", type=float, default=0.35)
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("fault", help="inject or clear a fault")
    p.add_argument("kind", choices=["latency", "failure", "off"])
    p.add_argument("--probability", type=float, default=0.2)
    p.add_argument("--seconds", type=float, default=0.5)
    p.add_argument("--rate", type=float, default=0.3)
    p.set_defaults(func=cmd_fault)

    p = sub.add_parser("status", help="show faults and live metrics")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify", help="check every component is healthy")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("setup", help="import the Kibana data view and saved searches")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("query", help="run an instant PromQL query")
    p.add_argument("expr")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("logs", help="fetch matching log documents from Elasticsearch")
    p.add_argument("query", nargs="?", default="*")
    p.add_argument("--size", type=int, default=3)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("experiment", help="Part E.1 - full baseline/fault/recovery run")
    p.add_argument("--rps", type=int, default=10)
    p.add_argument("--duration", type=int, default=60, help="seconds of traffic per stage")
    p.add_argument("--scrape-wait", type=int, default=15,
                   help="seconds to wait after each stage before reading results")
    p.add_argument("--tag", default="", help="label added to the result file name")
    p.add_argument("--probability", type=float, default=0.2)
    p.add_argument("--seconds", type=float, default=0.5)
    p.set_defaults(func=cmd_experiment)

    p = sub.add_parser("cardinality", help="Part E.2 - cardinality explosion")
    p.set_defaults(func=cmd_cardinality)

    p = sub.add_parser("ratelimit", help="A2 Part A - exceed the POST /api/orders rate limit")
    p.add_argument("--requests", type=int, default=60)
    p.add_argument("--concurrency", type=int, default=10)
    p.set_defaults(func=cmd_ratelimit)

    args = parser.parse_args()
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\ninterrupted - clearing faults")
        request("POST", f"{SHOP}/admin/chaos/reset")
        return 130


if __name__ == "__main__":
    sys.exit(main())
