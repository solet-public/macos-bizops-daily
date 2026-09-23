#!/usr/bin/env python3
"""Unit smoke for the D1 ``headless`` HostDriver (§5) —
``headless_adapter.py``. Two levels of fake, deliberately kept separate:

  - a RECORDS-ONLY fake ``popen_fn`` (no real process) to assert the exact
    command/env content ``spawn()`` builds — the identity wiring
    (AGENT_INSTANCE_ID/AGENT_SESSION_ID/AGENT_SESSION_LABEL) that
    ``backfill_registration`` depends on to find the right ledger row;
  - a REAL-SUBPROCESS fake ``popen_fn`` (spawns a real, short-lived,
    harmless child — ``sleep``/``python3 -c ...`` — ignoring the incoming
    cmd/env) to exercise alive()/terminate()/shutdown()'s actual OS-level
    signal + reap mechanics against a real pid, without ever invoking the
    real ``claude`` binary.

Never spawns a real Claude Code process — this is a unit smoke, not an
integration test against the live CLI.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/headless_adapter_smoke.py
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import headless_adapter  # noqa: E402
from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY,
    _HEADLESS_DENIED_TOOLS,
    _MANAGED_SETTINGS_PATHS,
    _WORKER_INJECTED_HOOK_FILENAMES,
    HeadlessHostDriver,
    WorkerHookResolutionError,
    _managed_policy_voids_permission_rules,
    _resolve_default_cwd,
    _resolve_worker_hook_path,
    _resolve_worker_hook_paths,
    _StreamJsonDriverChannel,
    _warn_permission_denies_inert,
)
from agent_messaging_plugin.session_hosts import HostCannotSpawnError  # noqa: E402

_passed = 0
_failed: list[str] = []


# Side-channel echo wait. Overridable ONLY so the red-first probe can force the
# timeout path (see _ECHO_WAIT_ENV); production runs never set it.
_ECHO_WAIT_ENV = "HEADLESS_SMOKE_ECHO_WAIT_S"
_ECHO_WAIT_SECONDS = float(os.environ.get(_ECHO_WAIT_ENV, "30"))


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeProc:
    """Records-only stand-in for ``subprocess.Popen`` — no real process."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.stdin = None
        self.stdout = None
        self.stderr = None


def _executable_stub(tmp_dir: Path) -> str:
    """A real, executable file — enough for ``os.access(path, os.X_OK)``,
    never actually run in the records-only tests."""
    stub = tmp_dir / "fake-claude"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return str(stub)


def _stub_worker_hook_files(tmp_dir: Path) -> None:
    """R4 Package C (2026-08-10): populate rung 1 (``.claude/hooks/``) with
    a stub for every file the worker-hook resolution ladder requires --
    matching a real dev checkout's own shape (rung 1 always present), and
    preserving every pre-ladder test's assumption that a spawn's generated
    settings simply reference SOME path under ``tmp_dir/.claude/hooks/``.
    Dedicated ladder tests below construct their OWN, more deliberate
    fixture layouts instead of calling this helper."""
    hooks_dir = tmp_dir / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in _WORKER_INJECTED_HOOK_FILENAMES:
        (hooks_dir / name).write_text("#!/usr/bin/env python3\n")


def _configured_driver(
    tmp_dir: Path, *, popen_fn: Any = None,
) -> HeadlessHostDriver:
    mcp_config = tmp_dir / ".mcp.json"
    mcp_config.write_text("{}")
    _stub_worker_hook_files(tmp_dir)
    # Injected, never resolved from the ambient environment: the CLI/PATH and
    # presence-sidecar assertions would otherwise pass or fail depending on
    # whether the machine running the gate happens to have a `solet-bridge` on PATH
    # or beside its interpreter.
    solet_bin = tmp_dir / "venv" / "bin" / "solet-bridge"
    solet_bin.parent.mkdir(parents=True, exist_ok=True)
    solet_bin.write_text("#!/bin/sh\nexit 0\n")
    solet_bin.chmod(0o755)
    kwargs: dict[str, Any] = {
        "claude_bin": _executable_stub(tmp_dir),
        "solet_name": "testhom",
        "solet_bin": str(solet_bin),
        "permission_mode": "bypassPermissions",
        "mcp_config_path": mcp_config,
        "cwd": tmp_dir,
        "python_executable": str(solet_bin.parent / "python3"),
    }
    if popen_fn is not None:
        kwargs["popen_fn"] = popen_fn
    return HeadlessHostDriver(**kwargs)


def _restore_env(key: str, val: str | None) -> None:
    if val is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = val


_APP_HOME_DERIVED_ENV_FAMILY = (
    "APP_HOME", "ANANTA_SESSION_MAPPING_SPOOL_DIR", "AGENT_HEARTBEAT_MARKER_DIR",
    "AGENT_COORDINATION_RECEIPT_PATH",
)


def _pop_env_family() -> dict[str, str | None]:
    """Pop the whole APP_HOME-derived env-var family, returning prior values
    for :func:`_restore_env_family` to put back. Runner-independence fix
    (fleet-watch-transport-migration phase 2 slice 1+5, 2026-08-06): a
    session spawned BY a host adapter (this smoke's own runner, if run from
    a tmux-adapter-spawned shell -- live-measured 2026-08-06) inherits
    ANANTA_SESSION_MAPPING_SPOOL_DIR/AGENT_HEARTBEAT_MARKER_DIR from ITS OWN
    spawn. ``_spawn_env`` builds its returned dict from ``dict(os.environ)``
    (the whole parent env, not a fresh dict), so popping only APP_HOME left
    those two leaking straight through into a spawned worker's env,
    producing a false red in this smoke's own 'unset -> entirely absent'
    legs -- confirmed a runner-environment artifact, not a production
    defect (the real headless-adapter host process is the platform's own
    ``ananta.cli`` launch, which does not inherit this family). Popping the
    full family makes the gate read correctly from an operator-launched
    shell AND from an adapter-spawned worker's own shell alike."""
    prior = {k: os.environ.get(k) for k in _APP_HOME_DERIVED_ENV_FAMILY}
    for k in _APP_HOME_DERIVED_ENV_FAMILY:
        os.environ.pop(k, None)
    return prior


def _restore_env_family(prior: dict[str, str | None]) -> None:
    for k, v in prior.items():
        _restore_env(k, v)


def test_resolve_default_cwd_prefers_app_home_git_checkout() -> None:
    """Regression guard for a live defect: a deployed colour's own OS cwd
    (observed: ~/.ananta/runtime, a pure state/spool dir) has no guaranteed
    relationship to the checkout -- APP_HOME's parent does, when it's a real
    git checkout. Mirrors seed_factory_plugin's
    assemble_repo_root_resolution_smoke.py case 1."""
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "clone"
        (clone / ".git").mkdir(parents=True)
        (clone / "ananta").mkdir()
        (clone / "profile").mkdir()
        orig = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = str(clone / "profile")
        try:
            _check(
                _resolve_default_cwd() == clone.resolve(),
                "APP_HOME's parent (a real git checkout) is preferred over Path.cwd()",
            )
        finally:
            _restore_env("APP_HOME", orig)


def test_resolve_default_cwd_falls_back_when_app_home_unset() -> None:
    orig = os.environ.get("APP_HOME")
    os.environ.pop("APP_HOME", None)
    try:
        _check(
            _resolve_default_cwd() == Path.cwd(),
            "APP_HOME unset -> falls back to Path.cwd() (standalone/test-driver case)",
        )
    finally:
        _restore_env("APP_HOME", orig)


def test_resolve_default_cwd_falls_back_when_app_home_not_a_checkout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "plain"
        (plain / "profile").mkdir(parents=True)  # no .git, no ananta/
        orig = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = str(plain / "profile")
        try:
            _check(
                _resolve_default_cwd() == Path.cwd(),
                "APP_HOME's parent lacking .git/ananta -> falls back to Path.cwd(), "
                "never trusts an unvalidated APP_HOME blindly",
            )
        finally:
            _restore_env("APP_HOME", orig)


