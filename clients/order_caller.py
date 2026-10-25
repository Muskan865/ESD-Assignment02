#!/usr/bin/env python3
"""A service-to-service caller for `POST /api/orders` (Assignment 2, Part A).

It plays the part of another service (say, a partner marketplace) placing
orders through the storefront's HTTP API, with the three standard defences a
caller needs:

  timeout          every attempt: 1.0 s to connect, 2.0 s to read the response.
  retry + backoff  up to 3 attempts per call (the first plus 2 retries), only
                   for transient failures - connection error, timeout, 502,
                   503, 504. Waits 0.2 s, then 0.4 s (doubling, capped at
                   2 s), plus up to 50 ms of random jitter.
  circuit breaker  opens after 4 consecutive failed attempts. While open,
                   calls fail at once without touching the network. After 5 s
                   it lets one trial call through (half-open): success closes
                   it, failure opens it for another 5 s.

Why the retry is safe: placing an order is not naturally idempotent - a blind
retry after a lost response could place and charge a second order. Every
logical order therefore gets ONE Idempotency-Key, sent unchanged on every
attempt; the server returns the original order to any repeat. The same
x-request-id is sent on every attempt too, so all attempts of one call line up
in Kibana. 4xx answers (declined, out of stock, invalid) are business
outcomes, not failures: never retried, never counted by the breaker. A 429 is
neither retried nor counted: the server is healthy, we are just over quota.

    pip install -r clients/requirements.txt
    python clients/order_caller.py --calls 3 --item KB-01:1        # plain use
    python clients/order_caller.py --demo                          # the A2 test

`--demo` drives the payment-service fault injection through the storefront's
/admin endpoints to show, in one run: success; a transient failure retried;
a timed-out attempt retried and answered as a replay; the breaker opening
and blocking calls while the provider is down; and recovery.
"""

import argparse
import random
import sys
import time
import uuid
from dataclasses import dataclass, field

import requests

DEFAULT_BASE = "http://localhost:8000"
RETRYABLE_STATUSES = {502, 503, 504}


# ------------------------------------------------------------ circuit breaker
class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(self, failure_threshold=4, recovery_seconds=5.0, clock=time.monotonic,
                 log=print):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._log = log
        self.state = self.CLOSED
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self._trial_in_flight = False

    def _move(self, new_state, why):
        if new_state != self.state:
            self._log(f"      breaker: {self.state} -> {new_state} ({why})")
            self.state = new_state

    def allow(self) -> bool:
        """May a request go out now?"""
        if self.state == self.OPEN:
            if self._clock() - self.opened_at < self.recovery_seconds:
                return False
            self._move(self.HALF_OPEN, f"{self.recovery_seconds:g} s recovery period over; "
                                       "allowing one trial call")
            self._trial_in_flight = False
        if self.state == self.HALF_OPEN:
            if self._trial_in_flight:
                return False  # one trial at a time
            self._trial_in_flight = True
        return True

    def record_success(self):
        self.consecutive_failures = 0
        self._trial_in_flight = False
        self._move(self.CLOSED, "call succeeded")

    def record_failure(self):
        self.consecutive_failures += 1
        self._trial_in_flight = False
        if self.state == self.HALF_OPEN:
            self.opened_at = self._clock()
            self._move(self.OPEN, f"trial call failed; blocking for {self.recovery_seconds:g} s")
        elif self.state == self.CLOSED and self.consecutive_failures >= self.failure_threshold:
            self.opened_at = self._clock()
            self._move(self.OPEN, f"{self.consecutive_failures} consecutive failures; "
                                  f"blocking for {self.recovery_seconds:g} s")

    def seconds_until_trial(self) -> float:
        if self.state != self.OPEN:
            return 0.0
        return max(0.0, self.recovery_seconds - (self._clock() - self.opened_at))


# --------------------------------------------------------------------- caller
@dataclass
class CallResult:
    outcome: str            # placed | replayed | rejected | failed | blocked | rate_limited
    status: int | None      # final HTTP status, None if no response
    attempts: int
    body: dict | str | None = None
    detail: str = ""
    elapsed: float = 0.0
    history: list = field(default_factory=list)


