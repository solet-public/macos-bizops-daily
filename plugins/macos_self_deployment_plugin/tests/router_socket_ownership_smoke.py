"""Smoke — mgmt socket OWNERSHIP across an overlapping router restart.

Regression guard for adopter issue #4 (solet-public/macos-bizops), reproduced
2026-08-14. An overlapping restart (`launchctl bootout` immediately followed by
`bootstrap`; also `launchctl kickstart -k`, which is overlapping BY DESIGN)
briefly runs two routers against one socket path, and the mgmt server handled
that window wrongly at BOTH ends:

  - ``stop()`` unlinked ``self._socket_path`` unconditionally, with no check
    that the file there was still the socket it bound. The OUTGOING router's
    shutdown therefore deleted the INCOMING router's socket file. The new
    router kept serving its fd — alive to ``lsof``, absent from disk — and the
    platform child's path-based readiness check FATAL'd at its deadline.
  - ``start()`` unlinked blindly on the way in, which is the same bug facing
    forward: a second router could delete a LIVE one's socket.

The observed failure signature (from the reproduction):

    1. old router bound     -> exists=True inode=128261671
    2. new router bound     -> exists=True inode=128261672 (rebound: True)
    3. OLD router stopped   -> exists=False  <-- NEW router's file
    4. new router serving   -> True (fd held, path gone from disk)

Cases — each asserts ONE leg, so a regression names itself:

  1. late_stop_keeps_new_socket   - the headline case above: R1.stop() AFTER R2
     rebinds must leave R2's file alone, and R2 must still answer ON ITS PATH.
     Guards the (st_dev, st_ino) ownership check in ``_release_socket_path``.
  2. owner_stop_still_unlinks     - the ownership check must not turn stop()
     into a leak: a server that still owns its socket DOES remove it. Without
     this, "never unlink" would pass case 1 while leaving turds everywhere.
     SCOPE, same caveat as case 3 and measured the same way: asyncio's
     ``Server.close()`` unlinks the socket path itself, so this case still
     passes with our explicit unlink mutated out. It guards the PROPERTY (a
     stopped router leaves no socket file behind), not our line of code.
  3. reclaims_dead_socket_file    - a stale socket file (bound, then abandoned
     without cleanup) must be reclaimable, or a crashed router wedges restarts.
     SCOPE, stated so this green is not read as more than it is: this case
     guards the end-to-end PROPERTY, not our implementation of it. CPython's
     own ``create_unix_server`` unlinks any existing socket file at bind, so
     case 3 still passes with our explicit reclaim mutated out — measured, not
     assumed. It will catch a regression that wedges restarts; it will NOT tell
     you which layer did the reclaiming.
  4. refuses_live_socket          - start() against a router that still ANSWERS
     past the reclaim window raises RouterSocketBusyError rather than stealing
     the path. This is the forward-facing half.
  5. waits_out_overlapping_restart - the operator path: an incumbent that stops
     answering INSIDE the window is waited out and reclaimed, not refused.
     Guards against "fix" #4 breaking `kickstart -k`.

Sandbox discipline (per ``[[sandbox_mutating_smokes]]``): every socket is bound
inside a TemporaryDirectory under a distinct name, never ``~/.ananta/runtime/``.
Reclaim waits are constructor-injected sub-second so cases 4/5 are fast.
"""

from __future__ import annotations

# ruff: noqa: E402
import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from macos_self_deployment_plugin.blue_green_router.router_mgmt import (
    MgmtServer,
    RouterSocketBusyError,
)

_PROBE_TIMEOUT_SECONDS = 2.0


async def _dispatch(verb: str, _args: dict[str, object]) -> dict[str, object]:
    """Minimal router-shaped dispatch: enough to answer the `status` verb."""
    if verb == "status":
        return {"ok": True, "colors": {}}
    return {"error": "unknown_verb"}


def _inode(path: Path) -> int | None:
    try:
        return path.stat().st_ino
    except OSError:
        return None


