"""Supervisor for tunnel-client with dynamic local MCP ingress ports."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Iterable
from pathlib import Path

from .mcp_ingress import ingress_port_file_path, read_router_port
from .service_install import validate_solet_name

logger = logging.getLogger("local_blue_green.tunnel_supervisor")

DEFAULT_MCP_PATH = "/api/v1/mcp/streamable"
DEFAULT_HEALTH_LISTEN_ADDR = "127.0.0.1:0"
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_CHILD_STOP_TIMEOUT_SECONDS = 10.0


class TunnelSupervisor:
    def __init__(
        self,
        *,
        solet: str,
        tunnel_client_path: Path,
        tunnel_id: str,
        control_plane_api_key: str,
        ingress_port_file: Path,
        health_url_file: Path,
        mcp_path: str = DEFAULT_MCP_PATH,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        health_listen_addr: str = DEFAULT_HEALTH_LISTEN_ADDR,
    ) -> None:
        validate_solet_name(solet)
        self.solet = solet
        self.tunnel_client_path = tunnel_client_path
        self.tunnel_id = tunnel_id
        self.control_plane_api_key = control_plane_api_key
        self.ingress_port_file = ingress_port_file
        self.health_url_file = health_url_file
        self.mcp_path = mcp_path if mcp_path.startswith("/") else f"/{mcp_path}"
        self.poll_interval_seconds = poll_interval_seconds
        self.health_listen_addr = health_listen_addr
        self._stop = asyncio.Event()
        self._child: asyncio.subprocess.Process | None = None
        self._active_port: int | None = None

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> int:
        while not self._stop.is_set():
            desired_port = await self._wait_for_ingress_port()
            if self._stop.is_set():
                break
            if self._child is None or self._child.returncode is not None:
                await self._start_child(desired_port)
            elif desired_port != self._active_port:
                logger.info(
                    "ingress port changed for %s: %s -> %s; restarting tunnel-client",
                    self.solet,
                    self._active_port,
                    desired_port,
                )
                await self._stop_child()
                await self._start_child(desired_port)

            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                pass

        await self._stop_child()
        return 0

    async def _wait_for_ingress_port(self) -> int:
        while not self._stop.is_set():
            try:
                return read_router_port(self.ingress_port_file)
            except RuntimeError as exc:
                logger.info("waiting for ingress port file: %s", exc)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
        return 0

    def _mcp_url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}{self.mcp_path}"

    def _child_argv(self, port: int) -> list[str]:
        return [
            str(self.tunnel_client_path),
            "run",
            "--control-plane.tunnel-id",
            self.tunnel_id,
            "--control-plane.api-key",
            self.control_plane_api_key,
            "--mcp.server-url",
            f"url={self._mcp_url(port)},channel=main",
            "--harpoon.allow-plaintext-http",
            "--health.listen-addr",
            self.health_listen_addr,
            "--health.url-file",
            str(self.health_url_file),
        ]

    async def _start_child(self, port: int) -> None:
        argv = self._child_argv(port)
        logger.info(
            "starting tunnel-client for %s against %s",
            self.solet,
            self._mcp_url(port),
        )
        self._child = await asyncio.create_subprocess_exec(*argv)
        self._active_port = port

    async def _stop_child(self) -> None:
        child = self._child
        if child is None:
            return
        if child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(
                    child.wait(),
                    timeout=DEFAULT_CHILD_STOP_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning("tunnel-client did not exit after SIGTERM; killing")
                child.kill()
                await child.wait()
        self._child = None
        self._active_port = None


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    supervisor: TunnelSupervisor,
) -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, supervisor.request_stop)


async def _amain(args: argparse.Namespace) -> int:
    supervisor = TunnelSupervisor(
        solet=args.solet,
        tunnel_client_path=args.tunnel_client_path,
        tunnel_id=args.tunnel_id,
        control_plane_api_key=args.control_plane_api_key,
        ingress_port_file=(
            args.ingress_port_file or ingress_port_file_path(args.solet)
        ),
        health_url_file=args.health_url_file,
        mcp_path=args.mcp_path,
        poll_interval_seconds=args.poll_interval_seconds,
        health_listen_addr=args.health_listen_addr,
    )
    _install_signal_handlers(asyncio.get_running_loop(), supervisor)
    return await supervisor.run()


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tunnel_supervisor",
        description=(
            "Run tunnel-client against the current per-solet MCP ingress "
            "port and restart it when that local ingress port changes."
        ),
    )
    parser.add_argument("--solet", required=True, help="Solet name.")
    parser.add_argument(
        "--tunnel-client-path",
        type=Path,
        required=True,
        help="Path to the tunnel-client binary.",
    )
    parser.add_argument("--tunnel-id", required=True, help="OpenAI tunnel id.")
    parser.add_argument(
        "--control-plane-api-key",
        required=True,
        help="tunnel-client control-plane API key reference, e.g. file:/path.",
    )
    parser.add_argument(
        "--ingress-port-file",
        type=Path,
        default=None,
        help="Port file written by mcp_ingress.",
    )
    parser.add_argument(
        "--health-url-file",
        type=Path,
        required=True,
        help="Where tunnel-client should write its health base URL.",
    )
    parser.add_argument(
        "--mcp-path",
        default=DEFAULT_MCP_PATH,
        help=f"MCP HTTP path (default: {DEFAULT_MCP_PATH}).",
    )
    parser.add_argument(
        "--health-listen-addr",
        default=DEFAULT_HEALTH_LISTEN_ADDR,
        help=f"tunnel-client health listener address (default: {DEFAULT_HEALTH_LISTEN_ADDR}).",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="How often to check the ingress port file.",
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
