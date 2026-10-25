#!/usr/bin/env python3
"""Repeatable success + failure traffic through BOTH order interfaces (A2 Part C).

Each round sends the same seven order requests over HTTP (POST /api/orders)
and over gRPC (OrderService/PlaceOrder), so the dashboard's side-by-side
panels have comparable data:

    request                          HTTP   gRPC
    2 x valid order                  201    OK
    1 x repeat of one, same key      201*   OK*            (* replayed)
    1 x quantity above stock         409    FAILED_PRECONDITION
    1 x unknown SKU                  422    NOT_FOUND
    1 x quantity 0 (invalid)         422    INVALID_ARGUMENT
    1 x payment provider failure     502    UNAVAILABLE     (fail_next=1 fault)

Repeatable on an otherwise idle stack: stock is reset to 19 units per SKU at
the start of every round (the out-of-stock line asks for 20, a valid line buys
1), and the payment failure is injected for exactly one call at a time. The
only randomness left is payment-service's built-in ~3% decline rate, which
turns an occasional valid order into 402 / FAILED_PRECONDITION. Stock goes
back to 500 at the end.

    python clients/traffic.py                     # 6 rounds, 5 s apart (~30 s)
    python clients/traffic.py --rounds 12 --pause 5
"""

import argparse
import collections
import sys
import time
import uuid

import grpc
import requests

from grpc_order_client import DEFAULT_TARGET, orders_pb2_grpc, place_order as grpc_place

HTTP_BASE = "http://localhost:8000"

CASES = [
    # name, items, replay_of_previous, inject_payment_failure
    ("valid", [{"sku": "KB-01", "quantity": 1}], False, False),
    ("valid", [{"sku": "CB-01", "quantity": 1}], False, False),
    ("replay", None, True, False),
    ("out_of_stock", [{"sku": "MN-01", "quantity": 20}], False, False),
    ("unknown_sku", [{"sku": "NOPE-01", "quantity": 1}], False, False),
    ("invalid", [{"sku": "KB-01", "quantity": 0}], False, False),
    ("payment_error", [{"sku": "MS-01", "quantity": 1}], False, True),
]


def admin(base, path, payload):
    requests.post(base + path, json=payload, timeout=5).raise_for_status()


def http_call(base, items, key):
    response = requests.post(base + "/api/orders", timeout=5,
                             json={"items": items, "customer": "traffic"},
                             headers={"Idempotency-Key": key})
    replayed = response.headers.get("Idempotent-Replayed") == "true"
    return f"{response.status_code}{'*' if replayed else ''}"


def grpc_call(stub, items, key):
    result = grpc_place(stub, items, customer="traffic", idempotency_key=key, deadline=5.0)
    replayed = result.ok and result.response.replayed
    return f"{result.code.name}{'*' if replayed else ''}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Repeatable HTTP + gRPC order traffic.")
    parser.add_argument("--base", default=HTTP_BASE)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--pause", type=float, default=5.0, help="seconds between rounds")
    args = parser.parse_args(argv)

    tally = {"http": collections.Counter(), "grpc": collections.Counter()}
    admin(args.base, "/admin/chaos/reset", {})
    try:
        with grpc.insecure_channel(args.target) as channel:
            stub = orders_pb2_grpc.OrderServiceStub(channel)
            for round_no in range(1, args.rounds + 1):
                admin(args.base, "/admin/restock", {"level": 19})
                row = []
                for interface in ("http", "grpc"):
                    previous = None
                    for name, items, replay, fail in CASES:
                        if replay:
                            items, key = previous
                        else:
                            key = str(uuid.uuid4())
                        if fail:
                            admin(args.base, "/admin/chaos/payment-failure",
                                  {"rate": 0.0, "fail_next": 1})
                        if interface == "http":
                            outcome = http_call(args.base, items, key)
                        else:
                            outcome = grpc_call(stub, items, key)
                        previous = (items, key)
                        tally[interface][(name, outcome)] += 1
                        row.append(outcome)
                print(f"round {round_no}/{args.rounds}: http {' '.join(row[:7])} | "
                      f"grpc {' '.join(row[7:])}")
                if round_no < args.rounds:
                    time.sleep(args.pause)
    finally:
        admin(args.base, "/admin/chaos/reset", {})
        admin(args.base, "/admin/restock", {"level": 500})

    print("\ntotals (* = idempotent replay)")
    for interface, counts in tally.items():
        for (name, outcome), n in sorted(counts.items()):
            print(f"  {interface:<5} {name:<14} {outcome:<22} x{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
