#!/usr/bin/env python3
"""Serve the released Python conformance WSGI app with pinned Granian."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

from vgi_rpc.conformance._impl import ConformanceServiceImpl
from vgi_rpc.conformance._protocol import ConformanceService
from vgi_rpc.http import make_wsgi_app
from vgi_rpc.rpc import RpcServer


app = make_wsgi_app(
    RpcServer(ConformanceService, ConformanceServiceImpl()),
    token_key=b"vgi-rpc-benchmark-token-key-0001",
)


def main() -> None:
    """Select a loopback port, report it to the driver, and exec Granian."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    print(f"PORT:{port}", flush=True)
    granian = Path(sys.executable).with_name("granian")
    os.execv(
        granian,
        [
            str(granian),
            "--interface",
            "wsgi",
            "--http",
            "1",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "16",
            "--blocking-threads",
            "1",
            "--runtime-threads",
            "1",
            "--no-log",
            "--working-dir",
            str(Path(__file__).resolve().parents[2]),
            "benchmarks.adapters.python_granian:app",
        ],
    )


if __name__ == "__main__":
    main()
