"""Out-of-process proof that Core AI's compiled cache is sound before in-process reuse.

Core AI compiles each model specialization once and reuses the on-disk result
without verifying it. An unclean OS or disk stop can tear that result; the next
load then returns garbage vectors (iss_453f69d5) or traps inside BNNS
(iss_2eefe356). A trap cannot be caught in-process, so a child process loads the
model and runs the readiness probe first. A child that dies by a crash signal or
reports invalid output gets this model's cache entries purged and exactly one
recompile; a second failure is loud and final.

Every Python-based solet on a host shares the cache entry, and they prepare
together after the unclean stop that tears it. A lock file beside the cache root
serializes repair: canary attempts hold it shared, and repair holds it
exclusively, re-running the canary first so a concurrent repairer's fresh compile
is never purged.

The child imports the parent's code, but never ahead of the standard library
(iss_831383f5). A live solet's ``sys.path`` can hold a directory that shadows a
stdlib module: ``lizard``, imported in process by the code-vetting scanners,
inserts ``dirname(sys.argv[0])`` (``.../ananta/src/ananta``, which holds
``types/`` and ``platform/`` packages) at index 0. The parent survives that
because it imported those stdlib modules first; a child handed the same list
through ``PYTHONPATH`` resolves ``enum``'s ``import types`` to ``ananta.types``
and dies. The child therefore starts isolated and without ``site`` (``-I -S``),
so its ``sys.path`` is exactly its standard library, runs ``site``, and then
takes the stdlib followed by the parent's path in the parent's order.
"""

import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import EmbeddingError, ErrorCode

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_EMBEDDING_ERROR = 2
EXIT_INVALID_OUTPUT = 3
CANARY_TIMEOUT_SECONDS = 45.0
_LOCK_POLL_SECONDS = 0.05
# Deaths a torn compiled cache can cause; SIGTRAP is the measured signature.
# A killed child (SIGKILL, SIGTERM, SIGINT, ...) says nothing about the cache.
CRASH_SIGNALS = frozenset({
    signal.SIGTRAP, signal.SIGILL, signal.SIGSEGV, signal.SIGBUS, signal.SIGABRT, signal.SIGFPE,
})


def model_cache_key() -> str:
    """Core AI keys a model's cache directory by the pinned ``main.mlirb`` sha256."""
    from .assets import FILE_PINS

    return next(pin.sha256 for pin in FILE_PINS if pin.relative_path == "model.aimodel/main.mlirb")


def default_cache_root() -> Path:
    """Core AI's per-user compiled-model cache; the layout below it is Apple-private."""
    return Path.home() / "Library" / "Caches" / "coreai-cache"


CHILD_MODULE = "coreai_embeddings_plugin.cache_canary"
# Runs in the child before any plugin import: argv is [-c, parent path JSON, module, *args].
CHILD_BOOTSTRAP = """\
import json, runpy, site, sys
stdlib = list(sys.path)
site.main()
sys.path[:] = stdlib + [entry for entry in json.loads(sys.argv.pop(1)) if entry not in stdlib]
runpy.run_module(sys.argv.pop(1), run_name="__main__", alter_sys=True)
"""


def default_child_command() -> tuple[str, ...]:
    """The canary child with the parent's import path as it stands at spawn time."""
    return (sys.executable, "-I", "-S", "-c", CHILD_BOOTSTRAP, json.dumps(sys.path), CHILD_MODULE)


@dataclass(frozen=True)
class CanaryAttempt:
    """One child run: ok, a suspected torn cache, or an unrelated failure."""

    ok: bool
    cache_suspect: bool
    code: ErrorCode
    reason: str


@dataclass(frozen=True)
class CanaryReport:
    """What preparation measured, surfaced through runtime diagnostics."""

    outcome: str
    seconds: float
    first_attempt: str
    purged: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome, "seconds": self.seconds,
            "first_attempt": self.first_attempt, "purged": list(self.purged),
        }


