"""F2 smoke — ``_wait_for_router_socket`` bounded-wait behavior.

Regression guard for the birth-time race that FATAL'd fresh seed births:
``macos_self_deployment_plugin.prepare_for_readiness`` checked the router socket
ONE-SHOT and raised immediately if the router (a SEPARATE KeepAlive agent that
comes up independently) had not yet created it. At a fresh birth both agents
load ~simultaneously (RunAtLoad), so the main boot could reach the check a beat
too early -> FATAL first boot + LaunchAgent-restart cycle. The fix bounded-waits
for the socket, only failing loudly past the window.

UPDATED 2026-08-14 (Lane X, adopter issue #4): readiness is now a CONVERSATION,
not a stat. The wait previously polled ``socket_path.exists()``, which is wrong
in both directions — a stale socket file left by a dead router read as HEALTHY
and let the boot proceed against nothing. This smoke's fixtures therefore bind
REAL unix sockets that answer the mgmt ``status`` verb; a plain touched file is
now precisely the failure case (case 4), not a stand-in for a live router.

Cases:

  1. answers_immediately  -> returns fast, no raise (the common, router-up path).
  2. appears_mid_poll     -> returns once the router starts answering (the birth
     race the original fix targets; the one-shot check would have raised here).
  3. absent_raises        -> raises past the window with the "no router is
     installed" diagnosis (no socket file, no router.port sibling).
  4. stale_file_raises    -> socket FILE present, nothing answering: must raise,
     naming the stale-file state. This is the direction the old existence check
     got WRONG — it returned healthy and let the boot proceed against a dead
     router. Guards the whole reason the check became a probe.
  5. unlinked_socket_diagnosis -> no socket file but a router.port sibling: must
     raise naming the overlapping-restart inference (a live router may be
     holding an unlinked socket). DIAGNOSIS ONLY — no path-based check can
     reach an unlinked socket; MgmtServer's ownership check is what prevents
     that state. This case asserts the message, not a recovery.

Sandbox discipline (per ``[[sandbox_mutating_smokes]]``): every socket is bound
inside a TemporaryDirectory under a distinct name, never ``~/.ananta/runtime/``.
The wait window is monkeypatched sub-second so the raising cases are fast.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from macos_self_deployment_plugin import plugin as _plugin
from macos_self_deployment_plugin.plugin import _wait_for_router_socket


class _StatusResponder:
    """Minimal threaded unix-socket server that answers the mgmt `status` verb.

    A real listener, deliberately: the helper under test now CONNECTS and
    speaks, so a fixture that only creates a file would prove nothing.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._sock.settimeout(0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                try:
                    conn.recv(4096)
                    conn.sendall(json.dumps({"ok": True, "colors": {}}).encode() + b"\n")
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._sock.close()
        self._path.unlink(missing_ok=True)


def _stamp(name: str, ok: bool) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def _shrink_window() -> tuple[float, float]:
    orig = (
        _plugin.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS,
        _plugin.DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS,
    )
    _plugin.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS = 0.4
    _plugin.DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS = 0.05
    return orig


def _restore_window(orig: tuple[float, float]) -> None:
    (
        _plugin.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS,
        _plugin.DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS,
    ) = orig


def _case_answers_immediately(tmp: Path) -> bool:
    sock = tmp / "present.router.sock"
    responder = _StatusResponder(sock)
    try:
        start = time.monotonic()
        _wait_for_router_socket(sock)  # must not raise, must be fast
        return (time.monotonic() - start) < 0.5
    finally:
        responder.stop()


def _case_appears_mid_poll(tmp: Path) -> bool:
    sock = tmp / "delayed.router.sock"
    holder: list[_StatusResponder] = []

    def _start_after_delay() -> None:
        time.sleep(0.2)
        holder.append(_StatusResponder(sock))

    starter = threading.Thread(target=_start_after_delay)
    starter.start()
    try:
        _wait_for_router_socket(sock)  # must return once the router answers
        return sock.exists()
    finally:
        starter.join()
        for responder in holder:
            responder.stop()


def _case_absent_raises(tmp: Path) -> bool:
    sock = tmp / "never.router.sock"  # never created, no port sibling
    orig = _shrink_window()
    try:
        _wait_for_router_socket(sock)
        return False  # should have raised
    except RuntimeError as exc:
        text = str(exc)
        return "no router answered" in text and "no router is installed" in text
    finally:
        _restore_window(orig)


def _case_stale_file_raises(tmp: Path) -> bool:
    """A socket file with nothing behind it must NOT read as healthy."""
    sock = tmp / "stale.router.sock"
    abandoned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abandoned.bind(str(sock))
    abandoned.close()  # closing an fd does not unlink: a real stale socket file
    if not sock.exists():
        return False  # fixture failed to leave a stale file — not a real pass
    orig = _shrink_window()
    try:
        _wait_for_router_socket(sock)
        return False  # the old exists()-based check returned HERE
    except RuntimeError as exc:
        return "socket FILE is present but nothing answered" in str(exc)
    finally:
        _restore_window(orig)


def _case_unlinked_socket_diagnosis(tmp: Path) -> bool:
    """Absent socket + surviving router.port names the restart-overlap state."""
    sock = tmp / "overlap.router.sock"
    (tmp / "overlap.router.port").write_text("8801", encoding="utf-8")
    orig = _shrink_window()
    try:
        _wait_for_router_socket(sock)
        return False  # should have raised
    except RuntimeError as exc:
        text = str(exc)
        return "overlapping router restart" in text and "INFERENCE" in text
    finally:
        _restore_window(orig)


def main() -> int:
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        results.append(("answers_immediately", _case_answers_immediately(tmp)))
        results.append(("appears_mid_poll", _case_appears_mid_poll(tmp)))
        results.append(("absent_raises", _case_absent_raises(tmp)))
        results.append(("stale_file_raises", _case_stale_file_raises(tmp)))
        results.append(
            ("unlinked_socket_diagnosis", _case_unlinked_socket_diagnosis(tmp))
        )

    print("\nsummary")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        _stamp(name, ok)
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
