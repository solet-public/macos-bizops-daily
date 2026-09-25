#!/usr/bin/env python3
"""Run the gate-eligible smoke suite listed in ``gate_smokes.txt``.

Project policy is no pytest: smokes are standalone scripts with an
``if __name__ == "__main__"`` entry point that exit 0 on pass, the dedicated
SKIP code on a disclosed, non-blocking dependency gap (see ``_SKIP_EXIT_CODE``
below), and any other non-zero on genuine fail. This runner executes each
smoke named in the tracked register ``quality_gates/gate_smokes.txt`` (one
repo-relative path per line; ``#`` comments and blank lines ignored), with a
per-smoke timeout, and aggregates the verdicts. It is the behavioral half of
the commit gate — the static gates live in ``code_quality_check.py``; this
runs the smokes.

The register is a tracked, growing allowlist (it mirrors the god-class / radon
allowlists): a smoke gates only once it is listed here, so adding a smoke to the
gate is an explicit, reviewable act and the gate never silently broadens to
smokes that need a live platform — with one ratified exception class (GTE-09,
Coordinator-Day Q2): a smoke whose load-bearing proof genuinely requires a live
external dependency may register if it fails LOUD on an unreachable dependency
(see quality_gates/gate_smokes.txt's header for the full statement).

Skip visibility (2026-08-08, undeclared-dependency audit follow-on:
workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md):
before this, a smoke that self-skipped on a missing OPTIONAL tool (printing
"SKIP" and returning normally, exit 0) was indistinguishable from a smoke
that genuinely ran and passed — this runner determined pass/fail purely from
the process exit code and never surfaced a passing smoke's own stdout, so
the skip text was captured and silently discarded. The aggregate "N/M
passed" figure was therefore an upper bound on real coverage, not a
statement of it. A smoke now signals a disclosed, non-blocking skip by
exiting with ``_SKIP_EXIT_CODE`` (77) instead of 0 — the reserved SKIP exit
code from the automake/Meson/CTest test-tooling convention, chosen
deliberately for that portability precedent rather than invented fresh, and
verified against every currently gate-registered smoke's own exit codes to
carry zero collision risk (none use anything but 0/1 today). REPORTING a
skip and treating it as FATAL are separate concerns: this runner always
reports passed/skipped/failed counts distinctly; whether a skip should fail
the *suite* is caller policy via ``--fail-on-skip`` (default: tolerant — the
normal commit gate accepts a disclosed skip; a future hermeticity/seal check
against a born clone, where a skip IS the hollow-gate condition, passes
``--fail-on-skip`` to make it fatal there without this runner needing two
copies of itself).

Concurrency (2026-08-30, lane-fable-parallelize-run-smokes): smokes run in a
bounded thread pool (``--jobs``, default ``_DEFAULT_JOBS``); each worker thread
only spawns and waits on one smoke subprocess, so threads are the right tool —
the parallelism is between child processes, not Python bytecode. Results are
PRINTED in register order regardless of completion order, so per-smoke
attribution and the output format are unchanged from the serial runner; a
smoke's stdout/stderr is still captured and reported individually. The one
measured hazard of intra-battery concurrency is smokes holding host-exclusive
resources (fixed TCP ports, the host tmux server, the shared venv itself) —
the same contention class already filed cross-lane as GTE-16/GTE-17 in the
workbench backlog. Two escapes exist, each naming the specific resource
it isolates: smokes that contend only with EACH OTHER share an
``_EXCLUSIVE_GROUPS`` chain (sequential on one worker, concurrent with the
rest), and smokes whose resource the whole suite shares are named in the
tracked schedule ``quality_gates/serial_only_smokes.txt`` (the ONE serial-only
source, shared with the born-clone publication gate since 2026-09-20,
iss_f939bae7) and run one at a time BEFORE the pool starts, while no other
smoke is running at all; their results are cached and printed at their true
register slots during the ordered walk. Since 2026-09-04 (iss_0fb8f519), the
serial-only phase additionally holds a per-user host-global advisory flock,
so separate batteries cannot overlap those host-exclusive smokes. The pooled
phase never takes that lock. The ``--serial-only-override`` option exists only
as a focused test/diagnostic seam; it replaces that named set but preserves the
same host-global lock. The suite's result set is identical to a fully serial
run either way. ``--jobs 1`` degenerates to one worker (plus the same
host-serialized serial-only phase) for diagnosis of suspected
concurrency-induced flakes.

Contention signal (2026-09-20, gates-cluster-A5): a battery that ran while
sharing the host with real fleet load can red or skip for reasons that have
nothing to do with the candidate under test — see ``contention_signal.py``'s
module docstring for the five issues this addresses. Every run now measures
and prints ``contention_signal.BatteryContention`` (concurrent live batteries,
liveness-verified, sampled before and after the suite; and how long this
battery waited for the host serial-only lock), and a full-battery run records
it on its own receipt (``contention`` key) so a later reader can tell "this
red co-occurred with real host contention" from "this is a clean regression"
without re-running anything.

Exit codes:
  0 - every listed smoke passed (skips, if any, did not trip --fail-on-skip)
  1 - one or more smokes failed, timed out, is a missing path, or (only with
      --fail-on-skip) one or more smokes skipped

Usage:
  .venv/bin/python3 quality_gates/run_smokes.py [--register PATH] [--timeout S] [--list] [--fail-on-skip] [--jobs N] [--campaign NAME] [--reauthorize] [--serial-only-override PATH]
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import IO, Literal

# This file is dual-invoked: directly as a script (sys.path[0] is this
# directory) and imported as `quality_gates.run_smokes` by
# run_smokes_battery_containment_smoke.py (sys.path[0] is the repo root).
# Inserting this file's own directory makes the flat sibling import below
# resolve identically in both cases, matching code_quality_check.py's flat
# `from gate_scope import ...` sibling-import style.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import contention_signal  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Overridable so an out-of-process regression fixture (contention_signal_smoke.py)
# can make a small, disposable fixture register "the" full battery for receipt
# purposes without touching the real gate_smokes.txt or the real candidate
# tree -- the same isolation _HOST_SERIAL_LOCK_ENV and _BATTERY_RECEIPT_DIR_ENV
# already give a subprocess-level test below.
_DEFAULT_REGISTER_ENV = "RUN_SMOKES_DEFAULT_REGISTER_PATH"
_DEFAULT_REGISTER = Path(
    os.environ.get(_DEFAULT_REGISTER_ENV, str(Path(__file__).resolve().parent / "gate_smokes.txt"))
)
_DEFAULT_SERIAL_ONLY = Path(__file__).resolve().parent / "serial_only_smokes.txt"
_DEFAULT_TIMEOUT = 120
_HOST_SERIAL_LOCK_ENV = "RUN_SMOKES_HOST_SERIAL_LOCK_PATH"
_HOST_SERIAL_LOCK_PATH = Path(
    os.environ.get(
        _HOST_SERIAL_LOCK_ENV,
        str(Path.home() / ".ananta" / "locks" / "run_smokes_serial_only.lock"),
    )
)
_HOST_LOCK_WAIT_REPORT_SECONDS = 30.0
_BATTERY_RECEIPT_DIR_ENV = "RUN_SMOKES_BATTERY_RECEIPT_DIR"
_BATTERY_RECEIPT_DIR = Path.home() / ".ananta" / "runtime" / "gate_battery_receipts"
_DEFAULT_CAMPAIGN = "default"

# The normal worker pool starts at most twelve children simultaneously.  A real
# full-battery calibration showed its 25th legitimate start can occur inside
# the initial one-second window, so the cap permits ten ordinary pool turnovers
# while still stopping a fork churn above 120 top-level starts per second. This
# is a fuse, not a throttle: the first attempted start past the cap aborts the
# whole battery before it can fork that child.
_MAX_SMOKE_STARTS_PER_WINDOW = 120
_SMOKE_START_WINDOW_SECONDS = 1.0

# The smoke runner is a boundary between the driving shell and each smoke.  A
# copied-and-scrubbed environment is not a boundary: it only excludes the
# variables whose leaks have already been discovered.  These are the caller
# values the registered smoke population deliberately consumes.  PATH is
# constructed below rather than inherited, so a caller cannot select a tool
# binary for a smoke by prepending its own directories.
_CHILD_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "APP_HOME",
    "SOLET_NAME",
    "SOLET_HOME",
    "SOLET_WORKSPACE_ROOT",
    "XDG_RUNTIME_DIR",
    "HOME",
)
# These are not caller configuration.  The runner-under-test receives them
# from ``run_smokes_host_lock_smoke.py`` and must carry them to its child: that
# child is the smoke's stub fixture.  Dropping the mode makes the fixture take
# its test path and recursively launch another runner.  Keep this exact list;
# accepting a RUN_SMOKES_* prefix would recreate the subtractive-filter error
# in reverse.
_RUNNER_CONTROL_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "RUN_SMOKES_HOST_LOCK_STUB_MODE",
    "RUN_SMOKES_HOST_LOCK_STUB_EVENTS",
    "RUN_SMOKES_HOST_LOCK_STUB_LABEL",
    "RUN_SMOKES_HOST_LOCK_STUB_SLEEP",
    "RUN_SMOKES_HOST_SERIAL_LOCK_PATH",
)
_NESTED_SELF_TEST_ENV = "RUN_SMOKES_NESTED_SELF_TEST"
_SYSTEM_PATH_DIRECTORIES: tuple[str, ...] = (
    # Homebrew's Apple Silicon prefix is machine-global like /usr/local, not
    # a caller-provided path segment.  Smokes use node, rg, brew, and tmux
    # from here; omitting it silently turns real checks into no-op passes or
    # disclosed skips on Apple Silicon hosts.
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

# The automake/Meson/CTest SKIP_RETURN_CODE convention — a smoke exits this
# code to report a disclosed, non-blocking dependency gap. Chosen for that
# portability precedent, not invented fresh. See the module docstring's
# "Skip visibility" section for the full rationale and the collision check.
_SKIP_EXIT_CODE = 77

# Worker-pool width. Smokes are subprocess-bound (the pool threads just wait on
# children), so this is really "how many smoke processes share the host at
# once"; the cap keeps concurrent live-DB smokes comfortably inside Postgres's
# connection budget and avoids CPU oversubscription from the handful of
# venv-building smokes.
_DEFAULT_JOBS = min(12, os.cpu_count() or 1)

# Smokes that hold a HOST-EXCLUSIVE resource for their whole run and therefore
# cannot share the host with ANY concurrently-running smoke are listed, with
# the specific resource each one measures, in serial_only_smokes.txt beside the
# register (see _read_serial_only). They are excluded from the worker pool and
# run one at a time before it starts; this serial phase is also host-serialized
# across separate batteries by _HOST_SERIAL_LOCK_PATH (iss_0fb8f519), while the
# pooled phase remains cross-process concurrent.

# Smokes that conflict ONLY WITH EACH OTHER: every smoke in a group contends
# for the same fixed resource that no smoke outside the group touches, so the
# whole group is chained onto a single pool worker (sequential within the
# group, concurrent with everything else). Cheaper than the serial-only
# schedule, which is reserved for resources the entire suite shares.
_EXCLUSIVE_GROUPS: tuple[tuple[str, ...], ...] = (
    # The swap family shares two fixed resources no smoke outside it touches:
    # TCP ports 50001/50002 (_BLUE_TEST_PORT/_GREEN_TEST_PORT — bound by
    # swap_round_trip_smoke, and again by its scratch-cleanup pair, which
    # re-runs it as a child process; GTE-16), and the pending-finisher record
    # for the fixture solet name "smoke" at the REAL
    # ~/.ananta/runtime/smoke.pending_finisher.json — swap_round_trip and
    # cutover_failure write it through production machinery and
    # rollback_release unlinks it in cleanup (measured: cutover_failure lost
    # its .tmp mid-os.replace when pooled alongside rollback_release). The
    # other solet_name="smoke" deployment smokes drive recording-stub routers
    # and never reach the runtime dir; complete_swap_crash_consistency uses
    # per-scenario-unique solet names.
    (
        "plugins/macos_self_deployment_plugin/tests/swap_round_trip_smoke.py",
        "plugins/macos_self_deployment_plugin/tests/swap_round_trip_scratch_cleanup_smoke.py",
        "plugins/macos_self_deployment_plugin/tests/cutover_failure_smoke.py",
        "plugins/macos_self_deployment_plugin/tests/rollback_release_smoke.py",
    ),
    # The WS5 Step-7 real-style smokes each drive the Solet Manager end to end
    # over a real ~4,000-file seed clone (real git, real genesis writers) many
    # times per process: a cold-host matrix row, a tree row, or a crash/resume
    # boundary is 5-15s of CPU-bound subprocess work, and a smoke holds 5-16 of
    # them. They are registered as one contiguous block, so a pool runs every
    # one of them at once: measured 2026-09-19, 12-wide with all 22 pooled,
    # every one TIMEOUT at 120s (140-300s each); 3-wide, 25-100s; alone,
    # 20-85s. The contended resource is this host's CPU and temp-volume
    # bandwidth, which the whole battery shares, so this is not a fixed
    # resource in the sense of the group above -- it is used here as the only
    # mechanism the runner has for bounding heavy-smoke concurrency. Three
    # chains for Step 7 (the matrix + tree + Homebrew rows, and the crash sweep
    # in two halves) and two for the Step-6 sweeps registered just before them
    # (the same class of work on a smaller fixture), so at most five of them
    # run concurrently with the light pool. The Step-6 sweep files hold at most
    # five crash points each (about 33s typical, 45s worst standalone): the old
    # 10-12-point slices ran 57-70s alone, and a transient external host-load
    # spike pushed two of them past the 120s cap (iss_d1a9649b).
    (
        "solet_cli/tests/update_crash_sweep_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_1_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_2_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_3_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_4_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_5_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_6_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_7_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_8_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_9_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_10_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_11_smoke.py",
        "solet_cli/tests/update_crash_sweep_writes_12_smoke.py",
        "solet_cli/tests/update_crash_sweep_applies_smoke.py",
    ),
    (
        "solet_cli/tests/existing_install_router_failure_sweep_1_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_2_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_3_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_4_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_5_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_6_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_7_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_8_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_9_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_10_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_applies_1_smoke.py",
        "solet_cli/tests/existing_install_router_failure_sweep_applies_2_smoke.py",
    ),
    (
        "solet_cli/tests/cold_host_matrix_smoke.py",
        "solet_cli/tests/cold_host_matrix_2_smoke.py",
        "solet_cli/tests/cold_host_matrix_3_smoke.py",
        "solet_cli/tests/cold_host_matrix_4_smoke.py",
        "solet_cli/tests/real_style_tree_smoke.py",
        "solet_cli/tests/real_style_tree_2_smoke.py",
        "solet_cli/tests/real_style_tree_3_smoke.py",
        "solet_cli/tests/real_style_tree_4_smoke.py",
        "solet_cli/tests/real_style_tree_5_smoke.py",
        "solet_cli/tests/real_style_tree_6_smoke.py",
        "solet_cli/tests/homebrew_import_lifecycle_smoke.py",
        "solet_cli/tests/homebrew_upgrade_persistence_smoke.py",
        "solet_cli/tests/homebrew_real_style_smoke.py",
    ),
    (
        "solet_cli/tests/real_style_sweep_writes_1_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_2_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_3_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_4_smoke.py",
        "solet_cli/tests/real_style_sweep_applies_smoke.py",
    ),
    (
        "solet_cli/tests/real_style_sweep_writes_5_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_6_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_7_smoke.py",
        "solet_cli/tests/real_style_sweep_writes_8_smoke.py",
    ),
)

_Verdict = Literal["passed", "skipped", "failed"]


class BatteryReceiptError(RuntimeError):
    """A full-battery receipt makes this invocation unsafe to start."""


class SmokeStartRateError(RuntimeError):
    """The battery attempted to fork faster than its fixed safety fuse permits."""


def _utc_timestamp() -> str:
    """Return an unambiguous receipt timestamp."""
    return dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")


def _candidate_path_exists(path: Path) -> bool:
    """Keep existing files and links; omit an unstaged deletion."""
    return path.exists() or path.is_symlink()


def _indexed_candidate_paths(root: Path, output: bytes) -> tuple[Path, ...]:
    """Resolve existing index entries, excluding an unstaged deletion."""
    return tuple(
        path
        for item in output.split(b"\0")
        if item
        for path in (root / item.decode("utf-8"),)
        if _candidate_path_exists(path)
    )


def _candidate_paths_for_digest(root: Path) -> tuple[Path, ...]:
    """Return the source paths that define the candidate being executed.

    A materialized candidate with a private Git directory has an index whose
    paths already describe its frozen tree, including its reviewed overlay.
    A working-tree deletion remains in that index until staging, but cannot be
    candidate source; omit it while retaining symlinks for their own byte/link
    validation below.
    A focused fixture or an intentionally Git-free candidate has no such
    index, so it falls back to a complete recursive census.  Generated venv
    and cache artifacts are excluded in both cases: they are runner byproducts,
    not candidate source bytes.
    """
    listed = subprocess.run(
        ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if listed.returncode == 0:
        indexed_paths = _indexed_candidate_paths(root, listed.stdout)
        if indexed_paths:
            return tuple(sorted(indexed_paths))

    ignored_directories = frozenset({".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache"})
    paths: list[Path] = []
    for directory, names, filenames in os.walk(root):
        names[:] = sorted(name for name in names if name not in ignored_directories)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix in {".pyc", ".pyo"}:
                continue
            paths.append(path)
    return tuple(sorted(paths))


def _candidate_digest(root: Path) -> str:
    """Hash the executable candidate's paths, modes, link targets, and bytes."""
    digest = hashlib.sha256()
    for path in _candidate_paths_for_digest(root):
        try:
            relative = path.relative_to(root).as_posix()
            stat_result = path.lstat()
        except OSError as exc:
            raise BatteryReceiptError(f"candidate digest cannot read {path}: {exc}") from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(oct(stat_result.st_mode).encode("ascii"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode("utf-8"))
            digest.update(b"\0")
            continue
        if not path.is_file():
            raise BatteryReceiptError(f"candidate digest found non-file path: {relative}")
        digest.update(b"file\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise BatteryReceiptError(f"candidate digest cannot read {relative}: {exc}") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def _receipt_directory() -> Path:
    """Return the host-external receipt location, creating it on demand."""
    configured = os.environ.get(_BATTERY_RECEIPT_DIR_ENV)
    directory = Path(configured) if configured else _BATTERY_RECEIPT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _receipt_path(directory: Path, candidate_digest: str, campaign: str) -> Path:
    campaign_digest = hashlib.sha256(campaign.encode("utf-8")).hexdigest()[:16]
    return directory / f"{candidate_digest}.{campaign_digest}.json"


def _read_receipt(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatteryReceiptError(f"battery receipt is unreadable: {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise BatteryReceiptError(f"battery receipt is not an object: {path}")
    return loaded


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    """Atomically publish one receipt state so crashes never leave partial JSON."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class _BatteryReceipt:
    """A held per-candidate lock plus the receipt it protects for one battery."""

    def __init__(self, path: Path, lock: IO[str], receipt: dict[str, object]) -> None:
        self.path = path
        self._lock = lock
        self._receipt = receipt

    def record_contention(self, contention: dict[str, object]) -> None:
        """Attach this battery's contention snapshot; written out by ``finish``."""
        self._receipt["contention"] = contention

    def finish(self, result: str) -> None:
        self._receipt["finished_at"] = _utc_timestamp()
        self._receipt["result"] = result
        _write_receipt(self.path, self._receipt)

    def close(self) -> None:
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()


def _prior_receipt_detail(receipt: dict[str, object]) -> str:
    started = receipt.get("started_at", "unknown timestamp")
    result = receipt.get("result", "unknown result")
    return f"at {started}; prior result={result}"


def _acquire_battery_receipt(
    candidate_digest: str, campaign: str, *, reauthorize: bool
) -> _BatteryReceipt:
    """Claim the only full battery permitted for this candidate/campaign."""
    directory = _receipt_directory()
    path = _receipt_path(directory, candidate_digest, campaign)
    lock = path.with_suffix(".lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        existing = _read_receipt(path)
        lock.close()
        detail = _prior_receipt_detail(existing) if existing else "with no readable receipt"
        raise BatteryReceiptError(
            "full battery refused: the same candidate is already running "
            f"in campaign {campaign!r} {detail}"
        ) from exc

    existing = _read_receipt(path)
    if existing is not None and not reauthorize:
        lock.close()
        raise BatteryReceiptError(
            "full battery refused: candidate digest "
            f"{candidate_digest} already ran in campaign {campaign!r} "
            f"{_prior_receipt_detail(existing)}; pass --reauthorize to run it again"
        )

    receipt: dict[str, object] = {
        "schema_version": 1,
        "candidate_digest": candidate_digest,
        "campaign": campaign,
        "started_at": _utc_timestamp(),
        "result": "running",
        # Recorded so a sibling battery's contention count (contention_signal
        # .count_concurrent_live_batteries) can tell a genuinely live battery
        # apart from one whose receipt says "running" only because it was
        # hard-killed before reaching `finally: _close_receipt`.
        "pid": os.getpid(),
    }
    if existing is not None:
        receipt["reauthorized_from"] = {
            "started_at": existing.get("started_at"),
            "finished_at": existing.get("finished_at"),
            "result": existing.get("result"),
        }
    _write_receipt(path, receipt)
    return _BatteryReceipt(path, lock, receipt)


def _terminate_process_group(process_group: int) -> None:
    """Terminate a smoke session, escalating only if its descendants linger."""
    try:
        os.killpg(process_group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.02)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


class _SmokeProcessSupervisor:
    """Own smoke process sessions and fail closed on unsafe fork rate."""

    def __init__(self, maximum_starts: int, window_seconds: float) -> None:
        self._maximum_starts = maximum_starts
        self._window_seconds = window_seconds
        self._starts: deque[float] = deque()
        self._active: dict[int, subprocess.Popen[str]] = {}
        self._started_groups: set[int] = set()
        self._aborted_reason: str | None = None
        self._lock = threading.Lock()

    def reserve_start(self) -> None:
        with self._lock:
            if self._aborted_reason is not None:
                raise SmokeStartRateError(self._aborted_reason)
            now = time.monotonic()
            while self._starts and now - self._starts[0] >= self._window_seconds:
                self._starts.popleft()
            if len(self._starts) >= self._maximum_starts:
                reason = (
                    "start-rate fuse tripped: attempted more than "
                    f"{self._maximum_starts} smoke starts in "
                    f"{self._window_seconds:.1f}s"
                )
                self._aborted_reason = reason
                groups = tuple(self._started_groups)
            else:
                self._starts.append(now)
                return
        for process_group in groups:
            _terminate_process_group(process_group)
        raise SmokeStartRateError(reason)

    def register(self, process: subprocess.Popen[str]) -> bool:
        """Track one new session; return false if a concurrent fuse tripped."""
        with self._lock:
            self._started_groups.add(process.pid)
            if self._aborted_reason is None:
                self._active[process.pid] = process
                return True
            reason = self._aborted_reason
        _terminate_process_group(process.pid)
        raise SmokeStartRateError(reason)

    def unregister(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._active.pop(process.pid, None)

    def terminate(self, process: subprocess.Popen[str]) -> None:
        _terminate_process_group(process.pid)


def _venv_python() -> Path:
    """Return the repo venv interpreter, failing loud if it is absent."""
    candidate = _REPO_ROOT / ".venv" / "bin" / "python3"
    if not candidate.exists():
        raise FileNotFoundError(f"repo venv interpreter not found: {candidate}")
    return candidate


def _require_solet_name() -> str:
    """Fail closed ONCE, here, when ``SOLET_NAME`` is unset.

    Many smokes resolve the platform identity at import time and fail closed
    without it, so an unset variable turns a suite run into a wall of unrelated
    tracebacks whose shared cause is invisible -- the failure signature reads as
    a broken platform rather than a missing export. Checking at the entry point
    turns that into one discoverable message before any smoke is spawned.

    The wording is bootstrap.py's, deliberately: the platform already states
    this requirement there, and a second phrasing of the same requirement is a
    divergence waiting to rot. Presence only -- name VALIDATION belongs at
    genesis (bootstrap.py), and duplicating the pattern here would be the same
    divergence in another form.
    """
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "SOLET_NAME env var is required -- it is this solet's "
            "database name (database per solet, named after it). The "
            "driving agent must export it before running the smoke suite: "
            "SOLET_NAME=<name> .venv/bin/python3 quality_gates/run_smokes.py"
        )
    return name


def _read_register(register: Path) -> list[str]:
    """Parse and validate the register's unique, existing smoke paths."""
    if not register.exists():
        raise FileNotFoundError(f"gate-smoke register not found: {register}")
    entries: list[str] = []
    first_lines: dict[str, int] = {}
    errors: list[str] = []
    for number, raw in enumerate(register.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line in first_lines:
            errors.append(
                f"{register}:{number}: duplicate smoke path {line!r} "
                f"(first registered at line {first_lines[line]})"
            )
            continue
        first_lines[line] = number
        if not (_REPO_ROOT / line).is_file():
            errors.append(f"{register}:{number}: registered smoke path does not exist: {line!r}")
            continue
        entries.append(line)
    if errors:
        raise ValueError("invalid gate-smoke register:\n" + "\n".join(errors))
    return entries


def _read_serial_only(schedule: Path) -> frozenset[str]:
    """Parse the serial-only schedule: register grammar, no duplicates.

    Entries are not checked against the filesystem or the register here — a
    focused ``--register`` run legitimately names none of them, and an entry
    absent from the register being run is simply inert. The parity smoke
    (``serial_only_schedule_parity_smoke.py``) holds every entry to the
    canonical register, so a renamed smoke cannot silently fall out of the
    serial phase.
    """
    if not schedule.exists():
        raise FileNotFoundError(f"serial-only smoke schedule not found: {schedule}")
    entries: dict[str, int] = {}
    errors: list[str] = []
    for number, raw in enumerate(schedule.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line in entries:
            errors.append(
                f"{schedule}:{number}: duplicate serial-only smoke path {line!r} "
                f"(first listed at line {entries[line]})"
            )
            continue
        entries[line] = number
    if errors:
        raise ValueError("invalid serial-only smoke schedule:\n" + "\n".join(errors))
    return frozenset(entries)


def _child_environment(python: Path) -> dict[str, str]:
    """Build the deliberate environment for one smoke subprocess.

    The six pass-through keys are the external configuration read by the
    registered smoke population: platform/profile identity and runtime-home
    locations.  ``GIT_CONTROLLER_NAME`` is deliberately absent even though
    gate smoke code names it: its fixtures set it themselves, while inheriting
    a caller value could arm a gate case that is meant to be unarmed.
    """
    environment = {
        "PATH": os.pathsep.join((str(python.parent), *_SYSTEM_PATH_DIRECTORIES)),
    }
    for key in _CHILD_ENVIRONMENT_KEYS:
        value = os.environ.get(key)
        if value is not None:
            environment[key] = value
    if os.environ.get(_NESTED_SELF_TEST_ENV) == "1":
        for key in (_NESTED_SELF_TEST_ENV, *_RUNNER_CONTROL_ENVIRONMENT_KEYS):
            value = os.environ.get(key)
            if value is not None:
                environment[key] = value
    return environment


def _run_one(
    python: Path,
    smoke: Path,
    timeout: int,
    supervisor: _SmokeProcessSupervisor | None = None,
) -> tuple[_Verdict, str]:
    """Run a single smoke; return (verdict, captured_output).

    Verdict is "skipped" ONLY on the dedicated ``_SKIP_EXIT_CODE`` — every
    other non-zero code (including a timeout) is "failed", never silently
    folded into "skipped". A smoke choosing to exit 77 for reasons other than
    a genuine disclosed gap is a smoke misusing the convention, not something
    this runner can or should second-guess.
    """
    active_supervisor = supervisor or _SmokeProcessSupervisor(
        _MAX_SMOKE_STARTS_PER_WINDOW, _SMOKE_START_WINDOW_SECONDS
    )
    proc = _start_smoke(python, smoke, active_supervisor)
    return _wait_for_smoke(proc, timeout, active_supervisor)


def _start_smoke(
    python: Path, smoke: Path, supervisor: _SmokeProcessSupervisor
) -> subprocess.Popen[str]:
    """Start one smoke in a session owned by the supervisor."""
    supervisor.reserve_start()
    proc = subprocess.Popen(
        [str(python), str(smoke)],
        cwd=_REPO_ROOT,
        env=_child_environment(python),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    supervisor.register(proc)
    return proc


def _wait_for_smoke(
    proc: subprocess.Popen[str], timeout: int, supervisor: _SmokeProcessSupervisor
) -> tuple[_Verdict, str]:
    """Collect one smoke result, killing its complete session on timeout."""
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        supervisor.terminate(proc)
        proc.communicate()
        return "failed", f"TIMEOUT after {timeout}s"
    finally:
        supervisor.unregister(proc)
    output = (stdout or "") + (stderr or "")
    if proc.returncode == 0:
        return "passed", output
    if proc.returncode == _SKIP_EXIT_CODE:
        return "skipped", output
    return "failed", output


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the gate-eligible smoke suite.")
    parser.add_argument("--register", type=Path, default=_DEFAULT_REGISTER)
    parser.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--list", action="store_true", help="List the register and exit.")
    parser.add_argument(
        "--campaign",
        default=_DEFAULT_CAMPAIGN,
        help=(
            "Receipt campaign for this full battery (default: %(default)s). "
            "A candidate may run once per campaign unless --reauthorize is explicit."
        ),
    )
    parser.add_argument(
        "--reauthorize",
        action="store_true",
        help="Explicitly replace this candidate/campaign's terminal battery receipt.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=_DEFAULT_JOBS,
        help=(
            "Worker-pool width for pool-eligible smokes (default: "
            f"{_DEFAULT_JOBS} on this host). The serial-only holders of "
            "host-exclusive resources always run one at a time before the "
            "pool starts, whatever this is set to; 1 runs everything "
            "serially."
        ),
    )
    parser.add_argument(
        "--fail-on-skip",
        action="store_true",
        help=(
            "Treat any skipped smoke as a suite failure. Off by default (the "
            "commit gate tolerates a disclosed skip); a hermeticity/seal check "
            "against a born clone should pass this, since a skip there IS the "
            "hollow-gate condition being checked for."
        ),
    )
    parser.add_argument(
        "--serial-only-override",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "Replace the serial-only schedule for focused test/diagnostic runs; repeated "
            "once per repo-relative smoke path. The replacement still takes "
            "the host-global serial-phase lock."
        ),
    )
    return parser.parse_args()


def _run_timed(
    python: Path,
    entry: str,
    timeout: int,
    supervisor: _SmokeProcessSupervisor,
) -> tuple[_Verdict, str, float]:
    """Run one smoke; return ``(verdict, output, elapsed_seconds)``."""
    start = time.monotonic()
    try:
        verdict, output = _run_one(python, _REPO_ROOT / entry, timeout, supervisor)
    except SmokeStartRateError as exc:
        verdict, output = "failed", f"START-RATE-FUSE: {exc}"
    return verdict, output, time.monotonic() - start


def _report_result(
    entry: str,
    verdict: _Verdict,
    output: str,
    elapsed: float,
    skipped: list[str],
    failures: list[str],
) -> None:
    """Print one smoke's verdict line (serial runner's exact format)."""
    label = {"passed": "ok", "skipped": "SKIP", "failed": "FAIL"}[verdict]
    print(f"  {label:7} {entry}  ({elapsed:.1f}s)")
    if verdict == "passed":
        return
    (skipped if verdict == "skipped" else failures).append(entry)
    tail = "\n    ".join(output.strip().splitlines()[-12:])
    print(f"    {tail}")


def _schedule_units(pooled: list[str]) -> list[tuple[str, ...]]:
    """Coalesce ``pooled`` into scheduling units: one smoke, or one
    ``_EXCLUSIVE_GROUPS`` chain run sequentially by whichever worker picks it
    up. Group members keep their register-order slot in the printed output
    either way."""
    grouped: dict[str, tuple[str, ...]] = {
        member: group for group in _EXCLUSIVE_GROUPS for member in group
    }
    units: list[tuple[str, ...]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for entry in pooled:
        group = grouped.get(entry)
        if group is None:
            units.append((entry,))
        elif group not in seen_groups:
            seen_groups.add(group)
            units.append(tuple(member for member in group if member in pooled))
    return units


def _acquire_host_serial_lock() -> tuple[IO[str], float]:
    """Acquire the host-wide advisory lock for one serial-only phase.

    Blocking flock alone would make a queued battery look hung. Retry with a
    non-blocking acquisition so the caller gets a durable stderr progress line
    every 30 seconds, while the kernel still releases the lock if its owner
    exits unexpectedly. Returns the wait duration alongside the handle: this
    is a direct, always-present measurement of contention for the resource
    the ``_SERIAL_ONLY`` smokes share (iss_c35292ca/358, iss_dfca317c/357) —
    see ``contention_signal``'s module docstring.
    """
    _HOST_SERIAL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = _HOST_SERIAL_LOCK_PATH.open("a+", encoding="utf-8")
    waited_since = time.monotonic()
    next_report = _HOST_LOCK_WAIT_REPORT_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} cwd={Path.cwd()}\n")
            handle.flush()
            return handle, time.monotonic() - waited_since
        except BlockingIOError:
            elapsed = time.monotonic() - waited_since
            if elapsed >= next_report:
                print(
                    f"Waiting {elapsed:.1f}s for host serial-only lock: "
                    f"{_HOST_SERIAL_LOCK_PATH}",
                    file=sys.stderr,
                    flush=True,
                )
                next_report += _HOST_LOCK_WAIT_REPORT_SECONDS
            time.sleep(0.1)


def _run_serial_phase(
    python: Path,
    serial_entries: list[str],
    timeout: int,
    supervisor: _SmokeProcessSupervisor,
) -> tuple[dict[str, tuple[_Verdict, str, float]], float]:
    """Run a nonempty serial phase while holding the per-user host lock.

    Returns the per-smoke results plus how long THIS battery waited to
    acquire the lock — 0.0 when there was nothing to serialize, so a register
    with no ``_SERIAL_ONLY`` member reports "did not contend for this
    resource" rather than a zero that looks identical to an instant
    acquisition.
    """
    if not serial_entries:
        return {}, 0.0
    handle, host_lock_wait_seconds = _acquire_host_serial_lock()
    try:
        results = {
            entry: _run_timed(python, entry, timeout, supervisor)
            for entry in serial_entries
        }
        return results, host_lock_wait_seconds
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _run_suite(
    python: Path,
    entries: list[str],
    timeout: int,
    jobs: int,
    serial_only: frozenset[str],
) -> tuple[list[str], list[str], list[str], float]:
    """Run every smoke in ``entries``; return ``(skipped, failures, missing, host_lock_wait_seconds)``.

    The ``serial_only`` holders of host-exclusive resources run FIRST, one at
    a time, before the pool exists — no other smoke is running during any of
    them — and their results are cached. That phase takes the per-user
    host-global lock; pool-eligible smokes then run without it on a bounded
    ``jobs``-wide thread pool. Printing walks the register in its own order —
    MISSING lines, cached serial results, and pooled results each appear at
    their true register slot, in the serial runner's exact per-smoke format —
    so neither completion order nor scheduling phase ever leaks into the output.
    ``host_lock_wait_seconds`` is this battery's own wait to acquire that lock
    (0.0 when the register has no ``_SERIAL_ONLY`` member) — half of the
    contention signal ``main()`` records on the battery receipt.
    """
    skipped: list[str] = []
    failures: list[str] = []
    missing: list[str] = []
    print(f"Running {len(entries)} gate-eligible smokes (timeout {timeout}s each)\n")
    present = [entry for entry in entries if (_REPO_ROOT / entry).exists()]
    present_set = set(present)
    pooled = [entry for entry in present if entry not in serial_only]
    serial_entries = [entry for entry in present if entry in serial_only]
    supervisor = _SmokeProcessSupervisor(
        _MAX_SMOKE_STARTS_PER_WINDOW, _SMOKE_START_WINDOW_SECONDS
    )
    serial_results, host_lock_wait_seconds = _run_serial_phase(
        python, serial_entries, timeout, supervisor
    )

    def _run_unit(unit: tuple[str, ...]) -> list[tuple[_Verdict, str, float]]:
        return [_run_timed(python, entry, timeout, supervisor) for entry in unit]

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures: dict[str, tuple[Future[list[tuple[_Verdict, str, float]]], int]] = {}
        for unit in _schedule_units(pooled):
            future = pool.submit(_run_unit, unit)
            for index, entry in enumerate(unit):
                futures[entry] = (future, index)
        _walk_register(
            entries, present_set, serial_results, futures, skipped, failures, missing,
        )
    return skipped, failures, missing, host_lock_wait_seconds


def _walk_register(
    entries: list[str],
    present_set: set[str],
    serial_results: dict[str, tuple[_Verdict, str, float]],
    futures: dict[str, tuple[Future[list[tuple[_Verdict, str, float]]], int]],
    skipped: list[str],
    failures: list[str],
    missing: list[str],
) -> None:
    """Print every register row at its true slot: MISSING lines, cached
    serial-only results, and pooled results (blocking on each pooled future
    in register order) all interleave exactly where the register puts them."""
    for entry in entries:
        if entry not in present_set:
            missing.append(entry)
            print(f"  MISSING  {entry}")
        elif entry in serial_results:
            _report_result(entry, *serial_results[entry], skipped, failures)
        else:
            future, index = futures[entry]
            _report_result(entry, *future.result()[index], skipped, failures)


def _summarize(
    total: int,
    skipped: list[str],
    failures: list[str],
    missing: list[str],
    *,
    fail_on_skip: bool,
) -> int:
    """Print the aggregate verdict; return the process exit code.

    Skips are ALWAYS reported distinctly from passes — never folded into the
    passed count. Whether they trip the exit code is the one policy knob
    (``fail_on_skip``); everything else about the reporting is unconditional.
    """
    passed = total - len(skipped) - len(failures) - len(missing)
    print(f"\nsmokes: {passed}/{total} passed, {len(skipped)}/{total} skipped")
    if skipped:
        print(f"skipped: {len(skipped)} ({', '.join(skipped)})")
    if missing:
        print(f"missing: {len(missing)} ({', '.join(missing)})")
    if failures:
        print(f"failed: {len(failures)} ({', '.join(failures)})")
    blocking_skips = fail_on_skip and bool(skipped)
    if blocking_skips:
        print("--fail-on-skip: skips above are treated as failures for this run.")
    return 1 if (failures or missing or blocking_skips) else 0


def _receipt_for_full_battery(args: argparse.Namespace) -> _BatteryReceipt | None:
    """Claim a receipt only for the canonical registered full battery."""
    if args.register.resolve() != _DEFAULT_REGISTER.resolve():
        return None
    return _acquire_battery_receipt(
        _candidate_digest(_REPO_ROOT), args.campaign, reauthorize=args.reauthorize
    )


def _finish_receipt(receipt: _BatteryReceipt | None, result: str) -> None:
    if receipt is not None:
        receipt.finish(result)


def _close_receipt(receipt: _BatteryReceipt | None) -> None:
    if receipt is not None:
        receipt.close()


def main() -> int:
    args = _parse_args()
    try:
        entries = _read_register(args.register)
        serial_only = (
            frozenset(args.serial_only_override)
            if args.serial_only_override is not None
            else _read_serial_only(_DEFAULT_SERIAL_ONLY)
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.list:
        print("\n".join(entries))
        return 0
    _require_solet_name()
    # A focused/custom register is not a full candidate battery.  In
    # particular, the host-lock regression starts tiny nested fixture runners;
    # allowing one of those to consume the receipt for the real registered
    # suite would make the next full battery refuse for the wrong reason.
    try:
        receipt = _receipt_for_full_battery(args)
    except BatteryReceiptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    receipt_dir = _receipt_directory()
    exclude_path = receipt.path if receipt is not None else None
    concurrent_at_start = contention_signal.count_concurrent_live_batteries(
        receipt_dir, exclude_path=exclude_path
    )
    try:
        skipped, failures, missing, host_lock_wait_seconds = _run_suite(
            _venv_python(), entries, args.timeout, args.jobs, serial_only
        )
        concurrent_at_finish = contention_signal.count_concurrent_live_batteries(
            receipt_dir, exclude_path=exclude_path
        )
        contention = contention_signal.measure(
            concurrent_live_batteries_start=concurrent_at_start,
            concurrent_live_batteries_finish=concurrent_at_finish,
            host_lock_wait_seconds=host_lock_wait_seconds,
        )
        print(contention.render())
        if receipt is not None:
            receipt.record_contention(contention.as_dict())
        result = _summarize(
            len(entries),
            skipped,
            failures,
            missing,
            fail_on_skip=args.fail_on_skip,
        )
    except BaseException:
        _finish_receipt(receipt, "interrupted")
        raise
    else:
        _finish_receipt(receipt, "passed" if result == 0 else "failed")
        return result
    finally:
        _close_receipt(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
