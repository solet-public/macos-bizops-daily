"""Fleet session-management Phase B, D1 (§5) — the ``headless`` HostDriver:
the universal-floor driver, hardening WS-6's ``drive_session.py``/
``driven_sessions.py`` prior art (``workbench/2026-07-31_ws6_equality_gate/``)
into production code. Spawns a real Claude Code process over stream-json
stdin/stdout (the same handle §5 calls the ``DriverChannel`` for
clear/compact — AMEND 5b, a separately-land-able later step; this module only
returns the raw ``Popen`` where a channel exists, it does not yet implement
send/turn semantics).

EPHEMERAL BY DESIGN, NOT PERSISTED (mirrors
``macos_coding_agent_session_plugin.bridge_tracker``'s proven "no persistence
across plugin restarts" posture): the ``host_ref -> Popen`` map lives in this
driver INSTANCE's memory only. A solet restart loses that map even
though the OS process may survive as an orphan (``start_new_session=True``
detaches it from the parent's process group on purpose — see ``shutdown()``).
So ``driver_channel()`` correctly returns ``None`` for any host_ref this
process's memory doesn't recognize; ``alive()`` still answers correctly
post-restart via a bare ``kill -0`` probe, since that needs no memory at all.

PERMISSION MODE IS A GOVERNANCE RULING, NOT THIS MODULE'S CALL:
``claude_launcher.template``'s invariant forbids
``--dangerously-skip-permissions`` for operator sessions ("your tool-approval
flow is the safety boundary between the agent and your machine"). An
unattended headless worker has no human to approve prompts, so *some*
permission posture is required to spawn at all — but which one is an
operator/governance decision this module does not make unilaterally. It reads
``FLEET_HEADLESS_PERMISSION_MODE``; unset -> :meth:`verify_config` returns a
remedy and :meth:`spawn` refuses (fail-closed, the same posture
``host_mechanism_missing`` already uses elsewhere in this package).

SWAP-MORTAL BY CONSTRUCTION (D2 live-acceptance evidence, 2026-08-04 13:07Z,
pid 96001): a headless worker is killed on every platform stop/restart,
including a blue-green deploy swap — ``shutdown()`` (below) is wired into
``plugin.py.stop_services`` and SIGTERM-then-SIGKILLs every tracked worker
unconditionally when the quiescing side stops. This is independent of the
``start_new_session=True`` process-group detachment noted above (that only
changes signal delivery for things like a closed terminal, not this plugin's
own explicit shutdown call) and independent of stdin/EOF behavior. Contrast:
a ``tmux``-hosted worker (``tmux_adapter.py``) is swap-durable BY
CONSTRUCTION — its PTY belongs to the independent tmux daemon, not to this
plugin process, so nothing on the platform's stop/swap path touches it.
Prefer the tmux driver for any lane-scoped worker that must survive a
deploy; a live headless worker at swap time needs an explicit quiesce-or-
accept decision recorded in the deploy reason.

IDENTITY WIRING (verified against the real registration flow, not assumed):
``mcp_bridge/__main__.py`` (the MCP bridge subprocess Claude Code's own
``--mcp-config`` attach spawns as its child) already honors an injected
``AGENT_INSTANCE_ID`` env var "so a managed spawner ... keeps a STABLE
agent_instance_id" (v10 Control #2.D) — exactly this driver's case. Passing
the ledger's ``agent_instance_id`` through env is what lets
``backfill_registration`` (``session_lifecycle_store.py``) find the right
``managed_session`` row on first registration; without it the child would
mint its own id and the registration hook would silently miss on every real
spawn. ``AGENT_SESSION_ID`` is minted exactly ONCE, here (never re-derived
elsewhere — two evaluations of an identity expression is two identities, per
standing platform lesson).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from .authority_contract import render_authority_delegation_contract
from .lane_worktrees import LaneWorktreeError, spawn_worktree_cwd, worktree_pythonpath
from .schema import CAPTURE_SOURCE_INIT_EVENT
from .solet_cli import (
    WakeCliResolver,
    expose_worker_cli,
    watch_sidecar_argv,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

logger = logging.getLogger(__name__)

_ENV_PERMISSION_MODE = "FLEET_HEADLESS_PERMISSION_MODE"
DEFAULT_TERMINATE_GRACE_SECONDS = 10.0


def _resolve_default_cwd() -> Path:
    """The git-checkout root a spawned headless worker operates in.

    Prefers ``APP_HOME``'s parent (the shared ``<clone>/profile`` every
    colour is launched with -- ``cli.py`` bakes ``--app-home`` into the env
    var) over a bare ``Path.cwd()``: the running solet process's own OS
    working directory has no guaranteed relationship to the checkout at all
    (observed live: a deployed colour's cwd was ``~/.ananta/runtime``, a pure
    state/spool directory with no ``.mcp.json`` or source in it). Mirrors
    ``seed_factory_plugin.assemble.resolve_default_repo_root`` /
    ``macos_self_deployment_plugin``'s ``_resolve_project_root_for_autostart``
    -- same validated pattern, duplicated rather than cross-imported (adapted
    binding, shared contract, not a plugin-to-plugin dependency, same
    rationale as ``_pid_alive`` above). Falls back to ``Path.cwd()`` when
    ``APP_HOME`` is unset or its parent is not a git checkout (the unit-test /
    standalone-driver context, which passes an explicit ``cwd`` anyway)."""
    app_home = os.environ.get("APP_HOME", "").strip()
    if app_home:
        candidate = Path(app_home).resolve().parent
        if (candidate / ".git").exists() and (candidate / "ananta").is_dir():
            return candidate
    return Path.cwd()


def _resolve_spawn_cwd(spec: Mapping[str, object], fallback: Path) -> Path:
    """Map an explicit lane tree into a host-spawn error at the adapter edge."""
    from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

    try:
        return spawn_worktree_cwd(spec.get("worktree_path"), fallback)
    except LaneWorktreeError as exc:
        raise HostCannotSpawnError(str(exc)) from exc


def _with_worktree_pythonpath(env: dict[str, str], cwd: Path) -> dict[str, str]:
    env["PYTHONPATH"] = worktree_pythonpath(cwd, env.get("PYTHONPATH", ""))
    return env


def _resolve_spawn_transport(spec: Mapping[str, object], floor: str) -> str:
    """Prefer the policy-resolved spawn value, then the driver floor."""
    return str(spec.get("transport") or "") or floor or "watch"


# R4 Package C (2026-08-10): a born clone ships NO ".claude/hooks/" at all
# (that directory is this dev checkout's own, never in seed_manifest.yaml's
# copy: allowlist) -- but both spawn adapters previously built every worker
# hook path as a bare ``self._cwd / ".claude" / "hooks" / <file>`` with no
# existence check. On a born clone this generated a ``--settings`` blob
# whose PreToolUse hook referenced a nonexistent file; Claude Code's own
# hook contract makes a `python3 <missing file>` PreToolUse failure a
# BLOCKING error (exit 2, non-hook-related), so every spawned worker's
# every tool call was blocked from its first turn. This ladder closes that
# gap: rung 2 is the shipped fallback every clone DOES carry.
_WORKER_INJECTED_HOOK_FILENAMES: tuple[str, ...] = (
    "headless_tool_allowlist_gate.py",
    "capture_session_mapping.py",
    "heartbeat_report_alive.py",
    "rotation_due_watch.py",
    "wake_waiter.py",
    "check_messages_reminder.py",
    "step_zero_reminder.py",
    "role_binding_reminder.py",
)
_PLUGIN_HOOKS_DIR_RELATIVE = Path(
    "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks",
)

# W4A item 2 (#8 §43.1) — the managed-policy preflight.
#
# An org-managed Claude Code policy carrying ``hooks`` inside
# ``strictPluginOnlyCustomization`` strips hooks from every NON-PLUGIN source,
# which includes the ``--settings`` blob this driver (and the tmux driver)
# uses to inject worker hooks. The adopter's own isolation test established
# that; we did not infer it from the flag's name.
#
# EVIDENCE GRADES, kept explicit because they differ and the difference
# matters if this ever mis-fires:
#  * The REMOTE path is MEASURED on an affected machine: a managed policy can be
#    pulled remotely to ~/.claude/remote-settings.json, and on such a machine the
#    system path DOES NOT EXIST at all. Searching only the system locations
#    therefore reports "no managed policy in force" on exactly the deployments
#    where one IS in force — a silent fail-open. Listed FIRST because it is the
#    only location a policy has actually been observed at in the field.
#  * The macOS system path is the originally-reported location.
#  * The Linux and Windows paths are Claude Code's documented siblings for the
#    same file. They are NOT adopter-evidenced, and there is no such file on
#    this machine to check any of them against — which is exactly why the
#    reader below is driven by a FIXTURE in the smokes rather than by a live
#    probe. An absent-file probe verifies nothing about the populated case.
_MANAGED_SETTINGS_PATHS: tuple[Path, ...] = (
    Path.home() / ".claude" / "remote-settings.json",
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path("/etc/claude-code/managed-settings.json"),
    Path("C:/ProgramData/ClaudeCode/managed-settings.json"),
)
_STRICT_PLUGIN_ONLY_KEY = "strictPluginOnlyCustomization"
_STRICT_PLUGIN_ONLY_HOOKS = "hooks"
# §45.1 (#17, 2026-08-17) — THE SECOND KEY IN THE SAME PARSED OBJECT.
#
# ``allowManagedPermissionRulesOnly: true`` makes Claude Code honour only the
# permission rules written inside the managed settings file itself: user- and
# project-level allow/ask/deny rules do not apply AT ALL, in every permission
# mode. Every ``permissions.deny`` this repository ships is therefore dropped
# before permission evaluation on such a host, with no local signal — the
# denies simply never participate. The adopter established this against Claude
# Code's own permissions documentation rather than inferring it, and ruled out
# the stale-session hypothesis (permission settings reload live in-session, so
# it reproduces identically in a brand-new session).
#
# This key was ALREADY in the dict the reader below parses for §43.1. The
# detector we built in response to that report was one sibling key short of
# detecting this one; adding it here, rather than in a new reader, is the
# point.
_ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY = "allowManagedPermissionRulesOnly"
# Single source of truth for the deny list this driver's ``--settings`` blob
# emits, so the inert-permissions warning below can never name a different set
# of tools than the one actually denied.
_HEADLESS_DENIED_TOOLS: tuple[str, ...] = ("Agent", "Task")


def _iter_managed_policies(
    paths: tuple[Path, ...],
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Every readable managed policy in ``paths``, parsed, in search order.

    FAILS OPEN on an unreadable or malformed file, loudly. Refusing every
    spawn because a policy file could not be parsed would be a worse failure
    than the ones this guards, and it would be a refusal we cannot actually
    justify — an unparseable file is not evidence that anything was stripped.
    The registration watchdog (``session_sweep``) is the backstop that catches
    the hooks consequence whatever the cause, so these narrow provers do not
    need to guess.

    Shared by both provers below so the two can never disagree about which
    files are in force, in what order, or how a bad one is handled.
    """
    for path in paths:
        try:
            if not path.is_file():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning(
                "managed-settings preflight: %s exists but could not be read or "
                "parsed — proceeding WITHOUT these checks (an unparseable policy "
                "file is not evidence that anything is stripped)", path, exc_info=True,
            )
            continue
        if not isinstance(raw, dict):
            continue
        yield path, raw


