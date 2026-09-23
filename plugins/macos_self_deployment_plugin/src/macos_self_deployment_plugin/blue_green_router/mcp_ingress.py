"""Stable local MCP ingress for one solet.

The local blue-green router owns ``<solet>.router.port`` and may be
reinstalled on a different dynamic port. MCP clients should not need to know
that. This ingress binds one stable loopback port and resolves the current
router port from the runtime file for each new TCP connection.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Iterable
from pathlib import Path

from .service_install import RUNTIME_DIR, validate_solet_name

logger = logging.getLogger("local_blue_green.mcp_ingress")

DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 0
BUFFER_SIZE = 65536
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 3.0


def router_port_file_path(solet: str, runtime_dir: Path = RUNTIME_DIR) -> Path:
    return runtime_dir / f"{solet}.router.port"


def ingress_port_file_path(solet: str, runtime_dir: Path = RUNTIME_DIR) -> Path:
    return runtime_dir / f"{solet}.mcp_ingress.port"


def _write_port_file(path: Path, port: int) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(str(port), encoding="utf-8")
    path.chmod(0o600)


def read_router_port(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        port = int(raw)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read valid router port from {path}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"router port out of range in {path}: {port}")
    return port


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(BUFFER_SIZE)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _write_unavailable(
    writer: asyncio.StreamWriter,
    message: str,
) -> None:
    body = (message + "\n").encode("utf-8", errors="replace")
    writer.write(
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"content-type: text/plain; charset=utf-8\r\n"
        + f"content-length: {len(body)}\r\n".encode("ascii")
        + b"connection: close\r\n\r\n"
        + body
    )
    with contextlib.suppress(Exception):
        await writer.drain()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    router_port_file: Path,
) -> None:
    try:
        router_port = read_router_port(router_port_file)
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", router_port),
            timeout=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("mcp ingress could not reach router: %s", exc)
        await _write_unavailable(client_writer, str(exc))
        return

    await asyncio.gather(
        _pipe(client_reader, upstream_writer),
        _pipe(upstream_reader, client_writer),
    )


async def start_ingress_server(
    *,
    solet: str,
    listen_host: str,
    listen_port: int,
    router_port_file: Path | None = None,
    ingress_port_file: Path | None = None,
) -> asyncio.AbstractServer:
    validate_solet_name(solet)
    port_file = router_port_file or router_port_file_path(solet)
    server = await asyncio.start_server(
        lambda reader, writer: handle_connection(
            reader,
            writer,
            router_port_file=port_file,
        ),
        listen_host,
        listen_port,
    )
    sockets = server.sockets or ()
    if ingress_port_file is not None and sockets:
        bound_port = int(sockets[0].getsockname()[1])
        _write_port_file(ingress_port_file, bound_port)
    return server


async def run_ingress(
    *,
    solet: str,
    listen_host: str,
    listen_port: int,
    router_port_file: Path | None = None,
    ingress_port_file: Path | None = None,
) -> None:
    server = await start_ingress_server(
        solet=solet,
        listen_host=listen_host,
        listen_port=listen_port,
        router_port_file=router_port_file,
        ingress_port_file=ingress_port_file or ingress_port_file_path(solet),
    )
    sockets = server.sockets or ()
    bound = ", ".join(str(sock.getsockname()) for sock in sockets)
    logger.info(
        "mcp ingress listening for solet=%s on %s",
        solet,
        bound or f"{listen_host}:{listen_port}",
    )
    async with server:
        await server.serve_forever()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    current_task = asyncio.current_task(loop=loop)
    if current_task is None:
        return

    def cancel() -> None:
        current_task.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, cancel)


async def _amain(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop)
    try:
        await run_ingress(
            solet=args.solet,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            router_port_file=args.router_port_file,
            ingress_port_file=args.ingress_port_file,
        )
    except asyncio.CancelledError:
        return 0
    return 0


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp_ingress",
        description=(
            "Bind a stable loopback MCP ingress and forward each connection "
            "to the current <solet>.router.port."
        ),
    )
    parser.add_argument("--solet", required=True, help="Solet name, e.g. iris.")
    parser.add_argument(
        "--listen-host",
        default=DEFAULT_LISTEN_HOST,
        help=f"Loopback host to bind (default: {DEFAULT_LISTEN_HOST}).",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help=(
            "Ingress port to bind. Use 0 for dynamic allocation "
            f"(default: {DEFAULT_LISTEN_PORT})."
        ),
    )
    parser.add_argument(
        "--router-port-file",
        type=Path,
        default=None,
        help="Override <solet>.router.port path, used by smoke tests.",
    )
    parser.add_argument(
        "--ingress-port-file",
        type=Path,
        default=None,
        help=(
            "Where to publish the bound ingress port "
            "(default: ~/.ananta/runtime/<solet>.mcp_ingress.port)."
        ),
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