def test_driver_uses_resolved_default_cwd_when_none_passed() -> None:
    """The bug this whole slice guards against: HeadlessHostDriver's cwd=None
    default must route through _resolve_default_cwd(), not a bare Path.cwd()."""
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "clone2"
        (clone / ".git").mkdir(parents=True)
        (clone / "ananta").mkdir()
        (clone / "profile").mkdir()
        orig = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = str(clone / "profile")
        try:
            driver = HeadlessHostDriver(
                claude_bin=_executable_stub(Path(tmp)),
                solet_name="testhom",
                permission_mode="bypassPermissions",
            )
            _check(
                driver._cwd == clone.resolve(),  # noqa: SLF001 -- testing the resolved default directly
                "a HeadlessHostDriver constructed with cwd=None resolves its "
                "cwd from APP_HOME's parent, not the process's bare Path.cwd()",
            )
        finally:
            _restore_env("APP_HOME", orig)


# ─── R4 Package C (2026-08-10): worker hook resolution ladder ────────────


def test_resolve_worker_hook_path_prefers_rung1_when_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        rung1 = repo_root / ".claude" / "hooks" / "wake_waiter.py"
        rung1.parent.mkdir(parents=True)
        rung1.write_text("rung1")
        rung2 = repo_root / "plugins" / "github_midwife_plugin" / "claude_plugin" / "coordination-hooks" / "hooks" / "wake_waiter.py"
        rung2.parent.mkdir(parents=True)
        rung2.write_text("rung2")
        resolved = _resolve_worker_hook_path(repo_root, "wake_waiter.py")
        _check(resolved == rung1, "rung 1 (origin checkout) wins when both rungs resolve")


def test_resolve_worker_hook_path_falls_back_to_rung2() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        rung2 = repo_root / "plugins" / "github_midwife_plugin" / "claude_plugin" / "coordination-hooks" / "hooks" / "wake_waiter.py"
        rung2.parent.mkdir(parents=True)
        rung2.write_text("rung2")
        resolved = _resolve_worker_hook_path(repo_root, "wake_waiter.py")
        _check(
            resolved == rung2,
            "rung 2 (shipped plugin fallback) resolves when rung 1 is absent -- "
            "the born-clone case, no .claude/hooks/ directory at all",
        )


def test_resolve_worker_hook_path_fails_loud_when_neither_rung_resolves() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        raised = False
        try:
            _resolve_worker_hook_path(repo_root, "wake_waiter.py")
        except WorkerHookResolutionError as exc:
            raised = True
            _check(
                "wake_waiter.py" in str(exc) and str(repo_root) in str(exc),
                "the error names the missing file and probes both rung paths, "
                "never a silent/opaque failure",
            )
        _check(raised, "neither rung resolving raises WorkerHookResolutionError, never returns a guess")


def test_resolve_worker_hook_paths_resolves_every_required_filename() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        hooks_dir = repo_root / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        for name in _WORKER_INJECTED_HOOK_FILENAMES:
            (hooks_dir / name).write_text("stub")
        resolved = _resolve_worker_hook_paths(repo_root)
        _check(
            set(resolved) == set(_WORKER_INJECTED_HOOK_FILENAMES),
            "every declared worker-injected hook filename is resolved, none dropped or invented",
        )
        _check(
            all(path.is_file() for path in resolved.values()),
            "every resolved path actually exists on disk",
        )


def test_spawn_refuses_when_a_worker_hook_resolves_at_neither_rung() -> None:
    """The end-to-end fail-loud contract: a spawn() whose cwd has NEITHER
    rung populated for even one required file must refuse via
    HostCannotSpawnError (never emit generated settings referencing a
    missing path), and the underlying WorkerHookResolutionError's detail
    must survive into that error's message."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        mcp_config = tmp_dir / ".mcp.json"
        mcp_config.write_text("{}")
        # Deliberately NOT calling _stub_worker_hook_files -- this is the
        # one test that wants the ladder to find nothing at either rung.
        driver = HeadlessHostDriver(
            claude_bin=_executable_stub(tmp_dir),
            solet_name="testhom",
            permission_mode="bypassPermissions",
            mcp_config_path=mcp_config,
            cwd=tmp_dir,
        )
        raised = False
        try:
            driver.spawn({"agent_instance_id": "agi-ladder-refuse"})
        except HostCannotSpawnError as exc:
            raised = True
            _check(
                "resolves at neither rung" in str(exc),
                "HostCannotSpawnError carries the underlying ladder failure's own detail",
            )
        _check(raised, "spawn() refuses via HostCannotSpawnError when a required worker hook is unresolvable")


def test_verify_config_remedies_are_independent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        unconfigured = HeadlessHostDriver(
            claude_bin="/nonexistent/claude", solet_name="", permission_mode="",
            mcp_config_path=tmp_dir / "missing.json", cwd=tmp_dir,
        )
        # transport="mcp" is what makes the MCP-config remedy reachable at
        # all (Dax Part 36 §36.3) -- see the dedicated watch/mcp legs below.
        remedies = unconfigured.verify_config(transport="mcp")
        _check(
            len(remedies) == 4,
            f"all 4 remedies fire when nothing is configured, mcp transport (got {len(remedies)})",
        )
        _check(
            any("claude" in r for r in remedies)
            and any("SOLET_NAME" in r for r in remedies)
            and any("permission mode" in r for r in remedies)
            and any("MCP config" in r for r in remedies),
            "each remedy names its own specific gap",
        )
        configured = _configured_driver(tmp_dir)
        _check(
            configured.verify_config(transport="mcp") == [],
            "a fully-configured driver has zero remedies",
        )


def _write_policy(path: Path, body: object) -> Path:
    """A POPULATED managed-settings fixture. There is no such file on this
    machine, and an absent-file probe verifies nothing about the populated
    case — so every leg below drives the reader with a real file it wrote."""
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_managed_policy_hooks_plugin_only_refuses_spawn() -> None:
    """W4A item 2 (#8 §43.1): the adopter's exact policy shape produces an
    attributable refusal instead of a worker that comes up deaf."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        policy = _write_policy(
            tmp_dir / "managed-settings.json",
            {"strictPluginOnlyCustomization": ["hooks"]},
        )
        remedies = driver.verify_config(
            transport="watch", managed_settings_paths=(policy,),
        )
        _check(len(remedies) == 1, f"exactly one remedy fires (got {remedies!r})")
        _check(
            "strictPluginOnlyCustomization" in remedies[0] and str(policy) in remedies[0],
            "the remedy names the offending key AND the file it read it from",
        )
        _check(
            "degraded_hooks_acknowledged" in remedies[0],
            "and names the escape hatch, so the refusal is actionable",
        )


def test_default_policy_paths_include_the_remote_pulled_location() -> None:
    """The DEFAULT search list must include ~/.claude/remote-settings.json.

    Measured on an affected machine: a managed policy can be pulled remotely to
    ``~/.claude/remote-settings.json``, and on such a machine the macOS system
    path DOES NOT EXIST at all. Searching only the system locations therefore
    reported "no managed policy in force" on exactly the deployments where one
    IS in force — a silent fail-open.

    Why no existing leg caught it: every other test in this block INJECTS its
    own ``managed_settings_paths`` fixture, so none of them ever exercises the
    default tuple. A defect that is an omission from a default is invisible to
    tests that always override that default — which is why this leg asserts on
    the constant itself rather than on behaviour driven by a fixture.
    """
    names = [(p.parent.name, p.name) for p in _MANAGED_SETTINGS_PATHS]
    _check(
        (".claude", "remote-settings.json") in names,
        f"the remote-pulled policy location is searched by default (got {names!r})",
    )
    _check(
        names and names[0] == (".claude", "remote-settings.json"),
        "and it is searched FIRST — the only location a real affected machine "
        "has been observed to use",
    )
    _check(
        any(p.name == "managed-settings.json" for p in _MANAGED_SETTINGS_PATHS),
        "without dropping the documented system locations",
    )