def _managed_policy_strips_hooks(paths: tuple[Path, ...]) -> Path | None:
    """The first policy file that declares hooks plugin-only, or ``None``."""
    for path, raw in _iter_managed_policies(paths):
        strict = raw.get(_STRICT_PLUGIN_ONLY_KEY)
        if isinstance(strict, list) and _STRICT_PLUGIN_ONLY_HOOKS in strict:
            return path
    return None


def _managed_policy_voids_permission_rules(paths: tuple[Path, ...]) -> Path | None:
    """The first policy file that switches off non-managed permission rules.

    Matches ``True`` and nothing else. The flag is documented as a boolean, so
    a truthy non-boolean (``"false"``, ``1``, ``[]``) is not evidence that the
    organisation set it — and guessing at a shape upstream does not define is
    exactly the silent fallback this repository forbids. Note ``1 == True`` in
    Python, so this comparison must stay an identity check.
    """
    for path, raw in _iter_managed_policies(paths):
        if raw.get(_ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY) is True:
            return path
    return None


def _permission_state_report(
    denied_tools: tuple[str, ...], *, denies_key: str,
) -> dict[str, object]:
    """The §45.1 permission-state keys both drivers' capability reports carry.

    Module-level and shared: the two reports must answer the same question the
    same way, and both driver classes sit within a handful of lines of the
    god-class LOC ceiling, so this cannot live in either class body.

    Live-probed rather than cached. A managed policy can be pulled remotely at
    any time, so a value resolved once at construction would go stale in
    exactly the direction that reports a lost guardrail as still present.
    """
    inert_policy = _managed_policy_voids_permission_rules(_MANAGED_SETTINGS_PATHS)
    return {
        denies_key: list(denied_tools),
        "permission_denies_operative": inert_policy is None,
        "permission_policy_path": str(inert_policy) if inert_policy else None,
    }


def _headless_capability_report() -> dict[str, object]:
    """Pure function over module constants, no ``self`` needed — module-level
    for the same god-class-ceiling reason ``_tmux_hook_settings_json`` is."""
    return {
        "host": "headless",
        "topology": "subprocess",
        "inspectable_via": ["transcript"],
        "attach_hint": "driver process holds the stdin/stdout stream-json channel",
        **_permission_state_report(_HEADLESS_DENIED_TOOLS, denies_key="permission_denies"),
    }


def _warn_permission_denies_inert(
    paths: tuple[Path, ...],
    *,
    agent_instance_id: str,
    host: str,
    denied_tools: tuple[str, ...],
) -> Path | None:
    """Loud notice that this host drops the driver's ``permissions.deny``.

    WARN, NOT REFUSE — deliberately the OPPOSITE posture from
    :func:`_managed_policy_remedy`, and the discriminator is whether the
    spawn's own contract survives.

    A hooks-stripped worker never registers, never heartbeats and is
    unaddressable by role: the spawn produces nothing usable and leaves a
    ledger row that looks live. Refusing costs the operator a worker they were
    never going to get.

    A permissions-inert worker registers, heartbeats, answers by role and does
    its work. What it loses is a hygiene guardrail — and a deny that a local
    policy can switch off was never a boundary, so this earns no security
    credit and must not be described as one. The finding is the SILENCE.
    Refusing here would trade the entire fleet capability for a guardrail that
    was never load-bearing, on a machine whose managed policy the operator has
    no authority to change; a refusal with no reachable remedy is a wall, not a
    gate. Enforcement of this repository's Agent/Task prohibition also degrades
    from two layers to one, not to zero: the instruction layer (``CLAUDE.md``,
    and ``CLAUDE.md.template`` for adopters) still carries it.

    There is deliberately NO acknowledgement flag matching
    ``degraded_hooks_acknowledged``. That flag works because accepting
    hooks-degraded operation is a real choice an operator can make once and
    mean. Here there is no fix available to the operator at all, so an opt-in
    every spawn on the affected machine must always pass is ceremony that
    stops carrying information within a day.

    ``denied_tools`` is the calling host's OWN list because the two differ: the
    tmux driver denies ``AskUserQuestion`` as well, and the headless driver
    does not (its stream-json mode never enumerates that tool, so the deny
    would be inert there for an unrelated reason).
    """
    policy_path = _managed_policy_voids_permission_rules(paths)
    if policy_path is None:
        return None
    denied = ", ".join(denied_tools)
    logger.warning(
        "PERMISSION DENIES INERT: the managed Claude Code policy at %s sets "
        "%s=true, under which user- and project-level allow/ask/deny rules do "
        "not apply at all — only rules inside the managed file itself are "
        "honoured. The %s driver's --settings deny of [%s] is therefore dropped "
        "before permission evaluation for %s, and Claude Code emits no signal "
        "of its own when this happens. The spawn PROCEEDS: this worker still "
        "registers, heartbeats and is addressable, and permissions.deny is a "
        "hygiene guardrail rather than a security control. What is lost is "
        "config-layer enforcement of this repository's Agent/Task prohibition; "
        "the instruction layer still carries it. The only place a deny is "
        "honoured on this host is the managed file's own permissions.deny — "
        "adding [%s] there is the fix, and it belongs to whoever administers "
        "that file.",
        policy_path, _ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY, host, denied,
        agent_instance_id, denied,
    )
    return policy_path


