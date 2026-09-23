"""Runner-neutral resolution and exposure of the ``solet-bridge`` CLI for spawned
fleet workers.

Every spawned worker — Claude Code or Codex, tmux-hosted or subprocess-hosted
— needs the CLI to be BOTH resolvable as a command and reachable at an
absolute path:

* ``heartbeat_report_alive.py`` (PostToolUse) shells out to a bare
  ``["solet-bridge", "call", ...]``;
* ``wake_waiter.py`` (Stop) runs ``[$AGENT_WAKE_CLI, "wake", ...]``;
* the ``/rename`` skill and the watch sidecar both invoke the CLI directly.

A materialized blue-green release, and a tmux pane inheriting the tmux
SERVER's environment rather than the spawning process's, both run with a
minimal ``PATH`` that excludes the release's own ``venv/bin``. Every one of
the consumers above then fails with ``FileNotFoundError``, and — because each
is deliberately non-fatal (portability posture, reviewed coordination-hooks
surface) — fails SILENTLY: exit 0, no liveness stamp, no wake, no error
anywhere. That is the registration-loss mechanism measured on 2026-08-14
(``workbench/2026-08-13_registration_loss_rca_lane_r_report.md``).

Resolving the CLI is only half the contract; the resolved path also has to
SURVIVE. Both rungs below land inside a versioned release directory, and a
deploy reaps old releases — so on 2026-08-16/17 every long-running worker's
``AGENT_WAKE_CLI``, ``PATH`` prepend, and ``solet-bridge watch`` sidecar went
dangling together on cutover, silently, for the same non-fatal-by-design
reason. :func:`stable_release_path` expresses the resolved path through the
deployment's atomically-swapped ``current`` pointer so a spawn survives the
next deploy; :func:`resolve_solet_bin` applies it once, for every surface.

These helpers were introduced for the managed-Codex path (bed25c2a8,
52edfb559, 74cd36af6, all 2026-08-13) and lived in ``codex_common``; they are
not Codex-specific in any way, and the Claude adapters needed exactly the same
treatment. Hoisted here so both runners share ONE implementation rather than
the asymmetry that left every spawned Claude worker unregistered.
``codex_common`` re-exports them under its established private names, so no
Codex call site or smoke changes.

stdlib-only, matching ``env_contract``'s constraint: the console script and
the standalone bridge subprocess must both be able to import it bare.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

CLI_COMMAND_NAME = "solet-bridge"
"""The wake CLI's own BINARY name — never the solet INSTANCE name
(``SOLET_NAME``, e.g. ``acme``). Conflating the two is the 2026-08-08
deaf-wake defect: ``which <instance-name>`` fails, ``which solet-bridge`` resolves."""

RELEASE_ID_PREFIX = "rel-"
"""Basename prefix of a materialized release directory. Mirrors
``macos_self_deployment_plugin.release_manager.RELEASE_ID_PREFIX``; duplicated
rather than imported because this module is stdlib-only by contract (the
console script and the standalone bridge subprocess both import it bare) and
must not take a plugin dependency to name a directory."""

CURRENT_LINK_NAME = "current"
"""The deployment's stable pointer, swapped atomically on cutover. Mirrors
``release_manager.CURRENT_LINK_NAME``; duplicated for the same reason."""

ANANTA_RELEASES_ROOT = Path.home() / ".ananta" / "releases"
"""Canonical parent of per-instance materialized release trees.