def test_managed_policy_without_hooks_key_is_not_a_remedy() -> None:
    """The discriminating leg: a policy file that exists and restricts
    something ELSE must not refuse the spawn. Without this, the check would
    fire on the mere presence of a managed policy."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        policy = _write_policy(
            tmp_dir / "managed-settings.json",
            {"strictPluginOnlyCustomization": ["mcpServers"], "hooks": {}},
        )
        _check(
            driver.verify_config(transport="watch", managed_settings_paths=(policy,)) == [],
            "a managed policy that does NOT list 'hooks' plugin-only is no remedy",
        )


def test_managed_policy_acknowledged_degraded_proceeds() -> None:
    """W4A item 3: the explicit opt-out turns the refusal off."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        policy = _write_policy(
            tmp_dir / "managed-settings.json",
            {"strictPluginOnlyCustomization": ["hooks"]},
        )
        _check(
            driver.verify_config(
                transport="watch", managed_settings_paths=(policy,),
                degraded_hooks_acknowledged=True,
            ) == [],
            "an acknowledged-degraded spawn is allowed through the preflight",
        )


def test_managed_policy_malformed_file_fails_open() -> None:
    """Deliberately the OPPOSITE failure direction from the spawn refusal,
    and the reason is evidential: an unparseable policy file is not evidence
    that hooks are stripped, so refusing on it would be a refusal we cannot
    justify. The registration watchdog catches the consequence regardless."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        policy = tmp_dir / "managed-settings.json"
        policy.write_text("{not json at all", encoding="utf-8")
        _check(
            driver.verify_config(transport="watch", managed_settings_paths=(policy,)) == [],
            "a malformed policy file fails OPEN (and logs), never blocks the spawn",
        )


def test_managed_policy_absent_file_is_silent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        _check(
            driver.verify_config(
                transport="watch", managed_settings_paths=(tmp_dir / "nope.json",),
            ) == [],
            "no policy file -> no remedy (the ordinary machine, including this one)",
        )


def test_verify_config_mcp_config_required_only_for_mcp_transport() -> None:
    """Regression guard for Dax Part 36 §36.3: verify_config() used to
    require .mcp.json unconditionally, even though _spawn_command() only
    ever reads self._mcp_config_path when the resolved transport is 'mcp'
    -- a 'watch' spawn passes an inline literal empty MCP config
    ('{"mcpServers":{}}') and never touches the file. A born clone ships
    no .mcp.json at all, so every watch-transport spawn (the charter
    default) refused for a file it was never going to read. Fails if the
    exists() check reverts to firing regardless of transport."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = HeadlessHostDriver(
            claude_bin=_executable_stub(tmp_dir), solet_name="testhom",
            permission_mode="bypassPermissions",
            mcp_config_path=tmp_dir / "missing.mcp.json",  # never created -- born-clone shape
            cwd=tmp_dir,
        )
        _check(
            not (tmp_dir / "missing.mcp.json").exists(),
            "precondition: .mcp.json genuinely absent, born-clone shape",
        )
        _check(
            driver.verify_config(transport="watch") == [],
            "watch transport never reads .mcp.json -- verify_config must not "
            "refuse a spawn for its absence",
        )
        _check(
            driver.verify_config() == [],
            "bare verify_config() with no transport arg resolves through the same "
            "floor spawn() would ('' -> charter default 'watch') and likewise does "
            "not require .mcp.json",
        )
        _check(
            any("MCP config" in r for r in driver.verify_config(transport="mcp")),
            "an MCP-reading transport still refuses when .mcp.json is genuinely absent",
        )


def test_spawn_watch_transport_succeeds_without_mcp_json_present() -> None:
    """End-to-end companion to the verify_config regression above: spawn()
    itself (not just verify_config() in isolation) must not refuse a
    watch-transport worker for a missing .mcp.json."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _stub_worker_hook_files(tmp_dir)
        solet_bin = tmp_dir / "venv" / "bin" / "solet-bridge"
        solet_bin.parent.mkdir(parents=True, exist_ok=True)
        solet_bin.write_text("#!/bin/sh\nexit 0\n")
        solet_bin.chmod(0o755)
        driver = HeadlessHostDriver(
            claude_bin=_executable_stub(tmp_dir), solet_name="testhom",
            permission_mode="bypassPermissions",
            mcp_config_path=tmp_dir / "missing.mcp.json",
            cwd=tmp_dir, solet_bin=str(solet_bin),
            python_executable=str(solet_bin.parent / "python3"),
            popen_fn=lambda *a, **k: _FakeProc(pid=4242),
        )
        host_ref = driver.spawn(
            {"agent_instance_id": "agi-watchspawn", "transport": "watch"},
        )
        _check(
            host_ref == "4242",
            "spawn() with transport='watch' succeeds on a born-clone tree "
            "(no .mcp.json present anywhere)",
        )


def test_verify_config_accepts_a_per_spawn_permission_mode_override() -> None:
    """Regression guard for the exact bug the live e2e probe surfaced: a
    per-spawn permission_mode (§6 ruling, resolved from plugin.yaml at the
    platform_process shim and threaded through spec) must actually reach
    verify_config()'s gate -- a bare driver with NO env/constructor-level
    permission_mode still refuses without the override, but accepts it once
    supplied per-call."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = HeadlessHostDriver(
            claude_bin=_executable_stub(tmp_dir), solet_name="testhom",
            permission_mode="", mcp_config_path=tmp_dir / ".mcp.json", cwd=tmp_dir,
        )
        (tmp_dir / ".mcp.json").write_text("{}")
        _check(
            any("permission mode" in r for r in driver.verify_config()),
            "precondition: no constructor/env permission_mode -> the remedy fires bare",
        )
        _check(
            driver.verify_config(permission_mode="default") == [],
            "a per-spawn permission_mode override alone clears the remedy, even "
            "with no constructor/env value set -- this is what spawn() must pass",
        )


def test_spawn_refuses_when_unconfigured() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = HeadlessHostDriver(
            claude_bin="/nonexistent/claude", solet_name="", permission_mode="",
            mcp_config_path=Path(tmp) / "missing.json", cwd=Path(tmp),
        )
        refused = False
        try:
            driver.spawn({"agent_instance_id": "agi-x"})
        except HostCannotSpawnError:
            refused = True
        _check(refused, "spawn() refuses (HostCannotSpawnError) before ever calling popen_fn")