def _claude_bin_remedy(claude_bin: str) -> str | None:
    if claude_bin and os.access(claude_bin, os.X_OK):
        return None
    return (
        f"no executable 'claude' binary found (checked PATH via "
        f"shutil.which, then {claude_bin!r}) — install Claude "
        "Code or pass claude_bin explicitly."
    )


def _solet_name_remedy(solet_name: str) -> str | None:
    if solet_name:
        return None
    return (
        "SOLET_NAME is not set — the spawned process cannot "
        "discover its bridge port without it."
    )


def _permission_mode_remedy(resolved_permission_mode: str) -> str | None:
    if resolved_permission_mode:
        return None
    return (
        f"no permission mode configured (neither a per-spawn override nor "
        f"{_ENV_PERMISSION_MODE} is set) — an unattended headless worker "
        "needs an explicit operator-ruled posture; this driver never "
        "defaults to bypass."
    )


def _mcp_config_remedy(resolved_transport: str, mcp_config_path: Path) -> str | None:
    if resolved_transport != "mcp" or mcp_config_path.exists():
        return None
    return (
        f"no MCP config found at {mcp_config_path} — required because "
        "the resolved transport is 'mcp' (a real MCP bridge config must "
        "exist and is passed verbatim to --mcp-config); 'watch' transport "
        "spawns with an inline empty MCP config and never reads this file, "
        "so switching FLEET_SESSION_HOST/transport to 'watch' (the charter "
        "default) also satisfies this remedy without creating the file."
    )


def _managed_policy_remedy(
    managed_settings_paths: tuple[Path, ...] | None,
    *,
    degraded_hooks_acknowledged: bool,
) -> str | None:
    """W4A item 2: an attributable refusal, matching this module's own
    :class:`WorkerHookResolutionError` precedent — a spawn we can PROVE will
    come up deaf is refused by name at config time rather than discovered
    later from a row that never leaves ``spawning``."""
    if degraded_hooks_acknowledged:
        return None
    policy_path = _managed_policy_strips_hooks(
        managed_settings_paths if managed_settings_paths is not None
        else _MANAGED_SETTINGS_PATHS,
    )
    if policy_path is None:
        return None
    return (
        f"the managed Claude Code policy at {policy_path} lists "
        f"{_STRICT_PLUGIN_ONLY_HOOKS!r} in {_STRICT_PLUGIN_ONLY_KEY}, which "
        "strips hooks from every non-plugin source — including the "
        "--settings blob this driver injects the worker's hooks through. "
        "A worker spawned here would answer turns while running NONE of "
        "its hooks: it would never register, never heartbeat, and never "
        "capture its session mapping. Delivering the worker hooks as "
        "PLUGIN hooks is the capability fix and is not yet shipped. To "
        "spawn anyway, set degraded_hooks_acknowledged on the spawn — it "
        "is recorded on the ledger row, so running degraded stays a "
        "stated choice rather than a later discovery."
    )


def _resolve_local_label(spec: Mapping[str, object], *, agent_instance_id: str) -> str:
    """W6 (#13 §44.3): the name the worker answers to on its own machine.

    ``spawn_session`` resolves ``local_name`` (role_name for a project-class
    role, else lane_id) and passes it down; the lane_id fallback preserves the
    previous behaviour exactly for any caller that sends no ``local_name``.
    Shared by BOTH host drivers so the two can never disagree about what a
    worker is called — the tmux driver imports it from here.
    """
    return (
        str(spec.get("local_name") or "")
        or str(spec.get("lane_id") or "")
        or agent_instance_id
    )


def _announce_managed_policy(
    degraded_ok: bool, *, agent_instance_id: str, host: str,
    denied_tools: tuple[str, ...],
) -> None:
    """Both spawn-time managed-policy announcements, in one call.

    Shared by both host drivers so neither can grow one announcement and not
    the other, and module-level because both driver classes are within a few
    lines of the god-class ceiling.
    """
    _log_degraded_spawn(degraded_ok, agent_instance_id=agent_instance_id)
    _warn_permission_denies_inert(
        _MANAGED_SETTINGS_PATHS, agent_instance_id=agent_instance_id,
        host=host, denied_tools=denied_tools,
    )


def _log_degraded_spawn(degraded_ok: bool, *, agent_instance_id: str) -> None:
    """W4A item 3: proceeding is allowed, silence is not. The ledger records
    the acknowledgement; this is the same fact in the log, so the choice is
    legible from either surface."""
    if not degraded_ok or _managed_policy_strips_hooks(_MANAGED_SETTINGS_PATHS) is None:
        return
    logger.warning(
        "DEGRADED SPAWN: %s is being spawned with degraded_hooks_acknowledged "
        "set, onto a host whose managed policy strips non-plugin hooks. This "
        "worker will run NONE of its injected hooks — it will not register, "
        "heartbeat, or capture its session mapping. This was an explicit choice.",
        agent_instance_id,
    )


class WorkerHookResolutionError(RuntimeError):
    """Raised when a worker-injected hook file resolves at neither rung of
    :func:`_resolve_worker_hook_paths` -- fail loud at spawn time rather
    than emit generated ``--settings`` pointing at a nonexistent path."""


def _resolve_worker_hook_path(repo_root: Path, filename: str) -> Path:
    """Two-rung resolution for one hook file a spawned worker's generated
    ``--settings`` must reference by an EXISTING path.

    Rung 1: ``<repo_root>/.claude/hooks/<filename>`` -- the origin/dev
    checkout's own copy. Always wins when present, preserving this
    driver's pre-ladder behavior exactly for every dev checkout.

    Rung 2: ``<repo_root>/plugins/github_midwife_plugin/claude_plugin/
    coordination-hooks/hooks/<filename>`` -- the shipped fallback a born
    clone still carries even with no ``.claude/hooks/`` directory at all.
    Reads the CHECKOUT copy of the plugin tree deliberately, never the
    version-keyed ``~/.claude/plugins/cache/<marketplace>/<plugin>/
    <version>/`` install path -- immune to that path's own staleness trap
    (Seed Publish Runbook: neither ``claude plugin install`` nor
    ``claude plugin update`` re-copies changed hook content when
    ``plugin.json``'s ``version`` is unchanged).

    FAIL LOUD if neither rung resolves: never emit generated settings
    pointing at a path that does not exist on disk -- that silent failure
    (a `PreToolUse` hook naming a missing file blocks every subsequent
    tool call) is exactly the defect this ladder exists to close."""
    rung1 = repo_root / ".claude" / "hooks" / filename
    if rung1.is_file():
        return rung1
    rung2 = repo_root / _PLUGIN_HOOKS_DIR_RELATIVE / filename
    if rung2.is_file():
        return rung2
    raise WorkerHookResolutionError(
        f"required worker hook {filename!r} resolves at neither rung "
        f"probed: {rung1} (origin checkout) nor {rung2} (shipped plugin "
        "fallback). Refusing to emit spawn settings pointing at a "
        "nonexistent path.",
    )


