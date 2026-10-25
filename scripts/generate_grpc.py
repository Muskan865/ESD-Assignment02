#!/usr/bin/env python3
"""Regenerate the Python gRPC code from proto/orders.proto.

    pip install grpcio-tools==1.84.1
    python scripts/generate_grpc.py

Equivalent to running, from the repository root:

    python -m grpc_tools.protoc -Iapp/grpc_gen=proto \
        --python_out=services/storefront-api --grpc_python_out=services/storefront-api \
        --pyi_out=services/storefront-api proto/orders.proto

The `-Iapp/grpc_gen=proto` mapping makes protoc treat the .proto as if it lived
at app/grpc_gen/orders.proto, so the generated stub imports its messages as
`from app.grpc_gen import orders_pb2` and works as part of the storefront
package. The output is committed, so building the containers or running the
clients needs only the grpcio and protobuf runtimes, not grpcio-tools.
The Python clients in clients/ import this same generated code.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = "services/storefront-api"


def main() -> int:
    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        "-Iapp/grpc_gen=proto",
        f"--python_out={OUT}", f"--grpc_python_out={OUT}", f"--pyi_out={OUT}",
        "proto/orders.proto",
    ]
    print(" ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        print(f"generated {OUT}/app/grpc_gen/orders_pb2.py, orders_pb2_grpc.py, orders_pb2.pyi")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