def test_spawn_succeeds_via_per_spawn_permission_mode_with_no_env_floor() -> None:
    """The exact end-to-end regression the live e2e probe caught: an
    driver with NO constructor/env permission_mode must still be able to
    spawn when the dispatch spec supplies one (§6 ruling's per-spawn
    resolution path) -- spawn() must thread spec['permission_mode'] into
    verify_config(), not just into _spawn_command()'s argv."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _stub_worker_hook_files(tmp_dir)
        driver = HeadlessHostDriver(
            claude_bin=_executable_stub(tmp_dir), solet_name="testhom",
            permission_mode="", mcp_config_path=tmp_dir / ".mcp.json", cwd=tmp_dir,
            popen_fn=lambda *a, **k: _FakeProc(pid=2222),
        )
        (tmp_dir / ".mcp.json").write_text("{}")
        host_ref = driver.spawn(
            {"agent_instance_id": "agi-permcheck", "permission_mode": "default"},
        )
        _check(
            host_ref == "2222",
            "spawn() succeeds with a per-spawn permission_mode even though "
            "the driver's own constructor/env value is empty",
        )


def test_spawn_refuses_without_agent_instance_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=lambda *a, **k: _FakeProc(pid=999))
        refused = False
        try:
            driver.spawn({"lane_id": "lane-x"})
        except HostCannotSpawnError as exc:
            refused = True
            _check(
                "agent_instance_id" in str(exc),
                "the refusal names the missing agent_instance_id",
            )
        _check(
            refused,
            "spawn() with no agent_instance_id refuses even when otherwise configured",
        )


def test_spawn_env_and_command_wiring() -> None:
    """The identity wiring backfill_registration depends on — verified
    against the real ``mcp_bridge/__main__.py`` contract (AGENT_INSTANCE_ID
    honored for exactly this 'managed spawner' case), not assumed."""
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=54321)

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_capture)
        host_ref = driver.spawn(
            {
                "agent_instance_id": "agi-abc", "lane_id": "lane-x",
                "model": "opus", "effort": "high",
            },
        )
        _check(host_ref == "54321", "spawn() returns str(pid) as host_ref")
        # Registration-loss fix (2026-08-14): a `watch`-transport spawn now
        # ALSO backgrounds the presence sidecar that puts the worker in
        # peer_list, so the worker process is popen call 0 and the watcher is
        # call 1 (mirrors codex_app_server). Asserted by position rather than
        # by a bare count, so a mutation that drops the worker spawn and keeps
        # only the watcher still reds here.
        worker_calls = [c for c in calls if "watch" not in c["cmd"]]
        watcher_calls = [c for c in calls if "watch" in c["cmd"]]
        _check(len(worker_calls) == 1, "popen_fn spawns exactly one worker process")
        _check(
            len(watcher_calls) == 1,
            "popen_fn also arms exactly one presence watcher on the watch transport",
        )
        env = worker_calls[0]["env"]
        _check(
            env["AGENT_INSTANCE_ID"] == "agi-abc",
            "env carries the ledger's own agent_instance_id",
        )
        _check(
            env["AGENT_SESSION_ID"] == "ases-agi-abc",
            "AGENT_SESSION_ID is derived from agent_instance_id",
        )
        _check(env["AGENT_SESSION_LABEL"] == "lane-x", "label prefers lane_id when given")
        _check(env["SOLET_NAME"] == "testhom", "SOLET_NAME flows from driver config")
        _check(
            Path(env["AGENT_WAKE_CLI"]).name == "solet-bridge"
            and env["AGENT_WAKE_CLI"] != env["SOLET_NAME"],
            "AGENT_WAKE_CLI is the wake-CLI EXECUTABLE, never the solet "
            "instance name -- `which <instance-name>` cannot resolve, so a "
            "value that tracked solet_name (e.g. 'testhom' here) silently "
            "broke every worker's idle-wake Stop hook; deaf-wake fix, "
            "2026-08-08",
        )
        _check(
            Path(env["AGENT_WAKE_CLI"]).is_absolute(),
            "AGENT_WAKE_CLI is ABSOLUTE, not the bare name -- a bare 'solet-bridge' "
            "is unresolvable under the minimal PATH a tmux pane or a "
            "materialized release actually runs with, and both the Stop-hook "
            "waker and the PostToolUse heartbeat then died silently "
            "(FileNotFoundError, exit 0); registration-loss fix, 2026-08-14",
        )
        _check(
            env["PATH"].split(os.pathsep)[0]
            == str(Path(env["AGENT_WAKE_CLI"]).parent),
            "the CLI's directory leads PATH, so hooks and skills that invoke "
            "a BARE `solet-bridge` resolve it too",
        )
        _check(
            env["FLEET_TRANSPORT"] == "watch",
            "an unspecified transport resolves to the charter's default 'watch' "
            "(fleet-watch-transport-migration phase 2 slice 1, 2026-08-06 -- "
            "non-MCP is now the fleet's PRIMARY transport, not 'mcp')",
        )
        cmd = calls[0]["cmd"]
        _check(
            "--permission-mode" in cmd and "bypassPermissions" in cmd,
            "permission mode flows into argv",
        )
        _check("--model" in cmd and "opus" in cmd, "model override flows into argv when given")
        _check("--effort" in cmd and "high" in cmd, "effort override flows into argv when given")
        _check(calls[0]["stdin"] == subprocess.PIPE, "stdin is a pipe (the driver channel)")
        _check(
            "--append-system-prompt" in cmd,
            "T2 authority-template: --append-system-prompt is ALWAYS injected, on by "
            "default, even for a caller (this test) that never supplied role_class/"
            "brief_ref/spawned_by_role -- missing fields render blank, never skip the flag",
        )
        _check(
            "FLEET_HEADLESS_TOOL_ALLOWLIST" not in env,
            "an omitted allowed_tools leaves the gate env var ENTIRELY ABSENT -- the "
            "deny-hook is unarmed by default (operator ruling, 2026-08-03: 'we don't "
            "have any restrictions now'), never an always-on empty-allowlist gate",
        )
        idx = cmd.index("--setting-sources")
        _check(
            cmd[idx + 1] == "project",
            "--setting-sources is pinned to project-only, excluding both this operator's "
            "user-scope bypassPermissions default and this checkout's local-scope broad "
            "Bash allow list (both verified live to leak into a spawned worker otherwise)",
        )
        settings_idx = cmd.index("--settings")
        settings_json = json.loads(cmd[settings_idx + 1])
        hook_command = settings_json["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        _check(
            hook_command.endswith("headless_tool_allowlist_gate.py"),
            "--settings injects the PreToolUse allowlist gate hook, scoped to this "
            "spawned worker only (never the shared .claude/settings.json)",
        )
        session_start_command = settings_json["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        _check(
            session_start_command.endswith("capture_session_mapping.py"),
            "--settings ALSO injects the T1 usage-capture SessionStart hook, "
            "merged alongside the PreToolUse gate (not replacing it)",
        )
        post_tool_use_command = settings_json["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        _check(
            post_tool_use_command.endswith("heartbeat_report_alive.py"),
            "--settings ALSO injects the T2 heartbeat PostToolUse hook, "
            "merged alongside the other two (not replacing either)",
        )
        rotation_due_command = settings_json["hooks"]["PostToolUse"][0]["hooks"][1]["command"]
        _check(
            rotation_due_command.endswith("rotation_due_watch.py"),
            "--settings ALSO injects the rotation-due watch PostToolUse hook "
            "(rotation-systematization P2 slice B), SECOND in the same PostToolUse "
            "group as the heartbeat hook, not replacing it",
        )
        _check(
            settings_json["permissions"]["deny"] == ["Agent", "Task"],
            "--settings ALSO injects the Agent/Task tool deny rule (capability-tier "
            "guardrail redesign, 2026-08-06), merged alongside the three hooks -- "
            "'Agent' is the live-registry-confirmed name, 'Task' a defensive alias",
        )


def test_spawn_transport_mcp_override_uses_real_mcp_config() -> None:
    """fleet-watch-transport-migration phase 2 slice 1 (2026-08-06): an
    explicit spec-level transport='mcp' (spawn_session's policy resolution,
    or a direct caller) gets the REAL --mcp-config path and
    --strict-mcp-config, never the watch-transport's explicit-empty one."""
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=1)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir, popen_fn=_capture)
        driver.spawn({"agent_instance_id": "agi-mcp-1", "transport": "mcp"})
    env = calls[0]["env"]
    cmd = calls[0]["cmd"]
    _check(env["FLEET_TRANSPORT"] == "mcp", "an explicit transport='mcp' flows into the env var")
    idx = cmd.index("--mcp-config")
    _check(
        cmd[idx + 1] == str(tmp_dir / ".mcp.json"),
        "transport='mcp' gets the real --mcp-config file path, not the explicit-empty JSON",
    )
    _check("--strict-mcp-config" in cmd, "--strict-mcp-config is present for the mcp transport too")


def test_spawn_transport_watch_uses_explicit_empty_mcp_config() -> None:
    """The watch-transport counterpart: --mcp-config carries an EXPLICIT
    empty '{"mcpServers":{}}', matching the WS-6-verified precedent --
    never simply omitted (omitting risks Claude Code's own ambient
    .mcp.json discovery silently re-attaching MCP under --setting-sources
    project)."""
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=1)

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_capture)
        driver.spawn({"agent_instance_id": "agi-watch-1", "transport": "watch"})
    env = calls[0]["env"]
    cmd = calls[0]["cmd"]
    _check(env["FLEET_TRANSPORT"] == "watch", "an explicit transport='watch' flows into the env var")
    idx = cmd.index("--mcp-config")
    _check(
        cmd[idx + 1] == '{"mcpServers":{}}',
        "transport='watch' gets an EXPLICIT empty MCP config, not the real file path "
        "and not an omitted flag",
    )
    _check("--strict-mcp-config" in cmd, "--strict-mcp-config is still present for the watch transport")
    _check(
        "--dangerously-load-development-channels" in cmd,
        "dev-channel loading stays unconditional -- orthogonal to MCP-vs-watch "
        "(a separate mechanism per the phase-2 scope ruling)",
    )