def _resolve_worker_hook_paths(repo_root: Path) -> dict[str, Path]:
    """Resolve every hook a spawned worker's generated ``--settings`` needs,
    keyed by filename. Raises :class:`WorkerHookResolutionError`, naming
    the first file that resolves at neither rung, rather than resolving
    the rest and reporting a partial/silent result."""
    return {
        name: _resolve_worker_hook_path(repo_root, name)
        for name in _WORKER_INJECTED_HOOK_FILENAMES
    }


def _resolve_worker_hook_paths_for_spawn(repo_root: Path) -> dict[str, Path]:
    """Resolve the complete hook set or preserve spawn's public error type."""
    from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

    try:
        return _resolve_worker_hook_paths(repo_root)
    except WorkerHookResolutionError as exc:
        raise HostCannotSpawnError(str(exc)) from exc


def _resolve_session_mapping_spool_dir() -> Path | None:
    """The T1 usage-capture SessionStart hook's spool dir (ruling
    2026-08-05, Q1(a)) -- computed platform-side from ``APP_HOME`` directly
    (never derived in the hook, which stays dumb and host-agnostic), under
    ``APP_HOME``'s data dir like every other platform spool (``profile/data/
    {blobs,logs,plugin_data}``). Read from ``APP_HOME`` rather than
    ``self._cwd``: a caller overriding ``cwd`` (tests, standalone contexts)
    must not silently redirect the spool onto an unrelated path. ``None``
    when ``APP_HOME`` is unset -- the caller omits the env var entirely
    rather than exporting an empty/bogus path (mirrors the ``allowed_tools``
    conditional-export contract just above)."""
    app_home = os.environ.get("APP_HOME", "").strip()
    if not app_home:
        return None
    return Path(app_home) / "data" / "session_claude_mapping_spool"


def _resolve_heartbeat_marker_dir() -> Path | None:
    """T2's PostToolUse heartbeat hook's per-worker marker directory (seat's
    redesign ruling 2026-08-05, after the pid-32482 false-liveness defect)
    -- same declared-not-derived, ``APP_HOME``-rooted contract as
    :func:`_resolve_session_mapping_spool_dir` (a SEPARATE directory, not a
    sibling file inside the mapping spool: that spool's own drain globs
    ``*.json`` and would try to parse a stray heartbeat marker as a
    malformed mapping record). ``None`` when ``APP_HOME`` is unset, same
    conditional-export contract as every sibling resolver here."""
    app_home = os.environ.get("APP_HOME", "").strip()
    if not app_home:
        return None
    return Path(app_home) / "data" / "heartbeat_marker"


def _resolve_coordination_receipt_path() -> Path | None:
    """The owner receipt is declared from APP_HOME, never inferred from cwd."""
    app_home = os.environ.get("APP_HOME", "").strip()
    if not app_home:
        return None
    return Path(app_home) / "data/coordination-hooks/claude/installation.v1.json"


def _add_coordination_receipt_env(env: dict[str, str]) -> None:
    receipt_path = _resolve_coordination_receipt_path()
    if receipt_path is not None:
        env["AGENT_COORDINATION_RECEIPT_PATH"] = str(receipt_path)


def _authority_system_prompt(spec: Mapping[str, object]) -> str:
    """T2 authority-template (seat's design ruling 2026-08-05) -- renders
    the delegation contract for THIS spawn from the caller-supplied spec.
    Missing fields (agent_instance_id excepted -- ``spawn()`` always sets
    it before building the command) render as blank slots via ``.get(...,
    "")`` rather than raising. Imported into ``tmux_adapter.py`` (same
    convention as ``_resolve_session_mapping_spool_dir``) rather than
    duplicated -- host-agnostic, no headless-specific behavior."""
    return render_authority_delegation_contract(
        agent_instance_id=str(spec.get("agent_instance_id") or ""),
        role_class=str(spec.get("role_class") or ""),
        lane_id=str(spec.get("lane_id") or ""),
        brief_ref=str(spec.get("brief_ref") or ""),
        spawned_by_role=str(spec.get("spawned_by_role") or ""),
        unit_id=str(spec.get("unit_id") or ""),
        repository_root=str(
            spec.get("repository_root") or spec.get("worktree_path") or "",
        ),
    )


def _coerce_allowed_tools(spec: Mapping[str, object]) -> tuple[str, ...]:
    """The spawn spec's ``allowed_tools`` (§6 ruling), tolerant of a missing
    or wrong-shaped value — split out of :meth:`HeadlessHostDriver.spawn`
    to keep it under the radon cc threshold."""
    raw = spec.get("allowed_tools") or ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(t) for t in raw)
    return ()