Kept as a module constant both to make the ownership boundary explicit and to
let the smoke exercise a synthetic release tree without writing under a real
operator home directory.
"""


def _pointer_names_release(pointer: Path, release_dir: Path) -> bool:
    """True when ``pointer`` (a symlink) CURRENTLY names ``release_dir``.

    Compares the raw link text — ``os.readlink`` plus ``normpath``, handling
    both the relative target the release manager writes and an absolute one —
    and deliberately never ``Path.resolve()``: resolving is the trap that
    would collapse the stable pointer back onto the versioned directory and
    make the whole rewrite decorative.

    A missing/unreadable link reads as "no match" rather than raising. This is
    a narrow, named race window, not a swallowed error: the pointer is
    replaced by ``rename`` during a cutover, so a spawn landing in that window
    must fall back to the versioned path it already had — raising here would
    break spawns that work today, which the Repair-4 precedent forbids.
    """
    try:
        target = os.readlink(pointer)
    except OSError:
        return False
    joined = os.path.normpath(os.path.join(str(pointer.parent), target))
    return joined == os.path.normpath(str(release_dir))


def stable_release_path(binary_path: str) -> str:
    """Rewrite a VERSIONED release path onto the deployment's stable pointer.

    ``~/.ananta/releases/<name>/rel-<ts>-<sha>/venv/bin/solet-bridge`` becomes
    ``~/.ananta/releases/<name>/current/venv/bin/solet-bridge`` — the same file
    today, and still a valid file after the next cutover swaps ``current``.

    This is the fix for the systemic defect measured 2026-08-16/17: a spawned
    worker's ``AGENT_WAKE_CLI``, its ``PATH`` prepend, and its ``solet-bridge watch``
    registration sidecar were all pinned into a versioned directory, and a
    deploy REAPS old releases (``release_manager`` keeps the last K), so every
    deploy dangled every long-running worker's CLI at once — silently, because
    each consumer is deliberately non-fatal.

    Conservative by construction. The rewrite happens only when the pointer
    currently names the very release the path came from, and only when the
    rewritten path actually stats as an executable file; anything else returns
    ``binary_path`` UNCHANGED. Two consequences worth stating:

    * A path outside a release layout (a developer checkout, an operator's
      ``/usr/local/bin/solet-bridge``, a test's temp dir) is returned untouched — no
      release layout is assumed to exist anywhere.
    * Mid-cutover skew, where ``current`` already names a DIFFERENT release
      than the running process, is REFUSED rather than rewritten. Redirecting
      a worker onto a version its spawner is not running would be a silent
      version substitution; keeping the versioned path is honest and is
      exactly the pre-fix behavior.

    The non-regression the Repair-4 guard established still holds either way:
    a dangling export contributes nothing anywhere, so a session that would
    resolve ``solet-bridge`` from PATH is never made worse by this.
    """
    if not binary_path:
        return binary_path
    source = Path(binary_path)
    if not source.is_absolute():
        return binary_path
    for release_dir in source.parents:
        if not release_dir.name.startswith(RELEASE_ID_PREFIX):
            continue
        pointer = release_dir.parent / CURRENT_LINK_NAME
        if not pointer.is_symlink():
            continue
        if not _pointer_names_release(pointer, release_dir):
            continue
        stable = pointer / source.relative_to(release_dir)
        if stable.is_file() and os.access(stable, os.X_OK):
            return str(stable)
    return binary_path


def _resolve_path(path: Path) -> Path | None:
    """Resolve symlinks without making a reaped release an exception path."""
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


def _venv_instance_root(python_executable: str | None) -> Path | None:
    """Return the active environment's owning root, resolving symlinks.

    Inspect the invoked Python spelling before resolving it: a normal
    ``venv/bin/python3`` is a symlink to a framework interpreter, and resolving
    that file itself would erase the venv ownership evidence. Resolving the
    environment directory still follows a release's ``current`` pointer to its
    concrete ``rel-*`` directory while retaining a developer venv's owner.
    """
    executable = Path(python_executable or sys.executable)
    if executable.parent.name != "bin":
        return None
    environment = executable.parent.parent
    if environment.name not in {"venv", ".venv"}:
        return None
    resolved_environment = _resolve_path(environment)
    if resolved_environment is None:
        return None
    return resolved_environment.parent


def _release_instance_root(instance_root: Path) -> Path | None:
    """Return ``~/.ananta/releases/<instance>`` owning ``instance_root``."""
    releases_root = _resolve_path(ANANTA_RELEASES_ROOT)
    if releases_root is None:
        return None
    candidate = instance_root.parent
    if candidate.parent == releases_root:
        return candidate
    return None


def _is_venv_console_script(path: Path) -> bool:
    """Whether a resolved path has the expected console-script shape."""
    return (
        path.name == CLI_COMMAND_NAME
        and path.parent.name == "bin"
        and path.parent.parent.name in {"venv", ".venv"}
    )


def _is_release_family_member(candidate: Path, instance_root: Path) -> bool:
    """Whether ``candidate`` is a current/rel-* CLI of this instance family."""
    release_root = _release_instance_root(instance_root)
    candidate_environment_root = candidate.parent.parent.parent
    if release_root is None or candidate_environment_root.parent != release_root:
        return False
    return candidate_environment_root.name == CURRENT_LINK_NAME or (
        candidate_environment_root.name.startswith(RELEASE_ID_PREFIX)
    )


def _is_instance_cli(
    candidate: str,
    *,
    python_executable: str | None,
) -> bool:
    """Return whether ``candidate`` is owned by the active instance.

    A lexical ``venv/bin/solet-bridge`` suffix is not ownership proof: an unrelated
    vendor virtualenv can use exactly that layout.  Resolve both the candidate
    and active Python environment, then accept only a console script whose
    environment owner equals that environment's root. A materialized release
    additionally recognizes its own ``~/.ananta/releases/<instance>`` family.
    Its ``current`` and ``rel-*`` spellings retain the established blue-green
    behavior without admitting a foreign instance or global package manager.

    The original candidate string is deliberately never rewritten here.  The
    stable-pointer spelling must reach :func:`stable_release_path` intact.
    """
    path = Path(candidate)
    resolved = _resolve_path(path)
    instance_root = _venv_instance_root(python_executable)
    if resolved is None or instance_root is None or not path.is_absolute():
        return False
    if not _is_venv_console_script(resolved):
        return False
    candidate_environment_root = resolved.parent.parent.parent
    if candidate_environment_root == instance_root:
        return True
    return _is_release_family_member(resolved, instance_root)


def _discover_solet_bin(
    explicit: str | None,
    python_executable: str | None,
) -> str:
    """Return an owned CLI from PATH or the active virtualenv sibling.

    A PATH candidate is still considered first for the established release
    behavior, but it must prove ownership by the supplied active Python
    environment or its recognized release family. A rejected foreign candidate
    does not terminate discovery: the active virtualenv sibling remains the
    next owned rung. Explicit paths obey the same check; an override is a
    source of a candidate, not an authority to cross the instance boundary.
    """
    if explicit is not None:
        return explicit if _is_instance_cli(
            explicit, python_executable=python_executable,
        ) else ""
    discovered = shutil.which(CLI_COMMAND_NAME)
    if discovered and _is_instance_cli(
        discovered, python_executable=python_executable,
    ):
        return discovered
    executable = Path(python_executable or sys.executable)
    sibling = executable.parent / CLI_COMMAND_NAME
    if (
        sibling.is_file()
        and os.access(sibling, os.X_OK)
        and _is_instance_cli(str(sibling), python_executable=python_executable)
    ):
        return str(sibling)
    return ""


def resolve_solet_bin(
    explicit: str | None,
    *,
    python_executable: str | None = None,
) -> str:
    """Resolve an owned CLI from PATH, then the active Python environment,
    then express the result through the stable release pointer when there is
    one.

    Materialized blue-green releases intentionally run with a minimal PATH
    that does not include their own ``venv/bin`` directory.  The release's
    Python executable and ``solet-bridge`` console script are siblings, so that
    directory is the deterministic second rung when PATH lookup is empty.

    Every candidate, including an ``explicit`` override, must prove ownership
    by the supplied/active Python environment (with symlinks resolved), or by
    that environment's own recognized materialized-release family. Both
    accepted rungs can hand back a path inside a VERSIONED release directory,
    which the next deploy may reap. Every spawn surface derives from this one
    function — ``expose_worker_cli``'s
    ``AGENT_WAKE_CLI`` and ``PATH`` prepend, ``watch_sidecar_argv``'s
    registration sidecar, ``worker_path``, and the Codex config overrides —
    so :func:`stable_release_path` is applied HERE, once, rather than at six
    call sites that would drift apart.

    Returns ``""`` when neither rung resolves — callers decide whether that is
    fatal (the Codex watch path refuses to spawn) or merely degrading (the
    Claude adapters fall back to the bare command name, preserving their
    pre-fix behavior exactly rather than refusing a spawn that used to work).

    **NEVER CACHE THIS FOR THE LIFE OF A PROCESS.** This function's answer is a
    function of MUTABLE FILESYSTEM STATE — the ``current`` symlink's target —
    so it is a transient, not a constant. Call it per spawn.

    That is not a style preference; it is the R11 defect, measured live
    2026-08-17. All four spawn adapters used to evaluate this once in
    ``__init__`` and store the result. The platform process started at 13:38:51Z,
    between a release being materialized (13:38Z) and cutover flipping ``current``
    onto it (13:43Z) — inside the skew window, where
    :func:`stable_release_path` correctly REFUSES to rewrite and hands back the
    versioned path. Refusing was right; caching the refusal was not. Every worker
    spawned for the rest of that process's life — verified on a spawn at 16:30Z,
    2h47m after the flip, while a fresh call in the same release returned the
    ``current`` path — inherited a versioned ``AGENT_WAKE_CLI``, ``PATH`` prepend
    and watch-sidecar argv, i.e. exactly the reap-dangling pin this whole module
    exists to prevent. A conservative branch cached forever becomes a permanent
    one, and every consumer downstream is non-fatal, so the whole fleet would
    have gone dark silently at the next cutover.

    The corollary for reviewers: a test that calls this function directly can
    only ever prove the FUNCTION is right. It cannot see a caller that asks once
    and remembers. Observing the flip must happen AFTER the caller is
    constructed — see ``release_pointer_stability_smoke``'s adapter legs.
    """
    return stable_release_path(
        _discover_solet_bin(explicit, python_executable),
    )


class WakeCliResolver:
    """Resolves the wake CLI per spawn without ever handing back a dead path.

    R11 (2026-08-17). All four spawn adapters used to call
    :func:`resolve_solet_bin` once in ``__init__`` and store the answer. That
    answer is a function of the ``current`` symlink's target — mutable state that
    cutover moves under a long-lived process — so caching it froze whatever the
    filesystem happened to say at startup. Measured consequence: the platform
    process started at 13:38:51Z, inside the window between a release being
    materialized (13:38Z) and cutover flipping ``current`` onto it (13:43Z), where
    :func:`stable_release_path` correctly REFUSES to rewrite. Every worker spawned
    for the rest of that process's life inherited the versioned path — confirmed
    on a real spawn at 16:30Z, 2h47m after the flip, while a fresh call inside the
    same release returned the ``current`` path.

    Re-resolving on every read fixes that and, on its own, INTRODUCES A SECOND
    DEFECT. Measured minimal repro before this class existed:

    * ``current`` -> ``rel-OLD``; resolve once -> ``.../current/venv/bin/solet-bridge``.
    * Deploy: materialize ``rel-NEW``, flip ``current``, REAP ``rel-OLD``.
    * The cached value still resolves through ``current`` and EXECUTES.
    * A fresh resolution returns ``.../rel-OLD/venv/bin/solet-bridge``, which no longer
      exists — the discovery rung hands back the construction-time versioned path
      (an explicit override, or ``sys.executable``'s sibling, which is equally
      fixed for a process's life), and the skew branch then refuses to rewrite it.

    So the cache was accidentally protecting a real property. Two shipped
    invariants are in tension whenever the running release is not ``current``:
    skew must not be silently version-substituted, and a worker must never be
    handed a path a reap can dangle. Neither is negotiable, and no single
    resolution strategy satisfies both.

    This class satisfies both by ranking two candidates instead of picking one
    strategy: prefer the FRESH resolution, and fall back to the construction-time
    stabilized value only when the fresh answer does not stat as an executable
    file and the fallback does. Consequences, all three measured by the adapter
    legs in ``release_pointer_stability_smoke``:

    * Flip onto the running release after a skewed startup — fresh wins, the R11
      defect is closed.
    * Cutover away plus a reap — fresh is a corpse, the stabilized fallback wins,
      no dangling CLI reaches a worker.
    * Skew with a LIVE source — fresh is a real versioned executable and is
      returned unchanged, so the deliberate no-silent-substitution invariant and
      its own smoke leg are untouched.
    * NEITHER usable — the fresh answer is returned, ``""`` included, so the
      degradation contract callers already implement is unchanged (the Codex watch
      path refuses to spawn; the Claude adapters fall back to the bare command
      name). This branch invents nothing. It is named here deliberately: an
      unnamed third branch is where the next defect of this shape would hide.

    THE FALLBACK DOES NOT WEAKEN THE NO-SILENT-SUBSTITUTION INVARIANT, and a
    reader who thinks it does will "fix" it back into a defect. That invariant
    governs the MOMENT OF REWRITING — do not silently swap in a different version
    when deciding what path to hand out. It says nothing about the durability of a
    path that was already, legitimately, rewritten. A ``current/...`` path
    continuing to follow ``current`` across a cutover is not a substitution; it is
    the entire purpose of rewriting onto a stable pointer. The live-source skew
    case still returns the fresh versioned path, which is where the invariant
    actually applies, and its smoke leg stays green.

    It never returns a path that does not exist while one that does is in hand,
    and it never substitutes a version while the honest answer is still alive.
    """

    __slots__ = ("_fallback", "_override", "_python_executable")

    def __init__(
        self,
        override: str | None,
        *,
        python_executable: str | None = None,
    ) -> None:
        self._override = override
        self._python_executable = python_executable
        # Resolved once, at construction, on purpose: this is the LAST-KNOWN-GOOD
        # rung, not the answer. It is only ever consulted when the fresh answer
        # has become unusable, which is precisely the case the pre-R11 cache
        # handled correctly and a naive re-resolve regressed.
        self._fallback = resolve_solet_bin(
            override, python_executable=self._python_executable,
        )

    def resolve(self) -> str:
        """The wake CLI to hand a worker, resolved now."""
        fresh = resolve_solet_bin(
            self._override, python_executable=self._python_executable,
        )
        if fresh and Path(fresh).is_file() and os.access(fresh, os.X_OK):
            return fresh
        if self._fallback and Path(self._fallback).is_file() and os.access(
            self._fallback, os.X_OK,
        ):
            logger.warning(
                "wake CLI %r no longer resolves to an executable; falling back to "
                "the construction-time stable path %r (a cutover moved `current` "
                "away from this process's release and the old release was reaped)",
                fresh,
                self._fallback,
            )
            return self._fallback
        # Both rungs unusable. Return the fresh answer — including "" — so the
        # degradation contract every caller already implements is unchanged: the
        # Codex watch path refuses to spawn, the Claude adapters fall back to the
        # bare command name. Never invent a path here.
        return fresh


def expose_worker_cli(env: dict[str, str], solet_bin: str) -> None:
    """Expose the resolved CLI to hooks and ordinary worker shell commands.

    Sets ``AGENT_WAKE_CLI`` to the ABSOLUTE binary (so the Stop hook's
    ``subprocess.run([$AGENT_WAKE_CLI, ...])`` never depends on PATH at all)
    AND prepends its directory to ``PATH`` (so the hooks and skills that
    invoke a bare ``solet-bridge`` — which this fix deliberately does not rewrite —
    resolve it too). Mutates ``env`` in place.
    """
    env["AGENT_WAKE_CLI"] = solet_bin or CLI_COMMAND_NAME
    if not solet_bin or not Path(solet_bin).is_absolute():
        return
    cli_dir = str(Path(solet_bin).parent)
    path_entries = env.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(
        [cli_dir, *[entry for entry in path_entries if entry and entry != cli_dir]],
    )


def worker_path(solet_bin: str) -> str:
    """The PATH a managed worker should run with, CLI directory first."""
    env = {"PATH": os.environ.get("PATH", "")}
    expose_worker_cli(env, solet_bin)
    return env["PATH"]


def watch_sidecar_argv(
    solet_bin: str, *, agent_id: str, spool: bool,
) -> list[str]:
    """The registered-presence sidecar's argv, minus ``--exit-with-parent``
    (each host supplies its own parent pid: a literal for a tracked
    subprocess, shell-expanded ``$$`` for a tmux pane).

    ``--no-claim`` ALWAYS. Registration is presence, not role ownership:
    ``spawn_session.role_name`` authorizes a later model-driven claim but must
    never bind a role as a launcher side effect
    (``local_cli.cli._register_without_claim``). It also keeps the
    claim-under-inherited-session-id trap out of the launcher path entirely.

    ``spool`` is a required keyword rather than a default anyone can drift
    into, since a caller genuinely can want either value:

    * Claude Code -> ``True``. ``wake_waiter.py`` is a real Stop hook and
      the spool tee is precisely what it consumes; arming without it would
      re-deafen the wake this sidecar exists to enable.
    * NEITHER current Codex host driver (``codex_tmux.py``,
      ``codex_app_server.py``) calls this helper at all -- they build their
      watch argv inline, and as of CDX-06 (2026-08-24) that inline argv
      keeps the spool armed too: ``inbox_consumer.py`` is now a real
      synchronous Stop hook that drains it, the same shape as Claude's.
      codex-0147-dead-spool-retirement (2026-08-13) disabled it because
      stock Codex then had no Stop-hook consumer at all -- that premise no
      longer holds, and ``spool=False`` here is exercised only as this
      helper's own mechanics (see ``test_sidecar_argv_contract``), not
      current policy for any live caller.
    """
    argv = [solet_bin, "watch", "--agent-id", agent_id, "--no-claim"]
    if not spool:
        argv.append("--no-spool")
    return argv


__all__ = [
    "CLI_COMMAND_NAME",
    "CURRENT_LINK_NAME",
    "RELEASE_ID_PREFIX",
    "WakeCliResolver",
    "expose_worker_cli",
    "resolve_solet_bin",
    "stable_release_path",
    "watch_sidecar_argv",
    "worker_path",
]
