#!/usr/bin/env python3
"""Command-line client for the gRPC OrderService (Assignment 2, Part B).

Calls tinyshop.orders.v1.OrderService/PlaceOrder directly on the order-grpc
container - no HTTP, no browser - with a deadline, and reports the result or
the gRPC status, details and `error-code` trailer.

    pip install -r clients/requirements.txt
    python clients/grpc_order_client.py --item KB-01:2 --customer ayesha
    python clients/grpc_order_client.py --item NOPE-01:1           # NOT_FOUND
    python clients/grpc_order_client.py --item KB-01:0             # INVALID_ARGUMENT
    python clients/grpc_order_client.py --item KB-01:1 --idempotency-key demo-key-0001  # run twice

The generated stub is imported from the storefront package, where
scripts/generate_grpc.py writes it - one copy of the generated code for the
server and every client.

Exit status: 0 on OK, 1 on any gRPC error.
"""

import argparse
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "storefront-api"))
from app.grpc_gen import orders_pb2, orders_pb2_grpc  # noqa: E402

DEFAULT_TARGET = "localhost:50051"

PAYMENT_METHODS = {
    "card": orders_pb2.PAYMENT_METHOD_CARD,
    "wallet": orders_pb2.PAYMENT_METHOD_WALLET,
    "cod": orders_pb2.PAYMENT_METHOD_COD,
}


@dataclass
class GrpcResult:
    code: grpc.StatusCode
    details: str
    error_code: str
    request_id: str
    elapsed_ms: float
    response: "orders_pb2.PlaceOrderResponse | None" = None

    @property
    def ok(self) -> bool:
        return self.code == grpc.StatusCode.OK


def place_order(stub, items, customer="", payment_method="card", idempotency_key="",
                deadline=2.0, request_id=None) -> GrpcResult:
    """One PlaceOrder call. Never raises for an RPC failure; returns its status."""
    request_id = request_id or "grpc-cli-" + uuid.uuid4().hex[:8]
    request = orders_pb2.PlaceOrderRequest(
        items=[orders_pb2.CartLine(sku=line["sku"], quantity=line["quantity"]) for line in items],
        customer=customer,
        payment_method=PAYMENT_METHODS.get(payment_method, orders_pb2.PAYMENT_METHOD_UNSPECIFIED),
        idempotency_key=idempotency_key,
    )
    started = time.perf_counter()
    try:
        # `timeout` is the gRPC deadline: the call fails with DEADLINE_EXCEEDED
        # if the server has not answered by then, and the server sees it too.
        response, call = stub.PlaceOrder.with_call(
            request, timeout=deadline, metadata=(("x-request-id", request_id),))
        elapsed = (time.perf_counter() - started) * 1000
        echoed = dict(call.initial_metadata() or ()).get("x-request-id", request_id)
        return GrpcResult(grpc.StatusCode.OK, "", "", echoed, elapsed, response)
    except grpc.RpcError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        trailers = dict(exc.trailing_metadata() or ())
        initial = dict(exc.initial_metadata() or ()) if hasattr(exc, "initial_metadata") else {}
        return GrpcResult(exc.code(), exc.details() or "", trailers.get("error-code", ""),
                          initial.get("x-request-id", request_id), elapsed)


def describe(result: GrpcResult) -> str:
    if result.ok:
        r = result.response
        return (f"OK in {result.elapsed_ms:.1f} ms: order_id={r.order_id} status={r.status} "
                f"total=${r.total_cents / 100:.2f} items={r.item_count} replayed={r.replayed} "
                f"(x-request-id={result.request_id})")
    return (f"{result.code.name} in {result.elapsed_ms:.1f} ms: {result.details} "
            f"(error-code={result.error_code or '-'}, x-request-id={result.request_id})")


def parse_item(text: str) -> dict:
    sku, _, qty = text.partition(":")
    return {"sku": sku, "quantity": int(qty or 1)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Call OrderService/PlaceOrder over gRPC.")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="host:port of order-grpc")
    parser.add_argument("--item", action="append", type=parse_item, metavar="SKU:QTY",
                        required=True, help="cart line, repeatable")
    parser.add_argument("--customer", default="", help="display name (empty = guest)")
    parser.add_argument("--payment-method", default="card", choices=sorted(PAYMENT_METHODS))
    parser.add_argument("--idempotency-key", default="",
                        help="repeat a call with the same key to get the same order back")
    parser.add_argument("--deadline", type=float, default=2.0, help="seconds")
    parser.add_argument("--request-id", default=None, help="x-request-id to send")
    args = parser.parse_args(argv)

    with grpc.insecure_channel(args.target) as channel:
        stub = orders_pb2_grpc.OrderServiceStub(channel)
        lines = ", ".join(f"{i['sku']}x{i['quantity']}" for i in args.item)
        print(f"-> PlaceOrder [{lines}] customer={args.customer or '(guest)'} "
              f"payment={args.payment_method} deadline={args.deadline:g}s "
              f"idempotency_key={args.idempotency_key or '-'}")
        result = place_order(stub, args.item, args.customer, args.payment_method,
                             args.idempotency_key, args.deadline, args.request_id)
        print("<- " + describe(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