def _pid_alive(pid: int) -> bool:
    """``kill -0`` probe — mirrors
    ``macos_coding_agent_session_plugin.bridge_tracker``'s proven pattern.
    Duplicated rather than cross-imported: adapted binding, shared contract,
    not a plugin-to-plugin dependency (same idiom as ``ClaudeSession`` vs
    ``CodexSession`` in the WS-6 prior art)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _drain_pipe(pipe: Any) -> None:
    """Read+discard a pipe in a background thread so a chatty child's stdout
    or stderr never deadlocks the process on a full OS pipe buffer (PIPE
    without a reader is a classic subprocess hang) — diagnostics are not
    captured in v1, only the safety property."""
    if pipe is None:
        return

    def _drain() -> None:
        try:
            for _ in pipe:
                pass
        except (OSError, ValueError):
            return

    threading.Thread(target=_drain, daemon=True).start()


def _write_init_event_spool_file(*, agent_instance_id: str, claude_session_id: str) -> None:
    """T1 usage-capture lane, slice D (ruling addendum 2026-08-05) -- the
    driver's OWN independent observation of this spawn's Claude session_id,
    written to the SAME file-per-firing spool the SessionStart hook uses
    (same filename shape, so the ingestion verb's existing generic loop
    picks it up with no changes). ``CAPTURE_SOURCE_INIT_EVENT`` keeps this
    witness distinguishable from the hook's own rows -- the drain path
    pairs it ONLY against ``hook:startup`` rows, never merging the two.
    Non-fatal: a missing spool dir (APP_HOME unset) or a write failure is
    logged and swallowed, mirroring the hook's own warn-and-continue
    contract -- a capture failure must never affect the spawned process."""
    spool_dir = _resolve_session_mapping_spool_dir()
    if spool_dir is None:
        return
    captured_at = datetime.now(UTC).isoformat()
    record = {
        "agent_instance_id": agent_instance_id,
        "claude_session_id": claude_session_id,
        "captured_at": captured_at,
        "capture_source": CAPTURE_SOURCE_INIT_EVENT,
    }
    try:
        spool_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{captured_at}__{agent_instance_id}__{claude_session_id}.json"
        (spool_dir / file_name).write_text(json.dumps(record))
    except OSError as exc:
        logger.warning(
            "init-event spool write failed for agent_instance_id=%s: %s", agent_instance_id, exc,
        )


def _maybe_capture_init_event(line: str, *, agent_instance_id: str) -> bool:
    """Returns True once a well-formed stream-json init event line has been
    seen (whether or not the spool write itself succeeded) -- a real init
    event fires exactly once per spawn (MEASURED shape, scratch probe
    2026-08-05: ``{"type":"system","subtype":"init",...,"session_id":...}``
    as line 1 of stdout), so the caller stops trying on later lines either
    way. A line that fails to parse or doesn't match the init shape
    returns False -- keep scanning (defensive; the protocol guarantees line
    1, but never assume silently)."""
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    if not (
        isinstance(event, dict)
        and event.get("type") == "system"
        and event.get("subtype") == "init"
    ):
        return False
    claude_session_id = str(event.get("session_id") or "")
    if claude_session_id:
        _write_init_event_spool_file(
            agent_instance_id=agent_instance_id, claude_session_id=claude_session_id,
        )
    return True


def _drain_stdout_with_init_capture(pipe: Any, *, agent_instance_id: str) -> None:
    """The stdout-never-read fix's continuous drain (the safety property),
    PLUS the slice-D init-event cross-check capture as a free side effect
    of a drain this fix already requires. Stops parsing once the init
    event has been seen (or the pipe's first lines prove uncapturable);
    draining itself never stops until EOF/close, same as :func:`_drain_pipe`.
    """
    if pipe is None:
        return

    def _drain() -> None:
        captured = False
        try:
            for line in pipe:
                if not captured:
                    captured = _maybe_capture_init_event(line, agent_instance_id=agent_instance_id)
        except (OSError, ValueError):
            return

    threading.Thread(target=_drain, daemon=True).start()


def _reap(proc: subprocess.Popen[str] | None) -> None:
    """``Popen.wait()`` so the OS reaps the zombie table entry."""
    if proc is None:
        return
    try:
        proc.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, OSError):
        return


def _sigterm_then_kill(
    pid: int, proc: subprocess.Popen[str] | None, grace_seconds: float,
) -> None:
    """SIGTERM, wait up to ``grace_seconds``, then SIGKILL — mirrors
    ``bridge_tracker._sigterm_then_kill``."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _reap(proc)
        return
    except PermissionError:
        logger.error("SIGTERM denied for headless worker pid=%d", pid)
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        # poll() when we hold the Popen (the normal case: this process is
        # the child's real parent, so a self-exited child is a zombie that
        # _pid_alive's bare kill(pid, 0) cannot distinguish from running --
        # that would burn the FULL grace_seconds on every terminate() even
        # when SIGTERM was heeded immediately). Only fall back to the raw
        # probe when there is no Popen handle at all.
        exited = proc.poll() is not None if proc is not None else not _pid_alive(pid)
        if exited:
            _reap(proc)
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.error("SIGKILL denied for headless worker pid=%d", pid)
    _reap(proc)


def _arm_watcher(
    popen_fn: Callable[..., subprocess.Popen[str]],
    solet_bin: str,
    cwd: Path,
    parent_pid: int,
    env: Mapping[str, str],
    transport: str,
) -> subprocess.Popen[str] | None:
    """Arm the registered-presence sidecar for a ``watch``-transport worker —
    what actually puts it in ``peer_list``.

    Mirrors ``codex_app_server.CodexAppServerHostDriver._start_watcher``, per
    the ruling to mirror rather than innovate, with the one ratified
    divergence: NO ``--no-spool``. Claude Code runs a real async Stop hook
    (``wake_waiter.py``) whose entire job is consuming that spool.

    Non-fatal on failure, unlike the Codex path's ``HostCannotSpawnError``:
    the worker process is already up by this point, and an unregistered worker
    — the status quo this fix improves on — still beats killing a live one.
    Logged at WARNING so it is never silent, which is precisely the failure
    mode this lane exists to eliminate.
    """
    if transport != "watch" or not solet_bin:
        return None
    argv = [
        *watch_sidecar_argv(solet_bin, agent_id="claude_code", spool=True),
        "--exit-with-parent", str(parent_pid),
    ]
    try:
        return popen_fn(
            argv, cwd=str(cwd), env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,
        )
    except OSError as exc:
        logger.warning(
            "presence watcher failed to arm for headless worker pid=%d (%s) — "
            "the worker will run UNREGISTERED: not in peer_list, unreachable "
            "by peer_send, contributing no liveness",
            parent_pid, exc,
        )
        return None


def _stop_watcher(watcher: subprocess.Popen[str] | None, grace_seconds: float) -> None:
    """Tear the presence sidecar down explicitly.

    ``--exit-with-parent`` already covers the ordinary case, but that is a
    POLL: the watcher would linger for up to one poll interval still holding
    a registry row, which dispatch reads as a live binding and sends into.
    SIGTERM is also what ``local_cli.cli._install_sigterm_unwind`` is built
    for — it unwinds so ``/close`` runs and the row is evicted SYNCHRONOUSLY,
    rather than being left behind as false liveness.
    """
    if watcher is None or watcher.poll() is not None:
        return
    _sigterm_then_kill(watcher.pid, watcher, grace_seconds)


@dataclass(slots=True)
class _TrackedHeadlessProcess:
    agent_instance_id: str
    proc: subprocess.Popen[str]
    # Registration-loss fix (2026-08-14): the registered-presence sidecar for
    # a ``watch``-transport worker, mirroring codex_app_server's own
    # ``_TrackedCodexProcess.watcher``. ``None`` on the ``mcp`` transport
    # (the bridge registers there) or when no CLI resolved.
    watcher: subprocess.Popen[str] | None = None


@dataclass(slots=True)
class _StreamJsonDriverChannel:
    """The ``DriverChannel`` (§5) for a tracked headless process — wraps the
    stdin pipe in the same stream-json envelope WS-6's ``ClaudeSession.send``
    uses (MEASURED working, ``driven_sessions.py``). Fire-and-forget: does
    not read/await the resulting turn."""

    proc: subprocess.Popen[str]

    def insert(self, text: str) -> None:
        if self.proc.stdin is None:
            return
        message = {"role": "user", "content": [{"type": "text", "text": text}]}
        line = json.dumps({"type": "user", "message": message})
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            # Fire-and-forget by contract (module docstring) — a worker that
            # died between driver_channel()'s liveness check and this write
            # (TOCTOU) gets a swallowed, logged send rather than an unmapped
            # exception escaping through the clear_session/compact_session
            # verb layer.
            logger.warning(
                "driver channel send() failed — pid=%d is no longer accepting stdin", self.proc.pid,
            )

    def submit(self) -> None:
        pass

    def send(self, text: str) -> None:
        self.insert(text)
        self.submit()


class HeadlessHostDriver:
    """The ``headless`` driver (§5): universal floor, no presentation
    layer. One real Claude Code subprocess per :meth:`spawn` call, driven
    over stream-json stdin/stdout."""

    def __init__(
        self,
        *,
        claude_bin: str | None = None,
        solet_name: str | None = None,
        solet_bin: str | None = None,
        permission_mode: str | None = None,
        transport: str | None = None,
        mcp_config_path: Path | None = None,
        cwd: Path | None = None,
        python_executable: str | None = None,
        popen_fn: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> None:
        # ``is not None`` throughout (never ``or``): an explicit empty string
        # means "explicitly blank," distinct from "not provided, resolve
        # from env/default" (``None``) — the same C3 distinction
        # ``resolve_host_driver`` already makes for ``host``. A truthy-``or``
        # chain would silently let ambient env win over a caller's explicit
        # blank override.
        self._claude_bin = (
            claude_bin if claude_bin is not None
            else shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        )
        self._solet_name = (
            solet_name if solet_name is not None
            else os.environ.get("SOLET_NAME") or ""
        )
        # Wake CLI BINARY (never solet_name). Unresolvable is non-fatal here
        # -- see _arm_watcher / _spawn_env for why it degrades, not refuses.
        # R11 (2026-08-17): resolved per read, never once here — see below.
        self._cli_resolver = WakeCliResolver(
            solet_bin, python_executable=python_executable,
        )
        self._permission_mode = (
            permission_mode if permission_mode is not None
            else os.environ.get(_ENV_PERMISSION_MODE) or ""
        )
        # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06): the
        # spec-level value (spawn_session's policy resolution, normally
        # plugin.yaml's default_fleet_transport) always wins; this
        # constructor floor only matters for a caller that bypasses
        # spawn_session entirely. Unlike permission_mode, transport is
        # never a fail-closed gate -- there is no "refuse to spawn without
        # an explicit value" posture, so an unset floor resolves to the
        # charter's own safe default ("watch") in _spawn_env/_spawn_command
        # rather than refusing.
        self._transport = transport if transport is not None else ""
        self._cwd = cwd if cwd is not None else _resolve_default_cwd()
        self._mcp_config_path = (
            mcp_config_path if mcp_config_path is not None else (self._cwd / ".mcp.json")
        )
        self._popen_fn = popen_fn
        self._grace_seconds = grace_seconds
        self._lock = RLock()
        self._processes: dict[str, _TrackedHeadlessProcess] = {}

    @property
    def _solet_bin(self) -> str:
        """Wake CLI, resolved FRESH per read — never cache it (R11, 2026-08-17).
        Rationale, ranked cases, residual: :class:`~.solet_cli.WakeCliResolver`.
        """
        return self._cli_resolver.resolve()

    def verify_config(
        self,
        *,
        permission_mode: str | None = None,
        transport: str | None = None,
        degraded_hooks_acknowledged: bool = False,
        managed_settings_paths: tuple[Path, ...] | None = None,
    ) -> list[str]:
        """Config-time, fail-closed remedies (§5) — empty means ready to
        spawn. Checked explicitly at the top of :meth:`spawn` too, so a
        caller can never bypass this by skipping the call.

        ``permission_mode`` is the per-spawn override :meth:`spawn` resolves
        from ``spec`` (§6 ruling: normally ``plugin.yaml``'s
        ``headless_permission_mode``, threaded through the dispatch spec) —
        this method has no other way to see it, since a bare
        ``driver.verify_config()`` call (e.g. a diagnostic context with no
        spec) can only ever check the constructor/env-sourced floor
        (``self._permission_mode``). Passing it here is what lets a
        per-spawn config resolution actually reach this gate instead of
        silently refusing on the floor value alone.

        ``transport`` is the same kind of per-spawn override, for the same
        reason (Dax Part 36 §36.3): :meth:`_spawn_command` only ever reads
        ``self._mcp_config_path`` when the resolved transport is ``"mcp"`` —
        a ``"watch"`` spawn passes an inline literal empty MCP config
        (``'{"mcpServers":{}}'``) and never touches the file. Requiring the
        file unconditionally refused every watch-transport spawn on a born
        clone, which ships no ``.mcp.json`` at all, for a file that spawn
        was never going to read. Resolution mirrors :meth:`_resolve_transport`
        exactly (spec-level override, then this driver's constructor/env
        floor, then the charter default ``"watch"``) so a bare call sees the
        same posture :meth:`spawn` would actually take."""
        candidates = (
            _claude_bin_remedy(self._claude_bin),
            _solet_name_remedy(self._solet_name),
            _permission_mode_remedy(permission_mode or self._permission_mode),
            _mcp_config_remedy(
                transport if transport is not None else (self._transport or "watch"),
                self._mcp_config_path,
            ),
            _managed_policy_remedy(
                managed_settings_paths,
                degraded_hooks_acknowledged=degraded_hooks_acknowledged,
            ),
        )
        return [remedy for remedy in candidates if remedy is not None]

    def capability_report(self) -> dict[str, object]:
        return _headless_capability_report()

    def _spawn_env(
        self, *, agent_instance_id: str, agent_session_id: str, label: str,
        allowed_tools: tuple[str, ...], transport: str,
        context_gauge_reporter_path: Path, cwd: Path | None = None,
    ) -> dict[str, str]:
        env = dict(os.environ)
        env["SOLET_NAME"] = self._solet_name
        env["AGENT_IDENTITY"] = "claude_code"
        env["AGENT_INSTANCE_ID"] = agent_instance_id
        env["AGENT_SESSION_ID"] = agent_session_id
        env["AGENT_SESSION_LABEL"] = label
        env["AGENT_CONTEXT_GAUGE_REPORTER_PATH"] = str(context_gauge_reporter_path.resolve())
        # Deaf-wake fix (2026-08-08): MUST be the wake CLI's own binary,
        # never self._solet_name (the solet INSTANCE name, e.g. "mysolet") --
        # `which <instance-name>` fails, `which solet-bridge` resolves.
        # Registration-loss fix (2026-08-14): the 2026-08-08 fix used the
        # bare command NAME, which only works when PATH can resolve it. A
        # materialized blue-green release runs with a minimal PATH excluding
        # its own venv/bin, so wake_waiter.py's
        # `subprocess.run([$AGENT_WAKE_CLI, "wake"])` and
        # heartbeat_report_alive.py's bare `["solet-bridge", "call", ...]` both died
        # with FileNotFoundError and, being non-fatal by design, died
        # SILENTLY. Absolute binary + PATH prepend closes both halves.
        expose_worker_cli(env, self._solet_bin)
        # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06):
        # caller-resolved (spec's policy-filled value, or this driver's
        # constructor/charter-default floor -- see spawn()) -- never
        # hardcoded here. This is the SAME env var the rename skill, every
        # spawned worker's hook guards, and wake_waiter.js all read
        # independently (declared, never probed).
        env["FLEET_TRANSPORT"] = transport
        # Operator ruling, 2026-08-03 ("we don't have any restrictions
        # now"): the deny-hook is UNARMED by default -- only set when an
        # allowlist is actually configured (non-empty). This REVERSES the
        # earlier "always gate, even with an empty list" design from the
        # same night; the hook's opt-in contract (unset env var = gate
        # off) already supported this, this driver just needed to stop
        # forcing it on. The mechanism stays landed as shelf capability
        # (armed per-work_class via plugin.yaml's work_class_tool_allowlists
        # whenever usage data argues for it), just not exercised by default.
        if allowed_tools:
            env["FLEET_HEADLESS_TOOL_ALLOWLIST"] = ",".join(allowed_tools)
        # T1 usage-capture lane (ruling 2026-08-05, Q1(a)): declared here,
        # never derived in the hook. Omitted entirely when APP_HOME is
        # unset (standalone/test contexts) -- the hook's own non-fatal
        # contract already handles a missing env var, so there is nothing
        # to export.
        spool_dir = _resolve_session_mapping_spool_dir()
        if spool_dir is not None:
            env["ANANTA_SESSION_MAPPING_SPOOL_DIR"] = str(spool_dir)
        # T2 heartbeat lane (seat's redesign ruling 2026-08-05): same
        # declared-not-derived, conditional-export contract.
        heartbeat_dir = _resolve_heartbeat_marker_dir()
        if heartbeat_dir is not None:
            env["AGENT_HEARTBEAT_MARKER_DIR"] = str(heartbeat_dir)
        _add_coordination_receipt_env(env)
        return _with_worktree_pythonpath(env, cwd or self._cwd)

    def _hook_settings_json(
        self, resolved_hooks: Mapping[str, Path] | None = None,
    ) -> str:
        """The ``--settings`` JSON injecting the PreToolUse allowlist gate
        (§6 permission-mode ruling, 2026-08-03), the SessionStart
        usage-capture hook (T1 lane, ruling 2026-08-05), the PostToolUse
        heartbeat hook (T2, seat's redesign ruling 2026-08-05) AND the
        PostToolUse rotation-due watch hook (rotation-systematization P2
        slice B, ruling 2, 2026-08-07 -- both share the SAME PostToolUse
        group, heartbeat first), the Agent/Task tool deny (capability-
        tier guardrail redesign, fleet-watch-transport-migration phase 2
        slice 1+5, 2026-08-06), and (deaf-wake fix, 2026-08-08) the Stop
        wake_waiter.py hook plus the SessionStart/UserPromptSubmit
        check_messages_reminder.py, role_binding_reminder.py, and
        step_zero_reminder.py hooks -- all scoped to THIS spawned worker
        only, never the shared ``.claude/settings.json`` (that would
        double-fire the wake hook for the seat, which already gets the
        equivalent JS hooks via the user-scope coordination-hooks@<solet>
        plugin). Merges with (does not replace) whatever
        ``--setting-sources project`` loads (e.g. the git-controller
        gate), per the standing "--settings MERGES" trap note.

        The deny rule is carried HERE too, not just in this checkout's own
        project-scope ``.claude/settings.json``, because ``--setting-sources
        project`` resolves relative to whatever ``cwd`` the spawned worker
        actually runs in -- a non-checkout spawn (a different clone, a
        scratch cwd) would not inherit this origin's own tracked settings
        file at all. Empirically verified: ``permissions.deny: ["Agent"]``
        removes the tool from the session's own available-tool listing
        entirely (a live scratch probe, 2026-08-06 -- a driven session asked
        to use it reported ``ToolSearch`` returning no match, not a refused
        call), the same clean-absence contract ``CLAUDE_CODE_DISABLE_
        ADVISOR_TOOL`` uses for the advisor. ``"Task"`` is carried alongside
        the confirmed-live ``"Agent"`` as a defensive alias only (this
        repo's own docs use "Agent/Task" interchangeably, suggesting past
        version drift in the tool's registered name); an unmatched deny
        entry is inert, never harmful, so listing both costs nothing."""
        # R4 Package C (2026-08-10): resolved via the two-rung ladder
        # (origin checkout, then this plugin's shipped fallback copy) --
        # see _resolve_worker_hook_paths's own docstring. Raises
        # WorkerHookResolutionError, converted to HostCannotSpawnError by
        # the caller (spawn()), if any file resolves at neither rung --
        # never silently emits settings pointing at a missing path.
        resolved_hooks = resolved_hooks or _resolve_worker_hook_paths(self._cwd)
        allowlist_hook_path = resolved_hooks["headless_tool_allowlist_gate.py"]
        capture_hook_path = resolved_hooks["capture_session_mapping.py"]
        heartbeat_hook_path = resolved_hooks["heartbeat_report_alive.py"]
        rotation_due_hook_path = resolved_hooks["rotation_due_watch.py"]
        # Deaf-wake fix (2026-08-08): project-vendored Python ports of
        # coordination-hooks@<solet>'s four JS hooks, wired here (this
        # adapter's own generated --settings blob) rather than the shared
        # project-scope .claude/settings.json, specifically so this does
        # not double-fire the wake for the seat -- the seat already gets
        # these via the user-scope plugin, which --setting-sources project
        # excludes for a spawned worker in the first place (that exclusion
        # is the root cause this fix addresses; see
        # workbench/2026-08-08_deaf_wake_diagnosis_findings_rotation-impl.md).
        # Mirrors tmux_adapter.py._spawn_command's identical wiring exactly.
        wake_hook_path = resolved_hooks["wake_waiter.py"]
        check_messages_hook_path = resolved_hooks["check_messages_reminder.py"]
        step_zero_hook_path = resolved_hooks["step_zero_reminder.py"]
        role_binding_hook_path = resolved_hooks["role_binding_reminder.py"]
        settings = {
            "permissions": {
                "deny": list(_HEADLESS_DENIED_TOOLS),
            },
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": f"python3 {allowlist_hook_path}"}]},
                ],
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": f"python3 {capture_hook_path}"}]},
                    {
                        # Matches coordination-hooks@<solet>'s own hooks.json
                        # matcher for this event exactly (fidelity, not a
                        # new choice) -- check_messages_reminder.py also
                        # fires here (in addition to UserPromptSubmit
                        # below) matching the plugin's own dual wiring.
                        "matcher": "startup|resume|clear",
                        "hooks": [
                            {"type": "command", "command": f"python3 {check_messages_hook_path}"},
                            {"type": "command", "command": f"python3 {role_binding_hook_path}"},
                        ],
                    },
                ],
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": f"python3 {step_zero_hook_path}"}]},
                    {"hooks": [{"type": "command", "command": f"python3 {check_messages_hook_path}"}]},  # noqa: E501
                ],
                "PostToolUse": [
                    {"hooks": [
                        {"type": "command", "command": f"python3 {heartbeat_hook_path}"},
                        {"type": "command", "command": f"python3 {rotation_due_hook_path}"},
                    ]},
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {wake_hook_path}",
                                # Matches wake_waiter.js's own required
                                # shape exactly -- without this the Stop
                                # hook applies Claude Code's short default
                                # timeout instead of blocking indefinitely
                                # for a peer-message wake.
                                "asyncRewake": True,
                                "timeout": 86400,
                            },
                        ],
                    },
                ],
            },
        }
        return json.dumps(settings)

    def _spawn_command(
        self, spec: Mapping[str, object], *, label: str, transport: str,
        resolved_hooks: Mapping[str, Path] | None = None,
    ) -> list[str]:
        # spec-level permission_mode (§6 ruling, resolved from plugin.yaml's
        # headless_permission_mode at the platform_process shim) takes
        # priority; self._permission_mode (the constructor/env-sourced
        # value) is the floor a caller that bypasses the shim still gets --
        # defense in depth, same posture as verify_config()'s own check.
        permission_mode = str(spec.get("permission_mode") or "") or self._permission_mode
        cmd = [
            self._claude_bin,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose", "--include-hook-events",
            "--permission-mode", permission_mode,
            # project-only: excludes BOTH user-scope (this operator's own
            # ~/.claude/settings.json carries permissions.defaultMode:
            # bypassPermissions) and local-scope (.claude/settings.local.json
            # carries a broad legacy Bash(...) allow list) -- verified
            # empirically, 2026-08-03: a spawned worker must never inherit
            # either. Confirmed live: --allowedTools alone does NOT restrict
            # anything (additive only); only --setting-sources scoping +
            # the injected PreToolUse hook actually enforce.
            "--setting-sources", "project",
            "--settings", self._hook_settings_json(resolved_hooks),
            "--name", label,
        ]
        # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06):
        # "mcp" gets the real MCP bridge config; "watch" gets an EXPLICIT
        # empty MCP config, matching the WS-6-verified precedent
        # (--strict-mcp-config '{"mcpServers":{}}') rather than omitting the
        # flags -- omitting them risks Claude Code's own ambient .mcp.json
        # discovery silently re-attaching MCP under --setting-sources
        # project. Never both, never neither -- exactly one MCP posture
        # ships in every spawn argv, declared by the resolved transport.
        if transport == "mcp":
            cmd += ["--mcp-config", str(self._mcp_config_path), "--strict-mcp-config"]
        else:
            cmd += ["--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
        # Dev-channel loading is orthogonal to MCP-vs-watch -- it is the
        # seat's/every worker's real-time push-notification surface
        # (distinct from MCP tool config; the phase-2 scope ruling names
        # this explicitly as a separate mechanism to verify at seat
        # migration, phase 4 -- not something slice 1 changes).
        #
        # DELIBERATELY UNCONDITIONAL HERE, unlike tmux_adapter._spawn_command
        # (§39.1/§40.1, decided 2026-08-10). The tmux driver must omit this
        # flag on a third-party provider because the flag ARMS a PTY confirm
        # loop whose no-prompt branch kills a fully-booted worker. This driver
        # has no such loop -- the confirmation prompt is PTY-only and a
        # piped-stdin spawn never sees it -- so the flag is merely inert here,
        # with no failure mode to fix. Mirroring the omission would buy
        # symmetry at the cost of (a) a second provider-detection site to keep
        # in lock-step with the tmux predicate when the per-spawn provider
        # overlay lands, and (b) an unmeasurable behaviour change on a working
        # boot path: we have no third-party endpoint in this checkout, so we
        # cannot observe what a headless spawn does with the flag on one.
        # Changing a green path on inference is the more expensive error, so
        # this stays as-is until a measurement or an adopter report says
        # otherwise. If that arrives, import the tmux predicate rather than
        # writing a second one.
        cmd += ["--dangerously-load-development-channels", f"server:{self._solet_name}"]
        model = str(spec.get("model") or "")
        if model:
            cmd += ["--model", model]
        effort = str(spec.get("effort") or "")
        if effort:
            cmd += ["--effort", effort]
        # T2 authority-template (seat's design ruling 2026-08-05): ON by
        # default for every spawn_session-managed spawn, not a lane
        # opt-in -- authority-at-first-contact is fleet doctrine. Missing
        # spec fields (e.g. a direct spawn() call that bypasses
        # spawn_session) render as blank slots rather than skipping the
        # flag entirely -- every spawned worker gets the trusted-surface
        # anchor, never a silently-degraded one.
        cmd += ["--append-system-prompt", _authority_system_prompt(spec)]
        return cmd

    def spawn(self, spec: Mapping[str, object]) -> str:
        # Deferred: session_hosts imports THIS module to populate its
        # driver registry, so a module-level import here would be
        # circular (breaks whichever module the caller imports first).
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        # Resolved ONCE, here, and threaded into verify_config (the
        # .mcp.json existence gate, transport-scoped since Dax Part 36
        # §36.3), _spawn_env (the FLEET_TRANSPORT env var), and
        # _spawn_command (the MCP-config argv posture).
        transport = _resolve_spawn_transport(spec, self._transport)
        degraded_ok = bool(spec.get("degraded_hooks_acknowledged"))
        remedies = self.verify_config(
            permission_mode=str(spec.get("permission_mode") or ""),
            transport=transport,
            degraded_hooks_acknowledged=degraded_ok,
        )
        if remedies:
            raise HostCannotSpawnError("; ".join(remedies))
        agent_instance_id = str(spec.get("agent_instance_id") or "")
        if not agent_instance_id:
            raise HostCannotSpawnError(
                "spawn spec is missing agent_instance_id — the headless "
                "driver cannot register the spawned process under the "
                "ledger's identity without it.",
            )
        _announce_managed_policy(
            degraded_ok, agent_instance_id=agent_instance_id,
            host="headless", denied_tools=_HEADLESS_DENIED_TOOLS,
        )
        label = _resolve_local_label(spec, agent_instance_id=agent_instance_id)
        # Minted exactly ONCE, here — never re-derived elsewhere (two
        # evaluations of an identity expression is two identities).
        agent_session_id = f"ases-{agent_instance_id}"
        cwd = _resolve_spawn_cwd(spec, self._cwd)
        resolved_hooks = _resolve_worker_hook_paths_for_spawn(cwd)
        env = self._spawn_env(
            agent_instance_id=agent_instance_id, agent_session_id=agent_session_id,
            label=label, allowed_tools=_coerce_allowed_tools(spec), transport=transport,
            context_gauge_reporter_path=resolved_hooks["rotation_due_watch.py"], cwd=cwd,
        )
        cmd = self._spawn_command(
            spec, label=label, transport=transport, resolved_hooks=resolved_hooks,
        )

        try:
            proc = self._popen_fn(
                cmd, cwd=str(cwd), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, start_new_session=True,
            )
        except OSError as exc:
            raise HostCannotSpawnError(f"subprocess.Popen raised: {exc}") from exc

        host_ref = str(proc.pid)
        watcher = _arm_watcher(
            self._popen_fn, self._solet_bin, cwd, proc.pid, env, transport,
        )
        with self._lock:
            self._processes[host_ref] = _TrackedHeadlessProcess(
                agent_instance_id=agent_instance_id, proc=proc, watcher=watcher,
            )
        _drain_stdout_with_init_capture(proc.stdout, agent_instance_id=agent_instance_id)
        _drain_pipe(proc.stderr)
        return host_ref

    def alive(self, host_ref: str) -> bool:
        try:
            pid = int(host_ref)
        except ValueError:
            return False
        with self._lock:
            tracked = self._processes.get(host_ref)
        if tracked is not None:
            # This driver is the child's real parent, so a self-exited
            # child sits as a zombie -- kill(pid, 0) still succeeds until
            # something waitpid()s it. poll() reaps it as a side effect and
            # is the only correct liveness check for a TRACKED process.
            return tracked.proc.poll() is None
        return _pid_alive(pid)

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        with self._lock:
            tracked = self._processes.pop(host_ref, None)
        try:
            pid = int(host_ref)
        except ValueError:
            return
        if tracked is not None and tracked.proc.stdin is not None:
            with contextlib.suppress(OSError):
                tracked.proc.stdin.close()
        grace = grace_seconds if grace_seconds > 0 else self._grace_seconds
        _sigterm_then_kill(pid, tracked.proc if tracked is not None else None, grace)
        if tracked is not None:
            _stop_watcher(tracked.watcher, grace)

    def driver_channel(self, host_ref: str) -> _StreamJsonDriverChannel | None:
        """Returns a live :class:`_StreamJsonDriverChannel` when this
        process's in-memory map still tracks ``host_ref`` AND the tracked
        pid is still alive (``None`` post-restart, for a host_ref this
        driver never spawned, or for a worker that has since died on its
        own — a stale-but-tracked pid would otherwise hand back a channel
        whose ``send()`` can only fail) — the exact case
        ``clear_session``/``compact_session`` (AMEND 5b) check ``is None``
        against to raise ``unsupported_on_host``."""
        with self._lock:
            tracked = self._processes.get(host_ref)
        if tracked is None or tracked.proc.poll() is not None:
            return None
        return _StreamJsonDriverChannel(proc=tracked.proc)

    def shutdown(self) -> None:
        """Terminate every tracked worker — wired into the plugin's stop
        path (``plugin.py.stop_services``) so a graceful shutdown/restart
        never leaves an orphaned worker burning tokens with nothing
        tracking it (mirrors ``BridgeTracker.shutdown``)."""
        with self._lock:
            items = list(self._processes.items())
            self._processes.clear()
        for host_ref, tracked in items:
            _stop_watcher(tracked.watcher, self._grace_seconds)
            try:
                pid = int(host_ref)
            except ValueError:
                continue
            _sigterm_then_kill(pid, tracked.proc, self._grace_seconds)


__all__ = [
    "DEFAULT_TERMINATE_GRACE_SECONDS",
    "HeadlessHostDriver",
]