def test_spawn_transport_constructor_floor_used_when_spec_omits_it() -> None:
    """A caller that bypasses spawn_session's policy resolution but DOES
    construct the driver with an explicit transport still gets that value,
    not the charter's hardcoded floor -- same spec-then-constructor-then-
    hardcoded-default chain permission_mode already uses."""
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=1)

    with tempfile.TemporaryDirectory() as tmp:
        mcp_config = Path(tmp) / ".mcp.json"
        mcp_config.write_text("{}")
        _stub_worker_hook_files(Path(tmp))
        driver = HeadlessHostDriver(
            claude_bin=_executable_stub(Path(tmp)), solet_name="testhom",
            permission_mode="bypassPermissions", transport="mcp",
            mcp_config_path=mcp_config, cwd=Path(tmp), popen_fn=_capture,
        )
        driver.spawn({"agent_instance_id": "agi-floor-1"})
    env = calls[0]["env"]
    _check(
        env["FLEET_TRANSPORT"] == "mcp",
        "the driver's own constructor-level transport floor is used when the spec omits it",
    )


def test_spawn_env_session_mapping_spool_dir() -> None:
    """The T1 usage-capture spool env var: present and correctly rooted
    under APP_HOME's data dir when APP_HOME is set, entirely ABSENT
    (never an empty string) when it is unset -- mirrors the allowed_tools
    gate env var's own conditional-export contract."""
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=1)

    orig = os.environ.get("APP_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        app_home = Path(tmp) / "profile"
        os.environ["APP_HOME"] = str(app_home)
        try:
            driver = _configured_driver(Path(tmp), popen_fn=_capture)
            driver.spawn({"agent_instance_id": "agi-spool-2"})
        finally:
            _restore_env("APP_HOME", orig)
    env = calls[0]["env"]
    _check(
        env.get("ANANTA_SESSION_MAPPING_SPOOL_DIR")
        == str(app_home / "data" / "session_claude_mapping_spool"),
        "ANANTA_SESSION_MAPPING_SPOOL_DIR is rooted under APP_HOME's data dir, "
        "like every other platform spool (profile/data/{blobs,logs,plugin_data})",
    )

    calls.clear()
    prior = _pop_env_family()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), popen_fn=_capture)
            driver.spawn({"agent_instance_id": "agi-spool-3"})
    finally:
        _restore_env_family(prior)
    env = calls[0]["env"]
    _check(
        "ANANTA_SESSION_MAPPING_SPOOL_DIR" not in env,
        "APP_HOME unset -> the spool env var is ENTIRELY ABSENT, never an "
        "empty/bogus path -- the hook's own non-fatal contract handles this",
    )


def test_spawn_env_heartbeat_marker_dir() -> None:
    """T2's heartbeat marker dir env var: same conditional-export contract
    as ANANTA_SESSION_MAPPING_SPOOL_DIR, rooted under a SEPARATE APP_HOME
    subdirectory (never a sibling file inside the mapping spool -- that
    spool's own drain globs *.json and would misparse a stray marker)."""
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=1)

    orig = os.environ.get("APP_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        app_home = Path(tmp) / "profile"
        os.environ["APP_HOME"] = str(app_home)
        try:
            driver = _configured_driver(Path(tmp), popen_fn=_capture)
            driver.spawn({"agent_instance_id": "agi-hb-spool-1"})
        finally:
            _restore_env("APP_HOME", orig)
    env = calls[0]["env"]
    _check(
        env.get("AGENT_HEARTBEAT_MARKER_DIR") == str(app_home / "data" / "heartbeat_marker"),
        "AGENT_HEARTBEAT_MARKER_DIR is rooted under APP_HOME's data dir, "
        "in its OWN subdirectory distinct from the mapping spool",
    )
    _check(
        env.get("AGENT_COORDINATION_RECEIPT_PATH")
        == str(app_home / "data/coordination-hooks/claude/installation.v1.json"),
        "AGENT_COORDINATION_RECEIPT_PATH is rooted under APP_HOME, never the worker cwd",
    )
    designated_reporter = env.get("AGENT_CONTEXT_GAUGE_REPORTER_PATH", "")
    _check(
        Path(designated_reporter).is_absolute()
        and designated_reporter.endswith("rotation_due_watch.py"),
        "the headless worker receives an absolute designated gauge-reporter path",
    )

    calls.clear()
    prior = _pop_env_family()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            driver = _configured_driver(Path(tmp), popen_fn=_capture)
            driver.spawn({"agent_instance_id": "agi-hb-spool-2"})
    finally:
        _restore_env_family(prior)
    env = calls[0]["env"]
    _check(
        "AGENT_HEARTBEAT_MARKER_DIR" not in env,
        "APP_HOME unset -> the heartbeat marker dir env var is ENTIRELY ABSENT, "
        "never an empty/bogus path",
    )
    _check(
        "AGENT_COORDINATION_RECEIPT_PATH" not in env,
        "APP_HOME unset -> the ownership receipt env var is entirely absent",
    )


def test_spawn_wires_authority_system_prompt_with_full_substitution() -> None:
    """T2 authority-template: a spec carrying role_class/brief_ref/
    spawned_by_role (the shape spawn_session's own widened dict now sends)
    flows all the way through to --append-system-prompt's rendered text."""
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=9999)

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_capture)
        driver.spawn(
            {
                "agent_instance_id": "agi-authority-1",
                "lane_id": "fleet-authority-test",
                "role_class": "project",
                "brief_ref": "workbench/2026-08-04_x.md",
                "spawned_by_role": "Coordinator-Main",
            },
        )
        cmd = calls[0]["cmd"]
        idx = cmd.index("--append-system-prompt")
        rendered = cmd[idx + 1]
        _check("agi-authority-1" in rendered, "the rendered prompt carries agent_instance_id")
        _check("'project'" in rendered, "the rendered prompt carries role_class")
        _check("'fleet-authority-test'" in rendered, "the rendered prompt carries lane_id")
        _check("workbench/2026-08-04_x.md" in rendered, "the rendered prompt carries brief_ref")
        _check("'Coordinator-Main'" in rendered, "the rendered prompt carries spawned_by_role")
        _check("{role_class}" not in rendered, "no unresolved placeholder ships into the command")


def test_spawn_threads_allowed_tools_into_the_gate_env_var() -> None:
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=1111)

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_capture)
        driver.spawn(
            {
                "agent_instance_id": "agi-allow", "lane_id": "lane-allow",
                "allowed_tools": ["mcp__testhom__peer_register", "mcp__testhom__process_call"],
            },
        )
        env = calls[0]["env"]
        expected = "mcp__testhom__peer_register,mcp__testhom__process_call"
        _check(
            env["FLEET_HEADLESS_TOOL_ALLOWLIST"] == expected,
            "a provided allowed_tools list is joined into the gate env var verbatim",
        )


def test_spawn_label_falls_back_to_agent_instance_id() -> None:
    calls: list[dict[str, Any]] = []

    def _capture(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append({"cmd": cmd, **kwargs})
        return _FakeProc(pid=1)

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_capture)
        driver.spawn({"agent_instance_id": "agi-nolane"})
        _check(
            calls[0]["env"]["AGENT_SESSION_LABEL"] == "agi-nolane",
            "an ephemeral spawn with no lane_id falls back to agent_instance_id as the label",
        )


def test_spawn_wraps_popen_oserror() -> None:
    def _raiser(*_a: Any, **_k: Any) -> _FakeProc:
        raise OSError("no such file")

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_raiser)
        wrapped = False
        try:
            driver.spawn({"agent_instance_id": "agi-y"})
        except HostCannotSpawnError:
            wrapped = True
        _check(wrapped, "a Popen OSError is wrapped as HostCannotSpawnError, never leaks raw")


def _real_short_lived_popen_fn(*_a: Any, **_k: Any) -> subprocess.Popen[str]:
    """Ignores the incoming cmd/env — spawns a real, harmless, short-lived
    process so alive()/terminate()/shutdown() exercise real OS signals."""
    return subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_alive_and_terminate_real_process() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_real_short_lived_popen_fn)
        host_ref = driver.spawn({"agent_instance_id": "agi-real-1"})
        _check(driver.alive(host_ref), "a freshly-spawned real process reports alive=True")
        _check(driver.alive("not-a-pid") is False, "a non-numeric host_ref is never alive")
        driver.terminate(host_ref, grace_seconds=2)
        time.sleep(0.2)
        _check(not driver.alive(host_ref), "terminate() leaves the process dead")
        _check(
            driver.driver_channel(host_ref) is None,
            "driver_channel() is None once terminate() has dropped the tracked entry",
        )