class OrderCaller:
    def __init__(self, base_url=DEFAULT_BASE, connect_timeout=1.0, read_timeout=2.0,
                 max_attempts=3, backoff_base=0.2, backoff_cap=2.0, breaker=None,
                 session=None, log=print, sleep=time.sleep):
        self.url = base_url.rstrip("/") + "/api/orders"
        self.timeout = (connect_timeout, read_timeout)
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.breaker = breaker or CircuitBreaker(log=log)
        self.session = session or requests.Session()
        self._log = log
        self._sleep = sleep

    def backoff(self, attempt: int) -> float:
        """Delay before attempt `attempt + 1`: 0.2, 0.4, 0.8 ... capped, plus jitter."""
        delay = min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
        return delay + random.uniform(0, 0.05)

    def place_order(self, items, customer="caller-svc", payment_method="card",
                    label="call") -> CallResult:
        idempotency_key = str(uuid.uuid4())     # one per logical order ...
        request_id = "caller-" + uuid.uuid4().hex[:8]  # ... reused on every attempt
        payload = {"items": items, "customer": customer, "payment_method": payment_method}
        headers = {"Idempotency-Key": idempotency_key, "x-request-id": request_id}
        started = time.perf_counter()
        history = []

        self._log(f"  [{label}] request_id={request_id} idempotency_key={idempotency_key[:8]}...")
        for attempt in range(1, self.max_attempts + 1):
            if not self.breaker.allow():
                wait = self.breaker.seconds_until_trial()
                self._log(f"      attempt {attempt}: BLOCKED by open breaker "
                          f"(no request sent; trial allowed in {wait:.1f} s)")
                return CallResult("blocked", None, attempt - 1, detail="circuit open",
                                  elapsed=time.perf_counter() - started, history=history)

            t0 = time.perf_counter()
            try:
                response = self.session.post(self.url, json=payload, headers=headers,
                                             timeout=self.timeout)
            except requests.Timeout:
                ms = (time.perf_counter() - t0) * 1000
                history.append(("timeout", ms))
                self._log(f"      attempt {attempt}/{self.max_attempts}: TIMEOUT after "
                          f"{ms:.0f} ms")
                self.breaker.record_failure()
            except requests.ConnectionError as exc:
                ms = (time.perf_counter() - t0) * 1000
                history.append(("connection_error", ms))
                self._log(f"      attempt {attempt}/{self.max_attempts}: CONNECTION ERROR "
                          f"({type(exc).__name__}) after {ms:.0f} ms")
                self.breaker.record_failure()
            else:
                ms = (time.perf_counter() - t0) * 1000
                body = _json(response)
                code = body.get("code", "") if isinstance(body, dict) else ""
                history.append((response.status_code, ms))
                if response.status_code in RETRYABLE_STATUSES:
                    self._log(f"      attempt {attempt}/{self.max_attempts}: "
                              f"{response.status_code} {code} in {ms:.0f} ms (transient)")
                    self.breaker.record_failure()
                else:
                    return self._finish(response, body, code, attempt, ms, started, history)

            if self.breaker.state == CircuitBreaker.OPEN:
                # This failure tripped the breaker: waiting to retry would only
                # end in a blocked attempt.
                self._log("      not retrying: the breaker is open")
                return CallResult("failed", history[-1][0] if isinstance(history[-1][0], int)
                                  else None, attempt, detail="breaker opened",
                                  elapsed=time.perf_counter() - started, history=history)
            if attempt < self.max_attempts:
                delay = self.backoff(attempt)
                self._log(f"      retrying in {delay:.2f} s with the same Idempotency-Key")
                self._sleep(delay)

        self._log(f"      gave up after {self.max_attempts} attempts")
        return CallResult("failed", history[-1][0] if isinstance(history[-1][0], int) else None,
                          self.max_attempts, detail="retries exhausted",
                          elapsed=time.perf_counter() - started, history=history)

    def _finish(self, response, body, code, attempt, ms, started, history):
        elapsed = time.perf_counter() - started
        status = response.status_code
        if status == 201:
            self.breaker.record_success()
            replayed = response.headers.get("Idempotent-Replayed") == "true"
            outcome = "replayed" if replayed else "placed"
            self._log(f"      attempt {attempt}/{self.max_attempts}: 201 in {ms:.0f} ms -> "
                      f"order {body.get('order_id')} {body.get('status')} "
                      f"${body.get('total_dollars', 0):.2f}"
                      + ("  (REPLAYED: no second order)" if replayed else ""))
            return CallResult(outcome, status, attempt, body, elapsed=elapsed, history=history)
        if status == 429:
            # Neither a success nor a failure of the server: do not move the breaker.
            self._log(f"      attempt {attempt}/{self.max_attempts}: 429 rate limited, "
                      f"Retry-After {response.headers.get('Retry-After')} s (not retried)")
            return CallResult("rate_limited", status, attempt, body, code, elapsed, history)
        # 4xx business outcome: the server is healthy and answered.
        self.breaker.record_success()
        self._log(f"      attempt {attempt}/{self.max_attempts}: {status} {code} in {ms:.0f} ms "
                  "(business outcome, not retried)")
        return CallResult("rejected", status, attempt, body, code, elapsed, history)


