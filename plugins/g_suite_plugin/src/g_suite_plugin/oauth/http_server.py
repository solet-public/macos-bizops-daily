"""FastAPI app construction and uvicorn lifecycle for the Google callback surface.

Builds a minimal FastAPI application that serves the OAuth callback route. The
server runs in a background daemon thread with its own asyncio event loop,
mirroring the pattern established by schwab_market_data_plugin.

The plugin's start_interface/stop_interface @platform_process methods manage
this module's lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, NoReturn

from ananta.core.runtime import PortManager

from ..constants import OAUTH_CALLBACK_PATH
from .app_config import AppConfigLoader
from .oauth_routes import create_oauth_router
from .token_store import TokenStore

_logger = logging.getLogger(__name__)

# How long start() waits for the socket to actually bind before giving up.
_STARTUP_TIMEOUT_SECONDS: float = 5.0


class OAuthServerStartError(RuntimeError):
    """Raised when the OAuth callback server fails to bind/start.

    The common cause is a port collision — another process already listening on
    the requested port. Surfaced loudly so start_interface never reports a
    server that is not actually accepting the Google redirect (a false success
    sends the operator's browser to whatever else owns the port).
    """


class OAuthServer:
    """Encapsulates the FastAPI/uvicorn OAuth callback server lifecycle."""

    def __init__(self) -> None:
        self._port_manager: PortManager = PortManager("gsuite")
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._started = threading.Event()

    @property
    def port(self) -> int | None:
        return self._port_manager.port

    def start(
        self,
        host: str,
        preferred_port: int | None,
        token_store: TokenStore,
        app_config_loader: AppConfigLoader,
        pending_states: dict[str, str],
    ) -> int:
        """Start the uvicorn server in a background thread and block until the
        socket is actually bound.

        Returns the allocated port on success. Raises ``OAuthServerStartError``
        if the server fails to bind (e.g. the port is already in use) or does
        not report startup within the timeout — it never returns a port for a
        server that is not listening.
        """
        import uvicorn
        from fastapi import FastAPI

        self._port_manager.allocate(preferred_port)
        port = self._port_manager.port
        assert port is not None

        oauth_router = create_oauth_router(token_store, app_config_loader, pending_states)
        self._startup_error = None
        self._started.clear()

        app = FastAPI(title="Google Workspace OAuth Callback", version="1.0.0")
        app.include_router(oauth_router)

        config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
        server = uvicorn.Server(config)

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._server_loop = loop
            try:
                loop.run_until_complete(server.serve())
            except BaseException as exc:
                # BaseException, not Exception, and deliberately so: uvicorn
                # raises SystemExit when it cannot bind, so `except Exception`
                # silently loses a port collision. The error is captured rather
                # than swallowed — start() re-raises it as OAuthServerStartError.
                self._startup_error = exc
                _logger.error(
                    "Google OAuth callback server failed on %s:%d: %s", host, port, exc
                )
            finally:
                loop.close()

        self._server_thread = threading.Thread(target=run, name="gsuite-oauth-srv", daemon=True)
        self._server_thread.start()

        # Block until the socket is bound (server.started) or startup fails.
        # uvicorn flips server.started to True only after create_server() binds,
        # so a port collision surfaces as a captured error here rather than a
        # false success that returns an unbound port.
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._startup_error is not None:
                self._fail_start(host, port, self._startup_error)
            if server.started:
                self._started.set()
                return port
            time.sleep(0.05)

        self._fail_start(
            host,
            port,
            self._startup_error
            or TimeoutError(
                f"server did not report startup within {_STARTUP_TIMEOUT_SECONDS:.0f}s"
            ),
        )

    def _fail_start(self, host: str, port: int, cause: BaseException) -> NoReturn:
        """Release the reserved port and raise a loud, typed start failure."""
        self.stop()
        # uvicorn's bind failure arrives as SystemExit(1), whose str() is a bare
        # "1" — name the type so the message is diagnosable on its own.
        raise OAuthServerStartError(
            f"OAuth callback server could not start on {host}:{port}: "
            f"{type(cause).__name__}: {cause} — the port is most likely already "
            f"in use (uvicorn logs the underlying bind error)"
        ) from cause

    def stop(self) -> None:
        """Gracefully stop the server and release the port."""
        if self._server_loop and self._server_loop.is_running():
            self._server_loop.call_soon_threadsafe(self._server_loop.stop)
        self._port_manager.release()
        self._server_loop = None
        self._started.clear()

    def is_running(self) -> bool:
        return self._server_loop is not None and self._server_loop.is_running()


def build_start_result(port: int, host: str) -> dict[str, Any]:
    return {
        "port": port,
        "host": host,
        "callback_url": f"http://{host}:{port}{OAUTH_CALLBACK_PATH}",
        "message": "OAuth callback server started on the requested bind host.",
    }
