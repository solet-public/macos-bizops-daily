#!/usr/bin/env python3
"""Smoke: stable MCP ingress follows router.port changes per connection.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/blue_green_router/mcp_ingress_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"),
)

from macos_self_deployment_plugin.blue_green_router.mcp_ingress import (  # noqa: E402
    start_ingress_server,
)


async def _read_http_response(host: str, port: int) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        b"GET /api/v1/mcp/streamable HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Connection: close\r\n\r\n"
    )
    await writer.drain()
    body = await reader.read()
    writer.close()
    await writer.wait_closed()
    return body


async def _start_backend(label: str) -> tuple[asyncio.AbstractServer, int]:
    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        body = label.encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            + f"content-length: {len(body)}\r\n".encode("ascii")
            + b"connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    sockets = server.sockets or []
    if not sockets:
        raise RuntimeError("backend did not bind")
    port = int(sockets[0].getsockname()[1])
    return server, port


async def _run() -> None:
    with tempfile.TemporaryDirectory() as temp:
        router_port_file = Path(temp) / "example.router.port"
        blue, blue_port = await _start_backend("blue")
        green, green_port = await _start_backend("green")
        ingress = await start_ingress_server(
            solet="example",
            listen_host="127.0.0.1",
            listen_port=0,
            router_port_file=router_port_file,
        )
        try:
            sockets = ingress.sockets or []
            if not sockets:
                raise RuntimeError("ingress did not bind")
            ingress_port = int(sockets[0].getsockname()[1])

            router_port_file.write_text(str(blue_port), encoding="utf-8")
            first = await _read_http_response("127.0.0.1", ingress_port)
            if not first.endswith(b"blue"):
                raise AssertionError(f"expected blue response, got {first!r}")

            router_port_file.write_text(str(green_port), encoding="utf-8")
            second = await _read_http_response("127.0.0.1", ingress_port)
            if not second.endswith(b"green"):
                raise AssertionError(f"expected green response, got {second!r}")
        finally:
            ingress.close()
            blue.close()
            green.close()
            await ingress.wait_closed()
            await blue.wait_closed()
            await green.wait_closed()


def main() -> int:
    asyncio.run(_run())
    print("PASS: mcp ingress follows router.port changes per connection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