def _json(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def parse_item(text: str) -> dict:
    sku, _, qty = text.partition(":")
    return {"sku": sku, "quantity": int(qty or 1)}


# ----------------------------------------------------------------------- demo
def _admin(base, path, payload=None):
    response = requests.post(base + path, json=payload if payload is not None else {}, timeout=5)
    response.raise_for_status()


def run_demo(args) -> int:
    base = args.base.rstrip("/")
    caller = OrderCaller(base, args.connect_timeout, args.timeout, args.attempts,
                         args.backoff, breaker=CircuitBreaker(args.failures, args.recovery))
    print("settings: timeout %.1f s connect / %.1f s read; %d attempts max; backoff %.1f s "
          "doubling (cap 2 s) + <=50 ms jitter; breaker opens after %d consecutive "
          "failures, recovery %.0f s" % (args.connect_timeout, args.timeout, args.attempts,
                                         args.backoff, args.failures, args.recovery))
    results = []
    item = [{"sku": "CB-01", "quantity": 1}]

    def call(label):
        result = caller.place_order(item, label=label)
        results.append((label, result))
        return result

    try:
        _admin(base, "/admin/chaos/reset")
        _admin(base, "/admin/restock", {"level": 500})

        print("\n== 1. healthy provider: calls succeed first time")
        for i in range(1, 4):
            call(f"healthy-{i}")

        print("\n== 2. one transient failure (the next payment call fails with 503 -> 502)")
        _admin(base, "/admin/chaos/payment-failure", {"rate": 0.0, "fail_next": 1})
        call("transient")

        print(f"\n== 3. lost response: payment takes {args.timeout + 0.5:.1f} s, longer than the "
              f"{args.timeout:.1f} s read timeout. The server finishes the order after the caller "
              "gave up; the retry must not place a second one")
        _admin(base, "/admin/chaos/payment-latency",
               {"probability": 1.0, "seconds": args.timeout + 0.5})
        call("lost-response")
        _admin(base, "/admin/chaos/reset")

        print("\n== 4. provider down (every payment call fails): retries exhausted, breaker opens")
        _admin(base, "/admin/chaos/payment-failure", {"rate": 1.0})
        for i in range(1, 4):
            call(f"outage-{i}")

        print("\n== 5. provider repaired, but the breaker is still open")
        _admin(base, "/admin/chaos/reset")
        call("still-open")
        wait = caller.breaker.seconds_until_trial() + 0.1
        print(f"  ... waiting {wait:.1f} s for the recovery period")
        time.sleep(wait)
        for i in range(1, 4):
            call(f"recovered-{i}")
    finally:
        try:
            _admin(base, "/admin/chaos/reset")
        except requests.RequestException:
            pass

    print("\nsummary")
    print(f"  {'call':<15} {'outcome':<13} {'attempts':>8} {'final':>6} {'elapsed':>9}")
    for label, r in results:
        final = r.status if r.status is not None else "-"
        print(f"  {label:<15} {r.outcome:<13} {r.attempts:>8} {final!s:>6} {r.elapsed:>8.2f}s")
    return 0


# ----------------------------------------------------------------------- main
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default=DEFAULT_BASE, help="storefront base URL")
    parser.add_argument("--item", action="append", type=parse_item, metavar="SKU:QTY",
                        help="cart line, repeatable (default CB-01:1)")
    parser.add_argument("--calls", type=int, default=3, help="number of orders to place")
    parser.add_argument("--timeout", type=float, default=2.0, help="read timeout, seconds")
    parser.add_argument("--connect-timeout", type=float, default=1.0)
    parser.add_argument("--attempts", type=int, default=3, help="max attempts per call")
    parser.add_argument("--backoff", type=float, default=0.2, help="first retry delay, seconds")
    parser.add_argument("--failures", type=int, default=4,
                        help="consecutive failures that open the breaker")
    parser.add_argument("--recovery", type=float, default=5.0,
                        help="seconds the breaker stays open")
    parser.add_argument("--demo", action="store_true",
                        help="run the scripted reliability demonstration (needs the stack)")
    args = parser.parse_args(argv)

    if args.demo:
        return run_demo(args)

    caller = OrderCaller(args.base, args.connect_timeout, args.timeout, args.attempts,
                         args.backoff, breaker=CircuitBreaker(args.failures, args.recovery))
    items = args.item or [{"sku": "CB-01", "quantity": 1}]
    failed = 0
    for i in range(1, args.calls + 1):
        result = caller.place_order(items, label=f"call-{i}")
        failed += result.outcome in ("failed", "blocked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