def _real_echo_to_file_popen_fn(
    echo_file: Path, *, launch_delay_s: float = 0.0,
) -> Any:
    """A real process that reads ONE line from stdin and writes it to
    ``echo_file`` (NOT stdout) -- spawn() now drains stdout in a background
    thread (the stdout-never-read fix), so a test observing what send()
    wrote can no longer read the child's stdout pipe directly; it must
    observe through a side channel the driver doesn't touch.

    ARGV-AWARE, AND THAT IS LOAD-BEARING (2026-08-16). ``spawn()`` makes TWO
    popen calls through this injected fn, not one: the worker itself, and then
    the presence sidecar (``_resolve_transport``'s floor is "watch", so
    ``_arm_watcher`` runs). An argv-BLIND fake handed the echo argv to both, so
    the watcher child ran ``open(echo_file, "w")`` — CREATE+TRUNCATE — on
    startup and then blocked forever on a stdin nobody writes. If its startup
    landed after the real child's write, it truncated the delivered line to
    zero bytes.

    That is the whole of the ~1-in-6 flake this file carried: file exists, size
    0, and NO wait bound can fix it because the bytes were already destroyed.
    Measured deterministically from the seat: argv-blind + a 150ms watcher delay
    -> 6/6 fail (size 0); argv-aware, same delay -> 6/6 pass (size 95). Load is
    the natural delay source, which is why it surfaced in full-suite and
    gate-register runs rather than on an idle box.

    Only the WORKER spawn gets the echo child; the watcher arm gets the real
    argv it asked for (the stub ``solet-bridge`` exits 0). This is the
    widening-degrades-fakes trap: the 08-13/14 registration fix widened spawn
    from one popen call to two, and this fake was never re-audited.

    ``launch_delay_s`` exists so the regression test can force the ordering that
    used to lose the bytes; production paths never set it.
    """

    def _is_watcher(argv: object) -> bool:
        """Discriminate on argv[0] ONLY — the launched BINARY.

        Not a substring search over the whole argv: measured 2026-08-16, the
        WORKER's argv also contains "watch", because its ``--settings`` JSON
        wires the watch/wake hooks. A whole-argv match therefore classifies the
        worker as the watcher, routes the real echo argv to neither, and fails
        every run. The sidecar is the one launched via the stub ``solet-bridge``
        binary, so argv[0] is the only honest signal.
        """
        parts = [str(x) for x in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
        return bool(parts) and Path(parts[0]).name == "solet-bridge"

    def _fn(*a: Any, **_k: Any) -> subprocess.Popen[str]:
        argv = a[0] if a else []
        if _is_watcher(argv):
            if launch_delay_s:
                time.sleep(launch_delay_s)
            # The sidecar gets what it actually asked for. Never the echo argv:
            # a second writer on the same path is what broke this.
            return subprocess.Popen(  # noqa: S603 -- caller-supplied stub argv, test-only
                [str(x) for x in argv],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        return subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
            [
                sys.executable, "-c",
                "import sys; open(sys.argv[1], 'w').write(sys.stdin.readline())",
                str(echo_file),
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    return _fn


def test_driver_channel_send_writes_stream_json_envelope() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        echo_file = Path(tmp) / "echo.txt"
        driver = _configured_driver(
            Path(tmp), popen_fn=_real_echo_to_file_popen_fn(echo_file),
        )
        host_ref = driver.spawn({"agent_instance_id": "agi-echo-1"})
        channel = driver.driver_channel(host_ref)
        _check(
            channel is not None,
            "driver_channel() returns a live channel for a tracked host_ref",
        )
        assert channel is not None
        channel.send("/clear")
        # A REAL child process must be scheduled, read a line from its stdin and
        # write it out, so this wait is a race against the machine rather than a
        # behavioural assertion. The bound is generous because a slow box is not a
        # product defect.
        #
        # THE BOUND WAS NEVER THE BUG, and the record of getting that wrong is
        # kept deliberately. At 5s this failed ~1-in-6; widening to 30s still
        # failed 2 of 8, which DISPROVED "the bound is too tight" rather than
        # confirming it. The real cause was this file's OWN fixture truncating
        # the file from a second child — see _real_echo_to_file_popen_fn. A
        # timing bound cannot fix destroyed bytes, and widening one to chase an
        # intermittent hides the question instead of answering it.
        deadline = time.monotonic() + _ECHO_WAIT_SECONDS
        while not (echo_file.exists() and echo_file.stat().st_size > 0):
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)
        wrote_echo = echo_file.exists() and echo_file.stat().st_size > 0
        _check(
            wrote_echo,
            f"the child wrote the echoed line to the side-channel file within {_ECHO_WAIT_SECONDS:g}s",
        )
        if not wrote_echo:
            # HALT before anything that reads the file. Previously execution fell
            # through to json.loads() on an empty file, so a TIMEOUT surfaced as
            # `JSONDecodeError: Expecting value: line 1 column 1` four lines later
            # — an unhandled traceback pointing at JSON parsing when the finding
            # was a wait that expired. A check that does not stop its dependents
            # reports the wrong defect.
            driver.terminate(host_ref, grace_seconds=2)
            return
        payload = json.loads(echo_file.read_text())
        _check(
            payload.get("type") == "user",
            "the envelope's type is 'user' (stream-json input contract)",
        )
        _check(
            payload.get("message", {}).get("content", [{}])[0].get("text") == "/clear",
            "the envelope carries the sent text verbatim",
        )
        driver.terminate(host_ref, grace_seconds=2)


def test_sidecar_popen_never_targets_the_worker_side_channel() -> None:
    """MECHANISM, asserted directly and WITHOUT timing.

    The previous version of this check counted popen calls by the argv the
    DRIVER passed in — which cannot see the bug, because the fixture substitutes
    the echo argv internally. An argv-blind fake still shows one solet-bridge argv and
    one claude argv, so the count looked right while both children wrote the
    same file. Measured: that assertion passed under the very mutation it was
    written to catch.

    So this asks the only question that settles it — hand the fixture a SIDECAR
    argv and see whether the child it returns touches the worker's file. No
    spawn, no delay, no ordering: if the sidecar can write that path at all, the
    collision is live.
    """
    with tempfile.TemporaryDirectory() as tmp:
        echo_file = Path(tmp) / "echo.txt"
        echo_file.write_text("SENTINEL")
        solet_stub = Path(tmp) / "venv" / "bin" / "solet-bridge"
        solet_stub.parent.mkdir(parents=True, exist_ok=True)
        solet_stub.write_text("#!/bin/sh\nexit 0\n")
        solet_stub.chmod(0o755)

        fn = _real_echo_to_file_popen_fn(echo_file)
        proc = fn([str(solet_stub), "watch", "--role", "probe"])
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        _check(
            echo_file.read_text() == "SENTINEL",
            "a SIDECAR-argv child never writes the worker's side-channel file "
            "(argv-blind fixtures truncate it — the R1 gate blocker)",
        )


def test_watcher_arm_does_not_clobber_the_worker_side_channel() -> None:
    """END-TO-END companion to the mechanism check above: a real spawn with the
    watcher arm deliberately DELAYED 150ms so it lands after the worker's write
    — the ordering that used to destroy the bytes, forced rather than awaited.

    Asserting outcomes here is enough only because the mechanism is asserted
    separately; the collision has at least three observable states (empty file,
    malformed non-empty json from a mid-write read, and a benign pass), and
    enumerating shapes is a losing game.
    """
    with tempfile.TemporaryDirectory() as tmp:
        echo_file = Path(tmp) / "echo.txt"
        driver = _configured_driver(
            Path(tmp),
            popen_fn=_real_echo_to_file_popen_fn(echo_file, launch_delay_s=0.15),
        )
        host_ref = driver.spawn({"agent_instance_id": "agi-echo-clobber"})
        channel = driver.driver_channel(host_ref)
        assert channel is not None
        channel.send("/clear")
        deadline = time.monotonic() + _ECHO_WAIT_SECONDS
        while not (echo_file.exists() and echo_file.stat().st_size > 0):
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)
        time.sleep(0.35)  # sleep PAST the injected delay: that ordering is the test
        survived = echo_file.exists() and echo_file.stat().st_size > 0
        _check(survived, "the worker's side-channel survives a LATE watcher arm")
        if survived:
            _check(
                json.loads(echo_file.read_text()).get("type") == "user",
                "the surviving bytes are the worker's envelope, not a truncated remnant",
            )
        driver.terminate(host_ref, grace_seconds=2)


def test_driver_channel_none_for_unknown_host_ref() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_real_short_lived_popen_fn)
        _check(
            driver.driver_channel("99999999") is None,
            "driver_channel() is None for a host_ref this driver never spawned "
            "(the exact post-restart case clear_session/compact_session will check)",
        )


