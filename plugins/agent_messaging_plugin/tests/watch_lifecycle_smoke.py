#!/usr/bin/env python3
"""Watcher lifecycle smoke — W1 singleton, W2 SIGTERM unwind, --exit-with-parent.

Two watchers under one ``$AGENT_SESSION_ID`` are byte-identical
(same instance id, same spool, same wake hook), so nothing downstream can tell
them apart and the second one silently re-points the registry binding. W1 makes
the kernel the arbiter; W2 makes a terminated watcher evict its own row instead
of leaving one that dispatch then reports as ``queued_watcher``.

Hermetic by construction: ``XDG_RUNTIME_DIR`` is redirected to a temp dir, so
no port file is discoverable and every watcher parks in its reconnect backoff
without reaching any solet — which is exactly the state that holds the
flock. Real subprocesses, because ``flock`` is a property of a process's open
file description and an in-process runner cannot exercise it honestly.

Project policy: stdlib-only, no pytest. Run with::

    python3 plugins/agent_messaging_plugin/tests/watch_lifecycle_smoke.py
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import os  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "plugins/agent_messaging_plugin/src"

sys.path.insert(0, str(_SRC))

from agent_messaging_plugin.local_cli.cli import _parent_is_gone  # noqa: E402
from agent_messaging_plugin.local_cli.client import resolve_solet_name  # noqa: E402
from agent_messaging_plugin.local_cli.spool import (  # noqa: E402
    watch_instance_digest,
    watch_singleton_lock_path,
)
from agent_messaging_plugin.models import WATCH_AGENT_INSTANCE_PREFIX  # noqa: E402

# SIGTERM handled by W2's handler -> SystemExit(128+15). Terminated by the
# DEFAULT disposition -> Python reports returncode -15. The two are distinct,
# which is what makes W2 provable rather than merely plausible: a test that
# only asserted "the process ended" would pass with the handler deleted.
_SIGTERM_HANDLED_EXIT = 128 + signal.SIGTERM
_SIGTERM_DEFAULT_RC = -signal.SIGTERM

_SESSION_ID = "ases-watch-lifecycle-smoke-0001"
_ROLE = "Watch-Lifecycle-Smoke"
# `watch`'s own identity resolution (resolve_solet_name) derives the name
# from this checkout's root_manifest.yaml / clone-dir basename, NOT from the
# SOLET_NAME env var this test sets for the child — a caller override
# cannot change what the child independently resolves. Deriving here the same
# way keeps the two in agreement on any checkout (this one or a newborn's),
# rather than pinning a literal that only happens to match this repo's name.
_SOLET = resolve_solet_name()

_LAUNCH = "from agent_messaging_plugin.local_cli.cli import cli; cli()"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str, detail: str = "") -> bool:
    global _passed
    if condition:
        _passed += 1
        return True
    _failed.append(f"{label}: {detail}" if detail else label)
    print(f"  FAIL  {_failed[-1]}")
    return False


def _env(runtime: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A child environment carrying only what `watch` legitimately needs.

    Built explicitly rather than inherited: this suite runs inside a session
    the fleet launcher already armed, so an inherited AGENT_SESSION_ID would
    point every case at the REAL watcher's lock and spool.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(_SRC),
        "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_RUNTIME_DIR": str(runtime),
        "SOLET_NAME": _SOLET,
        "AGENT_SESSION_ID": _SESSION_ID,
        "AGENT_SESSION_LABEL": _ROLE,
    }
    env.update(extra or {})
    return env


def _spawn(runtime: Path, args: list[str] | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _LAUNCH, "watch", *(args or [])],
        env=_env(runtime),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _lock_path(runtime: Path) -> Path:
    """The lock the CHILD will use — resolved through the production helper.

    `watch_singleton_lock_path` reads `XDG_RUNTIME_DIR` from the *calling*
    process, so it is pointed at the child's runtime dir for the duration of
    the call. Restating the path here instead would make the test agree with
    itself while drifting from the code, which is the failure this suite is
    supposed to catch elsewhere.
    """
    instance = f"{WATCH_AGENT_INSTANCE_PREFIX}{watch_instance_digest(_SESSION_ID)}"
    previous = os.environ.get("XDG_RUNTIME_DIR")
    os.environ["XDG_RUNTIME_DIR"] = str(runtime)
    try:
        return watch_singleton_lock_path(_SOLET, instance)
    finally:
        if previous is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = previous


def _wait_for_lock(runtime: Path, timeout: float = 20.0) -> bool:
    """Wait until the incumbent has actually taken the lock (not merely spawned)."""
    lock = _lock_path(runtime)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lock.is_file() and lock.read_text().strip():
            return True
        time.sleep(0.2)
    return False


def case_singleton_refuses_second_arm(runtime: Path) -> None:
    """W1: the second watcher REFUSES; it must not evict the incumbent."""
    first = _spawn(runtime)
    try:
        if not _check(_wait_for_lock(runtime), "incumbent writes its pid into the lock"):
            return
        holder = _lock_path(runtime).read_text().strip()
        _check(holder == str(first.pid), "lock names the incumbent's pid", f"got {holder!r}")

        second = _spawn(runtime)
        try:
            _, stderr = second.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            second.kill()
            _check(False, "second arm exits promptly", "it blocked instead of refusing")
            return
        _check(second.returncode != 0, "second arm exits non-zero", f"rc={second.returncode}")
        _check(str(_lock_path(runtime)) in stderr, "refusal names the lock path", f"got {stderr[:200]!r}")
        _check(holder in stderr, "refusal names the holder pid", f"got {stderr[:200]!r}")

        # The incumbent must be UNDISTURBED — refuse, never evict.
        _check(first.poll() is None, "incumbent still running after the refusal")
    finally:
        first.terminate()
        first.wait(timeout=30)


def case_lock_releases_on_exit(runtime: Path) -> None:
    """Negative control: a SEQUENTIAL re-arm after a clean exit must succeed.

    Without this, `case_singleton_refuses_second_arm` would pass just as well
    against a hook that refused unconditionally — the singleton would be a
    permanent lockout rather than a live-holder check.
    """
    first = _spawn(runtime)
    _check(_wait_for_lock(runtime), "first run takes the lock")
    first.terminate()
    first.wait(timeout=30)

    second = _spawn(runtime)
    try:
        time.sleep(4.0)
        _check(second.poll() is None, "a later run acquires the released lock")
    finally:
        second.terminate()
        second.wait(timeout=30)


def case_sigterm_unwinds(runtime: Path) -> None:
    """W2: SIGTERM must UNWIND (handler -> SystemExit), not terminate in place.

    Exit 143 proves the handler ran and every `finally` — including the bridge
    context manager's close() -> /close -> unregister — got its chance. A
    returncode of -15 would mean the default disposition killed the process and
    the registry row was left standing, which is the §34.1 defect itself.
    """
    proc = _spawn(runtime)
    try:
        _check(_wait_for_lock(runtime), "watcher armed before SIGTERM")
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            _check(False, "SIGTERM terminates the watcher", "it ignored SIGTERM")
            return
        _check(
            proc.returncode == _SIGTERM_HANDLED_EXIT,
            f"SIGTERM unwinds via the handler (exit {_SIGTERM_HANDLED_EXIT})",
            f"got {proc.returncode} "
            f"({'DEFAULT disposition — handler missing' if proc.returncode == _SIGTERM_DEFAULT_RC else 'unexpected'})",
        )
    finally:
        if proc.poll() is None:
            proc.kill()


def case_parent_probe_semantics() -> None:
    """`--exit-with-parent` probe: only a genuinely absent pid counts as gone.

    PermissionError means the pid EXISTS under another user — emphatically not
    "gone" — and treating it as gone would make a watcher exit whenever it
    could not signal its parent.
    """
    _check(_parent_is_gone(None) is False, "no flag given -> never 'gone' (inert by default)")
    _check(_parent_is_gone(os.getpid()) is False, "own live pid -> not gone")
    _check(_parent_is_gone(1) is False, "pid 1 (exists, unsignalable) -> not gone")

    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait(timeout=30)
    time.sleep(0.5)
    _check(_parent_is_gone(victim.pid) is True, "reaped pid -> gone")


def case_exit_with_parent_ends_the_watcher(runtime: Path) -> None:
    """End-to-end: the watcher exits 0 once the named parent is gone."""
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    proc = _spawn(runtime, ["--exit-with-parent", str(parent.pid)])
    try:
        _check(_wait_for_lock(runtime), "watcher armed with --exit-with-parent")
        parent.wait(timeout=30)
        try:
            proc.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            proc.kill()
            _check(False, "watcher exits after its parent", "it kept running")
            return
        _check(proc.returncode == 0, "watcher exits 0 when the parent is gone", f"rc={proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.kill()
        if parent.poll() is None:
            parent.kill()


def main() -> int:
    print("agent_messaging — watcher lifecycle (W1 singleton, W2 SIGTERM, exit-with-parent)")
    print("=" * 72)
    case_parent_probe_semantics()
    for case in (
        case_singleton_refuses_second_arm,
        case_lock_releases_on_exit,
        case_sigterm_unwinds,
        case_exit_with_parent_ends_the_watcher,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            case(Path(tmp))
    print("-" * 72)
    if _failed:
        print(f"{_passed} passed, {len(_failed)} FAILED")
        return 1
    print(f"{_passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