async def _speak_status(path: Path) -> dict[str, object] | None:
    """Connect to `path` and speak `status`; None if nothing answers."""
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(json.dumps({"verb": "status", "args": {}}).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
    except (TimeoutError, OSError):
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
    if not line.strip():
        return None
    payload = json.loads(line.decode())
    return payload if isinstance(payload, dict) else None


async def _close_listener(server: MgmtServer) -> None:
    """Close a server's listener, leaving its recorded ownership intact.

    Models the exact instant the ownership check exists for. ``stop()`` closes
    the listener and unlinks in one call, but those are two steps with an
    ``await`` between them: once the listener is closed the outgoing router has
    stopped answering, so an incoming router's reclaim probe succeeds and it can
    rebind BEFORE the outgoing router reaches its unlink. Splitting the call
    here is what makes that window observable in a test — it is the real
    interleaving, not an artificial one.

    MEASURED, and load-bearing for why the production bug bites: asyncio's unix
    server unlinks the socket path ITSELF on ``close()``. So the outgoing
    router's own close already cleared the path, the incoming router binds a
    fresh inode there, and the outgoing router's SUBSEQUENT explicit unlink is
    what deletes the incoming router's file. The explicit unlink is the whole
    bug; ownership is what makes it safe.
    """
    assert server._server is not None  # noqa: SLF001 - fixture models a race window
    server._server.close()  # noqa: SLF001
    await server._server.wait_closed()  # noqa: SLF001
    server._server = None  # noqa: SLF001


def _abandon_stale_socket_file(path: Path) -> int:
    """Bind a raw unix socket at `path` and abandon it; return its inode.

    Models a router KILLED without cleanup (SIGKILL / panic), which is the only
    way a real stale socket FILE survives: closing a socket fd does not unlink
    its path, and no shutdown hook runs. Deliberately NOT done by closing an
    asyncio server — that unlinks the path on close and would leave nothing
    stale to reclaim, i.e. the fixture would silently test nothing.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as abandoned:
        abandoned.bind(str(path))
        abandoned.listen(1)
        inode = path.stat().st_ino
    return inode


async def _case_late_stop_keeps_new_socket(tmp: Path) -> bool:
    """R1's late unlink, after R2 rebinds, must NOT delete R2's socket file."""
    sock = tmp / "late_stop.router.sock"
    r1 = MgmtServer(sock, _dispatch)
    await r1.start()
    r1_inode = _inode(sock)

    # R1 stops answering (listener closed) but has NOT yet unlinked — the gap
    # inside its own shutdown. R2 probes, finds nothing answering, reclaims and
    # rebinds: a NEW inode at the same path.
    await _close_listener(r1)
    r2 = MgmtServer(sock, _dispatch, reclaim_wait_seconds=0.5)
    await r2.start()
    r2_inode = _inode(sock)
    rebound = r1_inode is not None and r2_inode is not None and r1_inode != r2_inode

    # R1 now finishes its shutdown. This is the unlink that used to delete R2's
    # file and leave the platform child staring at an absent path.
    await r1.stop()

    survived = _inode(sock) == r2_inode and r2_inode is not None
    answers = await _speak_status(sock) is not None  # reachable ON ITS PATH
    await r2.stop()
    return rebound and survived and answers


async def _case_owner_stop_still_unlinks(tmp: Path) -> bool:
    """A server that still owns its socket must remove it on stop()."""
    sock = tmp / "owner_stop.router.sock"
    server = MgmtServer(sock, _dispatch)
    await server.start()
    bound = sock.exists()
    await server.stop()
    return bound and not sock.exists()


async def _case_reclaims_dead_socket_file(tmp: Path) -> bool:
    """A stale socket file left by a dead router must be reclaimable."""
    sock = tmp / "stale.router.sock"
    dead_inode = _abandon_stale_socket_file(sock)
    if not sock.exists():
        return False  # fixture failed to leave a stale file — not a real pass

    fresh = MgmtServer(sock, _dispatch, reclaim_wait_seconds=0.5)
    await fresh.start()
    reclaimed = _inode(sock) not in (None, dead_inode)
    answers = await _speak_status(sock) is not None
    await fresh.stop()
    return reclaimed and answers


async def _case_refuses_live_socket(tmp: Path) -> bool:
    """start() must refuse a path where a router still ANSWERS."""
    sock = tmp / "live.router.sock"
    incumbent = MgmtServer(sock, _dispatch)
    await incumbent.start()
    serving = asyncio.create_task(incumbent.serve_forever())
    await asyncio.sleep(0.05)
    incumbent_inode = _inode(sock)

    intruder = MgmtServer(
        sock, _dispatch, reclaim_wait_seconds=0.3, reclaim_poll_interval_seconds=0.05
    )
    refused = False
    try:
        await intruder.start()
    except RouterSocketBusyError:
        refused = True

    survived = _inode(sock) == incumbent_inode  # incumbent's file untouched
    still_answers = await _speak_status(sock) is not None
    serving.cancel()
    try:
        await serving
    except asyncio.CancelledError:
        pass
    await incumbent.stop()
    return refused and survived and still_answers


async def _case_waits_out_overlapping_restart(tmp: Path) -> bool:
    """An incumbent that stops answering INSIDE the window is waited out.

    This is `launchctl kickstart -k`: overlapping by design. Refusing on sight
    would convert a working operator restart into a hard error, so the incoming
    router must tolerate the outgoing one's graceful-shutdown window.
    """
    sock = tmp / "overlap.router.sock"
    outgoing = MgmtServer(sock, _dispatch)
    await outgoing.start()
    serving = asyncio.create_task(outgoing.serve_forever())
    await asyncio.sleep(0.05)

    async def _shutdown_mid_window() -> None:
        await asyncio.sleep(0.3)  # answers for a while, then goes away
        serving.cancel()
        try:
            await serving
        except asyncio.CancelledError:
            pass
        await outgoing.stop()

    shutdown = asyncio.create_task(_shutdown_mid_window())
    incoming = MgmtServer(
        sock, _dispatch, reclaim_wait_seconds=5.0, reclaim_poll_interval_seconds=0.05
    )
    took_over = True
    try:
        await incoming.start()  # must NOT raise — the window is legitimate
    except RouterSocketBusyError:
        took_over = False
    await shutdown

    answers = took_over and await _speak_status(sock) is not None
    if took_over:
        await incoming.stop()
    return took_over and answers


async def _run() -> list[tuple[str, bool]]:
    cases = (
        ("late_stop_keeps_new_socket", _case_late_stop_keeps_new_socket),
        ("owner_stop_still_unlinks", _case_owner_stop_still_unlinks),
        ("reclaims_dead_socket_file", _case_reclaims_dead_socket_file),
        ("refuses_live_socket", _case_refuses_live_socket),
        ("waits_out_overlapping_restart", _case_waits_out_overlapping_restart),
    )
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for name, case in cases:
            results.append((name, await case(tmp)))
    return results


def main() -> int:
    results = asyncio.run(_run())
    print("\nsummary")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