def _real_exit_immediately_popen_fn(*_a: Any, **_k: Any) -> subprocess.Popen[str]:
    """A real process that exits on its own, immediately -- never reaped via
    ``terminate()``, so the driver's ``_processes`` map keeps a STALE
    tracked entry for an already-dead pid (the TOCTOU/no-terminate-call case
    ``driver_channel()``'s liveness check now guards against)."""
    return subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
        [sys.executable, "-c", "pass"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_driver_channel_none_for_dead_but_still_tracked_process() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_real_exit_immediately_popen_fn)
        host_ref = driver.spawn({"agent_instance_id": "agi-dead-tracked-1"})
        deadline = time.monotonic() + 5
        while driver.alive(host_ref) and time.monotonic() < deadline:
            time.sleep(0.05)
        _check(
            not driver.alive(host_ref),
            "precondition: the spawned process has exited on its own",
        )
        _check(
            driver.driver_channel(host_ref) is None,
            "driver_channel() is None for a still-tracked pid that died on its own "
            "(never terminate()'d) -- a dead process must not hand back a live-looking channel",
        )


def test_send_swallows_broken_pipe_from_dead_process() -> None:
    proc = subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
        [sys.executable, "-c", "pass"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    proc.wait(timeout=5)
    channel = _StreamJsonDriverChannel(proc=proc)
    raised = False
    try:
        channel.send("/clear")
    except Exception:  # noqa: BLE001 -- the exact thing under test: nothing escapes
        raised = True
    _check(
        not raised,
        "send() on a dead process's stdin swallows the broken pipe (fire-and-forget "
        "contract) instead of raising an unmapped exception through the verb layer",
    )
    if proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.close()


def test_shutdown_terminates_every_tracked_process() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), popen_fn=_real_short_lived_popen_fn)
        ref1 = driver.spawn({"agent_instance_id": "agi-shutdown-1"})
        ref2 = driver.spawn({"agent_instance_id": "agi-shutdown-2"})
        _check(
            driver.alive(ref1) and driver.alive(ref2),
            "precondition: both tracked processes are alive",
        )
        driver.shutdown()
        time.sleep(0.2)
        _check(
            not driver.alive(ref1) and not driver.alive(ref2),
            "shutdown() terminates every tracked process",
        )


def test_capability_report_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = _configured_driver(Path(tmp)).capability_report()
        _check(report.get("host") == "headless", "capability_report names the host")
        _check(report.get("topology") == "subprocess", "capability_report names the topology")


# --- §45.1 (#17): the SECOND managed-policy key -----------------------------
#
# Every leg below drives the reader with a POPULATED fixture file, for the
# reason already stated on _write_policy: there is no managed policy on this
# machine, so an absent-file probe verifies nothing about the populated case
# and a green from one would be a success path that never ran.


@contextlib.contextmanager
def _captured_warnings() -> Iterator[list[logging.LogRecord]]:
    """Records WARNING records emitted by the adapter under test.

    Needed because §45.1's whole posture is warn-rather-than-refuse: the
    observable is the log line, so a test that only asserted "verify_config
    returned no remedy" would pass identically against a build that detected
    nothing at all. That is the difference between proving the warn path and
    proving the absence of a refusal.
    """
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    sink = _Sink(level=logging.WARNING)
    target = logging.getLogger(headless_adapter.__name__)
    target.addHandler(sink)
    previous = target.level
    target.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        target.removeHandler(sink)
        target.setLevel(previous)


@contextlib.contextmanager
def _default_policy_paths(paths: tuple[Path, ...]) -> Iterator[None]:
    """Point the MODULE-LEVEL default search list at a fixture.

    capability_report() and spawn() deliberately read the module constant
    rather than an injected tuple, because in production the question is
    always "what policy is in force on THIS host". That leaves this patch as
    the only way to exercise their populated case; verify_config keeps its own
    managed_settings_paths seam and does not need it.
    """
    original = headless_adapter._MANAGED_SETTINGS_PATHS
    headless_adapter._MANAGED_SETTINGS_PATHS = paths
    try:
        yield
    finally:
        headless_adapter._MANAGED_SETTINGS_PATHS = original


def test_permissions_inert_policy_warns_and_never_refuses() -> None:
    """The §45.1 shape: allowManagedPermissionRulesOnly voids every
    user/project deny, and this driver says so LOUDLY while still spawning.

    Would catch: adding the key to the reader but giving it the refusal
    posture (a remedy would appear), and adding the refusal-shaped code path
    without the log line (records would be empty). This number would also be
    this number if the reader matched on the key's PRESENCE rather than on
    the value True -- test_permissions_inert_requires_literal_true is the leg
    that excludes that.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        policy = _write_policy(
            tmp_dir / "managed-settings.json",
            {_ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY: True},
        )
        _check(
            driver.verify_config(transport="watch", managed_settings_paths=(policy,)) == [],
            "a permissions-inert policy is NOT a spawn refusal (warn, not refuse)",
        )
        _check(
            _managed_policy_voids_permission_rules((policy,)) == policy,
            "but the prover does detect it, so the empty remedy list is a "
            "decision rather than a blind spot",
        )
        with _captured_warnings() as records:
            returned = _warn_permission_denies_inert(
                (policy,), agent_instance_id="agi-fixture", host="headless",
                denied_tools=_HEADLESS_DENIED_TOOLS,
            )
        _check(returned == policy, "the warn helper reports which file it fired on")
        _check(len(records) == 1, f"exactly one warning is emitted (got {len(records)})")
        text = records[0].getMessage() if records else ""
        _check(str(policy) in text, "the warning names the policy file it read")
        _check(
            _ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY in text,
            "and names the offending key, so the operator can find it",
        )
        _check(
            all(tool in text for tool in _HEADLESS_DENIED_TOOLS),
            "and enumerates the denies that are now inert on this host",
        )
        _check(
            "hygiene" in text and "security control" in text,
            "and frames the loss honestly -- a deny a local policy can switch "
            "off was never a boundary, so this must not read as a breach",
        )


def test_permissions_inert_and_hooks_keys_are_independent() -> None:
    """Both keys in ONE file: the hooks key refuses, the permissions key
    warns, and neither answer is contaminated by the other.

    Would catch: a shared prover that returned on the first key it matched, or
    a refusal list that grew a second entry for the permissions key.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        policy = _write_policy(
            tmp_dir / "managed-settings.json",
            {
                "strictPluginOnlyCustomization": ["hooks"],
                _ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY: True,
            },
        )
        remedies = driver.verify_config(
            transport="watch", managed_settings_paths=(policy,),
        )
        _check(
            len(remedies) == 1 and "strictPluginOnlyCustomization" in remedies[0],
            f"exactly ONE remedy, the hooks one (got {remedies!r})",
        )
        _check(
            _managed_policy_voids_permission_rules((policy,)) == policy,
            "and the permissions key is still detected in the same file",
        )


def test_hooks_key_alone_leaves_permission_denies_operative() -> None:
    """The discriminating leg in the other direction: §43.1's policy shape
    must NOT report permissions as inert. Without this, a prover that fired on
    the mere presence of a managed policy would look correct."""
    with tempfile.TemporaryDirectory() as tmp:
        policy = _write_policy(
            Path(tmp) / "managed-settings.json",
            {"strictPluginOnlyCustomization": ["hooks"]},
        )
        _check(
            _managed_policy_voids_permission_rules((policy,)) is None,
            "a hooks-stripping policy says nothing about permission rules",
        )