@dataclass(frozen=True)
class CacheCanary:
    """Validate, purge this model's entries once, revalidate, else fail loudly."""

    asset_root: Path
    preference: str
    cache_root: Path = field(default_factory=default_cache_root)
    child_command: Callable[[], Sequence[str]] = default_child_command
    timeout_seconds: float = CANARY_TIMEOUT_SECONDS

    @property
    def lock_path(self) -> Path:
        """Beside the cache root, never inside Core AI's private layout."""
        return self.cache_root.with_name(f"{self.cache_root.name}.{model_cache_key()[:16]}.canary.lock")

    def ensure_healthy(self) -> CanaryReport:
        started = time.monotonic()
        with self._locked(fcntl.LOCK_SH):
            first = self._attempt()
        if first.ok:
            return CanaryReport("healthy", _elapsed(started), first.reason)
        if not first.cache_suspect:
            raise EmbeddingError(first.code, f"Core AI canary failed: {first.reason}")
        with self._locked(fcntl.LOCK_EX):
            return self._repair(started, first)

    def _repair(self, started: float, first: CanaryAttempt) -> CanaryReport:
        """Under the exclusive lock: re-check, then purge once and recompile once."""
        recheck = self._attempt()
        if recheck.ok:
            logger.warning("Core AI compiled cache was repaired by another process (%s)", first.reason)
            return CanaryReport("repaired_by_peer", _elapsed(started), first.reason)
        if not recheck.cache_suspect:
            raise EmbeddingError(recheck.code, f"Core AI canary failed: {recheck.reason}")
        purged = self.purge()
        logger.warning(
            "Core AI compiled cache failed validation (%s); purged %d entr%s for model %s and recompiling once: %s",
            recheck.reason, len(purged), "y" if len(purged) == 1 else "ies", model_cache_key(), purged,
        )
        retry = self._attempt()
        if retry.ok:
            logger.warning("Core AI compiled cache recovered after purge and recompile")
            return CanaryReport("recovered", _elapsed(started), first.reason, tuple(purged))
        logger.error(
            "Core AI compiled cache invalid after purge and recompile: %s (first failure: %s)",
            retry.reason, first.reason,
        )
        raise EmbeddingError(
            ErrorCode.UNAVAILABLE,
            f"Core AI compiled cache invalid after purge and recompile: {retry.reason}",
        )

    @contextmanager
    def _locked(self, mode: int) -> Generator[None]:
        """Hold the repair lock, waiting at most two canary timeouts for another holder."""
        path = self.lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise EmbeddingError(ErrorCode.UNAVAILABLE, f"Core AI canary lock {path} cannot be opened: {exc}") from exc
        try:
            self._acquire(descriptor, mode, path)
            yield
        finally:
            os.close(descriptor)

    def _acquire(self, descriptor: int, mode: int, path: Path) -> None:
        deadline = time.monotonic() + 2 * self.timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EmbeddingError(
                        ErrorCode.UNAVAILABLE,
                        f"Core AI canary lock {path} still held by another process's cache repair",
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)

    def purge(self) -> list[str]:
        """Remove only this model's specializations; refuse any symlink instead of following it.

        Every hit is found before anything is deleted, so a refusal leaves the
        cache untouched. Absence means nothing to purge.
        """
        hits = _model_entries(self.cache_root, model_cache_key())
        for path in hits:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                raise EmbeddingError(ErrorCode.UNAVAILABLE, f"Core AI cache purge of {path} failed: {exc}") from exc
        return [str(path) for path in hits]

    def _attempt(self) -> CanaryAttempt:
        try:
            completed = subprocess.run(
                [*self.child_command(), str(self.asset_root), self.preference],
                capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired:
            return CanaryAttempt(
                False, False, ErrorCode.UNAVAILABLE,
                f"canary exceeded {self.timeout_seconds:g} seconds",
            )
        return _classify(completed.returncode, completed.stdout, completed.stderr)


def _model_entries(cache_root: Path, key: str) -> list[Path]:
    """``<root>/<os build>/<bundle id>/<key>`` directories, walked without following links."""
    if not cache_root.is_absolute():
        raise EmbeddingError(ErrorCode.UNAVAILABLE, f"Core AI cache root is not absolute: {cache_root}")
    if cache_root.is_symlink():
        raise EmbeddingError(ErrorCode.UNAVAILABLE, f"Core AI cache root is a symlink; refusing to purge: {cache_root}")
    if not cache_root.is_dir():
        return []
    resolved_root = cache_root.resolve()
    hits: list[Path] = []
    for build in _real_directories(cache_root):
        for bundle in _real_directories(build):
            entry = bundle / key
            if entry.is_symlink():
                raise EmbeddingError(ErrorCode.UNAVAILABLE, f"Core AI cache entry is a symlink; refusing to purge: {entry}")
            if entry.is_dir():
                if not entry.resolve().is_relative_to(resolved_root):
                    raise EmbeddingError(ErrorCode.UNAVAILABLE, f"Core AI cache entry escapes {cache_root}: {entry}")
                hits.append(entry)
    return sorted(hits)


def _real_directories(parent: Path) -> list[Path]:
    """Subdirectories of ``parent``; a symlink among them is refused, never followed."""
    found: list[Path] = []
    with os.scandir(parent) as entries:
        for item in entries:
            if item.is_symlink():
                raise EmbeddingError(
                    ErrorCode.UNAVAILABLE, f"Core AI cache contains a symlink; refusing to purge: {item.path}",
                )
            if item.is_dir(follow_symlinks=False):
                found.append(Path(item.path))
    return found


def _classify(returncode: int, stdout: str, stderr: str) -> CanaryAttempt:
    if returncode == EXIT_OK:
        return CanaryAttempt(True, False, ErrorCode.UNAVAILABLE, "ok")
    if returncode < 0:
        return _signal_death(-returncode)
    reported = _reported_error(stdout)
    if returncode == EXIT_INVALID_OUTPUT:
        message = reported[1] if reported else "invalid output"
        return CanaryAttempt(False, True, ErrorCode.INVALID_OUTPUT, f"canary invalid output: {message}")
    if returncode == EXIT_EMBEDDING_ERROR and reported is not None:
        return CanaryAttempt(False, False, reported[0], reported[1])
    tail = (stderr.strip().splitlines() or ["no stderr"])[-1][:240]
    return CanaryAttempt(False, False, ErrorCode.UNAVAILABLE, f"canary exited {returncode}: {tail}")


def _signal_death(number: int) -> CanaryAttempt:
    try:
        name = signal.Signals(number).name
    except ValueError:
        name = f"signal {number}"
    if number in CRASH_SIGNALS:
        return CanaryAttempt(False, True, ErrorCode.UNAVAILABLE, f"canary killed by signal {number} ({name})")
    return CanaryAttempt(
        False, False, ErrorCode.UNAVAILABLE,
        f"canary killed by signal {number} ({name}); not a cache fault, cache left intact",
    )


def _reported_error(stdout: str) -> tuple[ErrorCode, str] | None:
    lines = stdout.strip().splitlines()
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
        return ErrorCode(payload["code"]), str(payload["message"])
    except (ValueError, KeyError, TypeError):
        return None


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


def main(argv: Sequence[str]) -> int:
    """Child entry: load, probe and validate in this process, never spawn another canary."""
    from .runtime import EmbeddingRuntime

    if len(argv) != 2:
        print("usage: python -m coreai_embeddings_plugin.cache_canary ASSET_ROOT PREFERENCE", file=sys.stderr)
        return EXIT_EMBEDDING_ERROR
    runtime = EmbeddingRuntime(Path(argv[0]), argv[1])
    try:
        runtime.prepare_in_process()
    except EmbeddingError as exc:
        print(json.dumps({"code": exc.code.value, "message": str(exc)}))
        return EXIT_INVALID_OUTPUT if exc.code is ErrorCode.INVALID_OUTPUT else EXIT_EMBEDDING_ERROR
    finally:
        runtime.close()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