def test_permissions_inert_key_alone_is_not_a_hooks_remedy() -> None:
    """And §45.1's policy shape must not start refusing spawns for §43.1's
    reason -- the adopter's machine carries both keys, so a prover that
    conflated them would refuse for the wrong stated cause."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        policy = _write_policy(
            tmp_dir / "managed-settings.json",
            {_ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY: True},
        )
        _check(
            driver.verify_config(transport="watch", managed_settings_paths=(policy,)) == [],
            "the permissions key alone produces no hooks remedy",
        )


def test_permissions_inert_requires_literal_true() -> None:
    """``1 == True`` in Python, so a ``== True`` implementation would fire on
    an integer 1 and on any truthy value. The flag is documented as a boolean;
    treating ``"false"``/``1``/``[]`` as evidence the organisation set it is
    the silent fallback this repository forbids.

    This is the leg that makes the warn path's green mean something: without
    it, the same number would appear for a reader that matched on the key's
    presence alone.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for index, value in enumerate([False, 1, "true", "false", [], {}, None]):
            policy = _write_policy(
                tmp_dir / f"policy-{index}.json",
                {_ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY: value},
            )
            _check(
                _managed_policy_voids_permission_rules((policy,)) is None,
                f"{value!r} is not the boolean true and must not fire",
            )
        exact = _write_policy(
            tmp_dir / "policy-true.json",
            {_ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY: True},
        )
        _check(
            _managed_policy_voids_permission_rules((exact,)) == exact,
            "and the reachable all-clear: literal true DOES fire, so the legs "
            "above are not all passing for the trivial reason",
        )


def test_permissions_inert_malformed_file_fails_open() -> None:
    """Same fail-open-loudly posture as the hooks prover, for the same
    evidential reason: an unparseable policy file is not evidence that
    permission rules were voided."""
    with tempfile.TemporaryDirectory() as tmp:
        policy = Path(tmp) / "managed-settings.json"
        policy.write_text("{not json at all", encoding="utf-8")
        with _captured_warnings() as records:
            result = _managed_policy_voids_permission_rules((policy,))
        _check(result is None, "a malformed policy file fails OPEN, never fires")
        _check(
            len(records) == 1 and "could not be read" in records[0].getMessage(),
            "and fails open LOUDLY -- a silent skip would be a third silence",
        )


def test_permissions_inert_non_dict_top_level_is_silent() -> None:
    """A policy file whose top level is a list (or a bare scalar) parses fine
    and has no keys at all. Without this leg a ``raw.get`` on a list would
    raise AttributeError at spawn time rather than skipping the file."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for index, body in enumerate([["hooks"], "allowManagedPermissionRulesOnly", 7]):
            policy = _write_policy(tmp_dir / f"policy-{index}.json", body)
            _check(
                _managed_policy_voids_permission_rules((policy,)) is None,
                f"a non-dict top level ({body!r}) is skipped, not an exception",
            )


def test_capability_report_answers_permission_state_without_spawning() -> None:
    """Q1 descoped the ledger record, which makes this the ONLY surface that
    answers "are permission denies operative on this host?" in advance. Driven
    against a populated fixture through the module default, because that is
    what capability_report actually reads -- a green obtained with no file
    present would be the absent-file probe again.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(tmp_dir)
        clean = driver.capability_report()
        _check(
            clean.get("permission_denies") == list(_HEADLESS_DENIED_TOOLS),
            "the report names the denies this driver injects",
        )
        _check(
            clean.get("permission_denies_operative") is True
            and clean.get("permission_policy_path") is None,
            "on a host with no managed policy the denies are operative",
        )
        policy = _write_policy(
            tmp_dir / "managed-settings.json",
            {_ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY: True},
        )
        with _default_policy_paths((policy,)):
            inert = driver.capability_report()
        _check(
            inert.get("permission_denies_operative") is False,
            "and a flagged host reports them as NOT operative",
        )
        _check(
            inert.get("permission_policy_path") == str(policy),
            "naming the file, so the answer is actionable without a spawn",
        )


def test_spawn_warns_on_a_permissions_inert_host_and_still_spawns() -> None:
    """End to end through spawn(): the warning fires and the spawn SUCCEEDS.

    This is the leg that would catch the posture being flipped later. A test
    that only called the helper directly would stay green if someone wired the
    detector into verify_config's remedy list instead.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        driver = _configured_driver(
            tmp_dir, popen_fn=lambda *a, **k: _FakeProc(pid=4242),
        )
        policy = _write_policy(
            tmp_dir / "managed-settings.json",
            {_ALLOW_MANAGED_PERMISSION_RULES_ONLY_KEY: True},
        )
        with _default_policy_paths((policy,)), _captured_warnings() as records:
            host_ref = driver.spawn(
                {"agent_instance_id": "agi-inert", "transport": "watch"},
            )
        _check(host_ref == "4242", "the spawn PROCEEDS on a permissions-inert host")
        inert_warnings = [
            r for r in records if "PERMISSION DENIES INERT" in r.getMessage()
        ]
        _check(
            len(inert_warnings) == 1,
            f"and emits exactly one inert-permissions warning (got "
            f"{len(inert_warnings)})",
        )
        _check(
            "headless" in inert_warnings[0].getMessage() if inert_warnings else False,
            "which names the host whose denies were dropped",
        )


def main() -> int:
    test_resolve_default_cwd_prefers_app_home_git_checkout()
    test_resolve_default_cwd_falls_back_when_app_home_unset()
    test_resolve_default_cwd_falls_back_when_app_home_not_a_checkout()
    test_driver_uses_resolved_default_cwd_when_none_passed()
    test_resolve_worker_hook_path_prefers_rung1_when_present()
    test_resolve_worker_hook_path_falls_back_to_rung2()
    test_resolve_worker_hook_path_fails_loud_when_neither_rung_resolves()
    test_resolve_worker_hook_paths_resolves_every_required_filename()
    test_spawn_refuses_when_a_worker_hook_resolves_at_neither_rung()
    test_verify_config_remedies_are_independent()
    test_managed_policy_hooks_plugin_only_refuses_spawn()
    test_default_policy_paths_include_the_remote_pulled_location()
    test_managed_policy_without_hooks_key_is_not_a_remedy()
    test_managed_policy_acknowledged_degraded_proceeds()
    test_managed_policy_malformed_file_fails_open()
    test_managed_policy_absent_file_is_silent()
    test_permissions_inert_policy_warns_and_never_refuses()
    test_permissions_inert_and_hooks_keys_are_independent()
    test_hooks_key_alone_leaves_permission_denies_operative()
    test_permissions_inert_key_alone_is_not_a_hooks_remedy()
    test_permissions_inert_requires_literal_true()
    test_permissions_inert_malformed_file_fails_open()
    test_permissions_inert_non_dict_top_level_is_silent()
    test_capability_report_answers_permission_state_without_spawning()
    test_spawn_warns_on_a_permissions_inert_host_and_still_spawns()
    test_verify_config_mcp_config_required_only_for_mcp_transport()
    test_verify_config_accepts_a_per_spawn_permission_mode_override()
    test_spawn_refuses_when_unconfigured()
    test_spawn_watch_transport_succeeds_without_mcp_json_present()
    test_spawn_succeeds_via_per_spawn_permission_mode_with_no_env_floor()
    test_spawn_refuses_without_agent_instance_id()
    test_spawn_env_and_command_wiring()
    test_spawn_transport_mcp_override_uses_real_mcp_config()
    test_spawn_transport_watch_uses_explicit_empty_mcp_config()
    test_spawn_transport_constructor_floor_used_when_spec_omits_it()
    test_spawn_env_session_mapping_spool_dir()
    test_spawn_env_heartbeat_marker_dir()
    test_spawn_wires_authority_system_prompt_with_full_substitution()
    test_spawn_threads_allowed_tools_into_the_gate_env_var()
    test_spawn_label_falls_back_to_agent_instance_id()
    test_spawn_wraps_popen_oserror()
    test_alive_and_terminate_real_process()
    test_driver_channel_send_writes_stream_json_envelope()
    test_sidecar_popen_never_targets_the_worker_side_channel()
    test_watcher_arm_does_not_clobber_the_worker_side_channel()
    test_driver_channel_none_for_unknown_host_ref()
    test_driver_channel_none_for_dead_but_still_tracked_process()
    test_send_swallows_broken_pipe_from_dead_process()
    test_shutdown_terminates_every_tracked_process()
    test_capability_report_shape()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
