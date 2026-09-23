#!/usr/bin/env python3
"""Offline smoke for managed Codex runtime parity.

Named mutations this suite must catch:

* collapsing runtime into host (``codex-headless``) instead of preserving the
  ``(agent_runtime, host)`` tuple;
* restart defaulting a Codex row back to Claude;
* silently accepting a Claude provider/provider_env overlay on Codex;
* treating app-server ``/clear`` as a new managed-session identity;
* tmux sending text+Enter without styled-pane pickup verification;
* watch registration claiming the session label as a role;
* spawning a Codex lane whose ``codex-code-mode-host`` helper cannot exec --
  a code-mode model runs every tool call through that helper, so an
  un-exec-able one wedges the lane silently from birth.

All fakes are records-only and return the real envelope/result shapes consumed
by production code.  No Codex model turn, bridge, or database is used.  One
contained recovery proof uses a disposable real tmux session.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from _real_state_fake import RealShapeState  # noqa: E402
from _recorded_lane_worktree_fixture import RecordedLaneWorktreeFixture  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_CLAIM_EPOCH,
    COL_HOLDER_IDENTITY,
    COL_HOLDER_KIND,
    COL_ROLE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.codex_adapter import (  # noqa: E402
    CodexAppServerHostDriver,
    CodexTmuxHostDriver,
    _CodexAppServerClient,
    _CodexTmuxDriverChannel,
    _identity_env,
)
from agent_messaging_plugin.local_cli.cli import (  # noqa: E402
    WatchIdentity,
    _register_without_claim,
)
from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_ACTIVE,
    DISPATCH_UPTAKE_PENDING,
    DISPATCH_WORKER_LOST,
    DispatchActor,
    DispatchError,
    DispatchSpec,
    dispatch_managed_work,
    read_managed_dispatch,
    report_managed_dispatch,
)
from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _spawn_session_request_from_params,
)
from agent_messaging_plugin.schema import get_managed_session_schema  # noqa: E402
from agent_messaging_plugin.session_hosts import (  # noqa: E402
    DriverChannelSendError,
    HostCannotSpawnError,
    resolve_host_driver,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    backfill_registration,
    read_managed_session,
)
from agent_messaging_plugin.session_lifecycle_verbs import SpawnSessionRequest  # noqa: E402
from agent_messaging_plugin.session_sweep import sweep_managed_dispatches  # noqa: E402

# Hoisted out of codex_common to the runner-neutral solet_cli (2026-08-14):
# the Claude adapters need the identical resolution, and the asymmetry was the
# registration-loss root cause.
from agent_messaging_plugin.solet_cli import (  # noqa: E402
    resolve_solet_bin as _resolve_solet_bin,
)

_passed = 0
_failed: list[str] = []

# Set when the real-tmux tier disclosed a missing host binary instead of running.
_real_tmux_tier_skipped = False

# The born-clone gate's exit code for "environment-dependent, not broken", and the
# machine-readable cause it parses. The witness schema is CLOSED — the gate accepts
# exactly {"reason", "executable"} and rejects anything else, so the leg that did
# not run is named in the human SKIP line beside it rather than as a third JSON
# key, which would invalidate the witness and turn a tolerated row back into a
# BLOCKING one. Exactly one witness line may be printed; more than one also blocks.
_SKIP_EXIT_CODE = 77
_SKIP_WITNESS_PREFIX = "BORN_CLONE_SKIP_WITNESS="


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _executable(path: Path, name: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    return str(target)


def _codex_home(path: Path, *, plugin_enabled: bool = True) -> Path:
    home = path / "codex-home"
    home.mkdir()
    enabled = "true" if plugin_enabled else "false"
    (home / "config.toml").write_text(
        "[mcp_servers.testhom]\n"
        "command = \"solet-bridge\"\n"
        "[plugins.\"coordination-hooks@testhom-development\"]\n"
        f"enabled = {enabled}\n",
    )
    return home


def _composite_spec(
    root: Path,
    dispatch_id: str,
    host: str,
    *,
    agent_runtime: str = "codex",
) -> DispatchSpec:
    brief = root / f"{dispatch_id}-brief.md"
    brief.write_text("exact managed Codex brief\n", encoding="utf-8")
    now = datetime.now(UTC)
    return DispatchSpec(
        dispatch_id=dispatch_id,
        lane_id=dispatch_id,
        role_name=f"{dispatch_id}-Builder",
        role_class="project",
        work_class="production_mutation",
        budget_line="codex-managed-smoke",
        brief_ref=str(brief),
        brief_sha256=hashlib.sha256(brief.read_bytes()).hexdigest(),
        expected_path=str(root / f"{dispatch_id}-report.md"),
        completion_contract={
            "evidence_obligations": [
                {"id": "focused", "allowed_statuses": ["pass"]},
            ],
            "allowed_verdicts": ["READY-FOR-REVIEW", "BLOCKED"],
        },
        model="gpt-5.6-sol",
        effort="xhigh",
        agent_runtime=agent_runtime,
        allowed_hosts=[host],
        host=host,
        visibility="visible" if host == "tmux" else "headless",
        local_name=f"{dispatch_id}-Builder",
        report_by_seconds=900,
        ttl_seconds=14400,
        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
        transport="mcp",
        allow_askuserquestion=False,
        degraded_hooks_acknowledged=False,
        spawned_by_instance_id="agi-coordinator",
        spawned_by_role="Coordinator-Main",
        directed_by="operator:seat",
        uptake_due_at=(now + timedelta(minutes=2)).isoformat(),
        report_by=(now + timedelta(minutes=15)).isoformat(),
        watchdog_due_at=(now + timedelta(minutes=3)).isoformat(),
        expires_at=(now + timedelta(hours=4)).isoformat(),
        dispatch_kind="infrastructure",
    )


def _composite_spawn_request(spec: DispatchSpec, host: str) -> SpawnSessionRequest:
    return SpawnSessionRequest(
        role_class=spec.role_class,
        lane_id=spec.lane_id,
        brief_ref=spec.brief_ref,
        work_class=spec.work_class,
        budget_line=spec.budget_line,
        agent_runtime=spec.agent_runtime,
        role_name=spec.role_name,
        host=host,
        visibility=spec.visibility,
        model=spec.model,
        effort=spec.effort,
        report_by_seconds=spec.report_by_seconds,
        ttl_seconds=spec.ttl_seconds,
        spawned_by_instance_id=spec.spawned_by_instance_id,
        spawned_by_role=spec.spawned_by_role,
        directed_by=spec.directed_by,
        allowed_tools=spec.allowed_tools,
        permission_mode=spec.permission_mode,
        transport=spec.transport,
        allow_askuserquestion=spec.allow_askuserquestion,
        local_name=spec.local_name,
        degraded_hooks_acknowledged=spec.degraded_hooks_acknowledged,
        dispatch_kind=spec.dispatch_kind,
        reviewed_report_vendor=spec.reviewed_report_vendor,
        pair_id=spec.pair_id,
    )


def _seed_worker_binding(
    state: RealShapeState,
    *,
    role: str,
    instance: str,
    agent_id: str = "codex",
) -> None:
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(role),
                COL_ROLE: role,
                COL_HOLDER_KIND: HOLDER_KIND_SESSION,
                COL_AGENT_INSTANCE_ID: instance,
                COL_AGENT_SESSION_ID: f"ases-{instance}",
                COL_HOLDER_IDENTITY: {"agent_id": agent_id, "session_label": role},
                COL_CLAIM_EPOCH: 1,
            },
            "conflict_columns": ["external_id"],
        },
    )


class _FakeClient:
    instances: list[_FakeClient] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.pid = 4242
        self.started = False
        self.closed = False
        self.sent: list[str] = []
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def alive(self) -> bool:
        return self.started and not self.closed

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, _grace_seconds: float) -> None:
        self.closed = True


class _ClaudeParityChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _ClaudeParityDriver:
    def __init__(self, host: str) -> None:
        self.host = host
        self.channel = _ClaudeParityChannel()

    def spawn(self, spec: dict[str, object]) -> str:
        return f"{self.host}:{spec['agent_instance_id']}"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _ClaudeParityChannel:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def test_runtime_registry_is_orthogonal() -> None:
    headless, headless_name = resolve_host_driver("headless", "codex")
    tmux, tmux_name = resolve_host_driver("tmux", "codex")
    _check(
        isinstance(headless, CodexAppServerHostDriver) and headless_name == "headless",
        "agent_runtime=codex + host=headless resolves the app-server driver",
    )
    _check(
        isinstance(tmux, CodexTmuxHostDriver) and tmux_name == "tmux",
        "agent_runtime=codex + host=tmux resolves the interactive driver",
    )


def test_claude_headless_and_tmux_use_the_same_composite_state_machine() -> None:
    """Accepted-design parity: Claude hosts traverse prepare/turn/ACK identically."""
    outcomes: list[bool] = []
    for host in ("headless", "tmux"):
        key = (session_hosts.AGENT_RUNTIME_CLAUDE_CODE, host)
        prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
        driver = _ClaudeParityDriver(host)
        session_hosts._REGISTRY[key] = driver  # noqa: SLF001
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = RealShapeState()
                spec = _composite_spec(
                    root,
                    f"mdp-claude-{host}",
                    host,
                    agent_runtime=session_hosts.AGENT_RUNTIME_CLAUDE_CODE,
                )
                result = dispatch_managed_work(
                    state,  # type: ignore[arg-type]
                    spec,
                    _composite_spawn_request(spec, host),
                )
                attempt_id = str(result["attempt"]["agent_instance_id"])
                backfill_registration(
                    state,  # type: ignore[arg-type]
                    agent_instance_id=attempt_id,
                    agent_id=session_hosts.AGENT_RUNTIME_CLAUDE_CODE,
                    agent_session_id=f"ases-{attempt_id}",
                )
                _seed_worker_binding(
                    state,
                    role=spec.role_name,
                    instance=attempt_id,
                    agent_id=session_hosts.AGENT_RUNTIME_CLAUDE_CODE,
                )
                active = report_managed_dispatch(
                    state,  # type: ignore[arg-type]
                    dispatch_id=spec.dispatch_id,
                    event_id=f"evt-claude-{host}-ack",
                    event_kind="ack",
                    attempt_agent_instance_id=attempt_id,
                    actor=DispatchActor(
                        attempt_id,
                        f"ases-{attempt_id}",
                        "live_peer_binding",
                    ),
                    prior_version=1,
                    payload={
                        "brief_sha256": spec.brief_sha256,
                        "role_binding": spec.role_name,
                        "scope_readback_sha256": "a" * 64,
                        "plan_sha256": "b" * 64,
                    },
                    observed_at=datetime.now(UTC),
                )
                outcomes.append(
                    result["dispatch"]["state"] == DISPATCH_UPTAKE_PENDING
                    and active["state"] == DISPATCH_ACTIVE
                    and len(driver.channel.sent) == 1
                )
        finally:
            if prior is None:
                session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
            else:
                session_hosts._REGISTRY[key] = prior  # noqa: SLF001
    _check(
        all(outcomes),
        "Claude headless+tmux composite parity reaches transport-pending then model ACK",
    )


def test_local_name_reaches_codex_registration_identity_on_both_hosts() -> None:
    """F8: local_name, not lane_id, is the registered/displayed identity."""
    spec = {
        "agent_instance_id": "agi-local-name",
        "lane_id": "scheduler-action-definition-landing",
        "local_name": "Git-Controller",
    }
    headless = CodexAppServerHostDriver._spawn_identity(spec)  # noqa: SLF001
    tmux = CodexTmuxHostDriver._spawn_identity(spec)  # noqa: SLF001
    env = _identity_env(
        agent_instance_id=headless.agent_instance_id,
        agent_session_id=headless.agent_session_id,
        label=headless.label,
        solet_name="fixture-solet",
        solet_bin="/bin/true",
        transport="watch",
    )
    _check(headless.label == "Git-Controller", "F8 Codex headless uses local_name")
    _check(tmux.label == "Git-Controller", "F8 Codex tmux uses local_name")
    _check(
        env["AGENT_SESSION_LABEL"] == "Git-Controller",
        "F8 watch registration environment advertises local_name",
    )


def test_codex_headless_composite_requires_model_ack() -> None:
    """Codex app-server submission and registration are not model uptake."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _FakeClient.instances.clear()
        driver = CodexAppServerHostDriver(
            codex_bin=_executable(root, "codex"),
            solet_bin=_executable(root / "venv" / "bin", "solet-bridge"),
            solet_name="testhom",
            codex_home=_codex_home(root),
            cwd=root,
            python_executable=str(root / "venv" / "bin" / "python3"),
            client_factory=_FakeClient,
        )
        key = (session_hosts.AGENT_RUNTIME_CODEX, "headless")
        prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
        session_hosts._REGISTRY[key] = driver  # noqa: SLF001
        try:
            state = RealShapeState()
            spec = _composite_spec(root, "mdp-codex-headless", "headless")
            result = dispatch_managed_work(
                state,  # type: ignore[arg-type]
                spec,
                _composite_spawn_request(spec, "headless"),
            )
            attempt_id = str(result["attempt"]["agent_instance_id"])
            backfill_registration(
                state,  # type: ignore[arg-type]
                agent_instance_id=attempt_id,
                agent_id="codex",
                agent_session_id=f"ases-{attempt_id}",
            )
            backfill_registration(
                state,  # type: ignore[arg-type]
                agent_instance_id=attempt_id,
                agent_id="codex",
                agent_session_id=f"ases-{attempt_id}",
            )
            _seed_worker_binding(state, role=spec.role_name, instance=attempt_id)
            pending = read_managed_dispatch(
                state,  # type: ignore[arg-type]
                spec.dispatch_id,
            )
            active = report_managed_dispatch(
                state,  # type: ignore[arg-type]
                dispatch_id=spec.dispatch_id,
                event_id="evt-headless-ack",
                event_kind="ack",
                attempt_agent_instance_id=attempt_id,
                actor=DispatchActor(
                    agent_instance_id=attempt_id,
                    agent_session_id=f"ases-{attempt_id}",
                    authority_source="live_peer_binding",
                ),
                prior_version=1,
                payload={
                    "brief_sha256": spec.brief_sha256,
                    "role_binding": spec.role_name,
                    "scope_readback_sha256": "a" * 64,
                    "plan_sha256": "b" * 64,
                },
                observed_at=datetime.now(UTC),
            )
            duplicate = report_managed_dispatch(
                state,  # type: ignore[arg-type]
                dispatch_id=spec.dispatch_id,
                event_id="evt-headless-ack",
                event_kind="ack",
                attempt_agent_instance_id=attempt_id,
                actor=DispatchActor(
                    agent_instance_id=attempt_id,
                    agent_session_id=f"ases-{attempt_id}",
                    authority_source="live_peer_binding",
                ),
                prior_version=1,
                payload={
                    "brief_sha256": spec.brief_sha256,
                    "role_binding": spec.role_name,
                    "scope_readback_sha256": "a" * 64,
                    "plan_sha256": "b" * 64,
                },
                observed_at=datetime.now(UTC),
            )
            competing_code = ""
            try:
                report_managed_dispatch(
                    state,  # type: ignore[arg-type]
                    dispatch_id=spec.dispatch_id,
                    event_id="evt-headless-competing-ack",
                    event_kind="ack",
                    attempt_agent_instance_id=attempt_id,
                    actor=DispatchActor(
                        agent_instance_id=attempt_id,
                        agent_session_id=f"ases-{attempt_id}",
                        authority_source="live_peer_binding",
                    ),
                    prior_version=1,
                    payload={
                        "brief_sha256": spec.brief_sha256,
                        "role_binding": spec.role_name,
                        "scope_readback_sha256": "c" * 64,
                        "plan_sha256": "d" * 64,
                    },
                    observed_at=datetime.now(UTC),
                )
            except DispatchError as exc:
                competing_code = exc.code
        finally:
            if prior is None:
                session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
            else:
                session_hosts._REGISTRY[key] = prior  # noqa: SLF001
        session = read_managed_session(state, attempt_id)  # type: ignore[arg-type]
        _check(pending["state"] == DISPATCH_UPTAKE_PENDING, "Codex registration stays uptake_pending")
        _check(active["state"] == DISPATCH_ACTIVE, "Codex model-turn ACK activates the dispatch")
        _check(
            session["agent_session_id"] == f"ases-{attempt_id}"
            and active["current_agent_instance_id"] == attempt_id,
            "09 real re-registration preserves managed attempt lineage",
        )
        _check(
            duplicate["version"] == active["version"]
            and competing_code == "stale_dispatch_version",
            "09 duplicate/competing ACK order is idempotent and causal",
        )
        _check(session["first_turn_delivered"] is True, "Codex first-turn receipt is durable on attempt")
        _check(len(_FakeClient.instances[-1].sent) == 1, "Codex app-server receives exactly one first turn")


class _ManagedTmuxRun:
    def __init__(self) -> None:
        self.alive = True
        self.literal_sent = False
        self.enter_sent = False

    def __call__(self, argv: list[str], **_kwargs: Any) -> Any:
        if argv[1:2] == ["-V"]:
            return SimpleNamespace(returncode=0, stdout="tmux 3.3a", stderr="")
        if "has-session" in argv:
            return SimpleNamespace(returncode=0 if self.alive else 1, stdout="", stderr="")
        if "capture-pane" in argv:
            if self.enter_sent:
                content = "Working\n›"
            elif self.literal_sent:
                content = "› managed first turn"
            else:
                content = "› Ask Codex to do anything"
            return SimpleNamespace(returncode=0, stdout=content, stderr="")
        if "send-keys" in argv and "-l" in argv:
            self.literal_sent = True
        if argv[-1:] == ["Enter"]:
            self.enter_sent = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_codex_tmux_disappearance_converges() -> None:
    """Codex tmux registration cannot keep a vanished host falsely live."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = _ManagedTmuxRun()
        driver = CodexTmuxHostDriver(
            codex_bin=_executable(root, "codex"),
            tmux_bin=_executable(root, "tmux"),
            solet_bin=_executable(root / "venv" / "bin", "solet-bridge"),
            solet_name="testhom",
            codex_home=_codex_home(root),
            cwd=root,
            python_executable=str(root / "venv" / "bin" / "python3"),
            run_fn=run,
        )
        key = (session_hosts.AGENT_RUNTIME_CODEX, "tmux")
        prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
        session_hosts._REGISTRY[key] = driver  # noqa: SLF001
        try:
            state = RealShapeState()
            spec = _composite_spec(root, "mdp-codex-tmux", "tmux")
            result = dispatch_managed_work(
                state,  # type: ignore[arg-type]
                spec,
                _composite_spawn_request(spec, "tmux"),
            )
            attempt_id = str(result["attempt"]["agent_instance_id"])
            backfill_registration(
                state,  # type: ignore[arg-type]
                agent_instance_id=attempt_id,
                agent_id="codex",
                agent_session_id=f"ases-{attempt_id}",
            )
            run.alive = False
            sweep = sweep_managed_dispatches(
                state,  # type: ignore[arg-type]
                now=datetime.now(UTC),
            )
            dispatch = read_managed_dispatch(
                state,  # type: ignore[arg-type]
                spec.dispatch_id,
            )
            session = read_managed_session(state, attempt_id)  # type: ignore[arg-type]
        finally:
            if prior is None:
                session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
            else:
                session_hosts._REGISTRY[key] = prior  # noqa: SLF001
        _check(result["dispatch"]["state"] == DISPATCH_UPTAKE_PENDING, "tmux transport is pending before ACK")
        _check(sweep["dead"] == 1, "tmux disappearance is detected in the next supervisor interval")
        _check(dispatch["state"] == DISPATCH_WORKER_LOST, "tmux disappearance yields worker_lost")
        _check(session["lifecycle_state"] == "terminated", "vanished tmux attempt cannot remain live")


def test_runtime_is_schema_and_restart_sticky() -> None:
    column = get_managed_session_schema().columns["agent_runtime"]
    _check(
        column.default == "claude_code" and column.not_null is not True,
        "managed_session.agent_runtime is declarative nullable TEXT with claude_code default",
    )
    request = _spawn_session_request_from_params(
        {
            "role_class": "ephemeral",
            "lane_id": "lane-codex",
            "brief_ref": "brief.md",
            "work_class": "read_only",
            "budget_line": "budget",
            "agent_runtime": "codex",
        },
        "operator:test",
    )
    _check(request.agent_runtime == "codex", "spawn transport preserves agent_runtime=codex")
    defaulted = _spawn_session_request_from_params(
        {
            "role_class": "ephemeral",
            "lane_id": "lane-claude",
            "brief_ref": "brief.md",
            "work_class": "read_only",
            "budget_line": "budget",
        },
        "operator:test",
    )
    _check(
        defaulted.agent_runtime == "claude_code",
        "omitted runtime keeps backward-compatible claude_code behavior",
    )
    params = AgentMessagingPlugin()._build_restart_spawn_params(  # noqa: SLF001
        {
            "brief_ref": "brief.md",
            "work_class": "read_only",
            "budget_line": "budget",
            "agent_runtime": "codex",
            "host": "headless",
        },
        "ephemeral",
        "lane-codex",
        "",
    )
    _check(
        params["agent_runtime"] == "codex",
        "restart carries codex runtime instead of silently respawning Claude",
    )


def test_headless_spawn_uses_codex_native_config_and_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _FakeClient.instances.clear()
        driver = CodexAppServerHostDriver(
            codex_bin=_executable(root, "codex"),
            solet_bin=_executable(root / "venv" / "bin", "solet-bridge"),
            solet_name="testhom",
            codex_home=_codex_home(root),
            cwd=root,
            python_executable=str(root / "venv" / "bin" / "python3"),
            client_factory=_FakeClient,
        )
        host_ref = driver.spawn(
            {
                "agent_instance_id": "agi-codex-1",
                "lane_id": "cheap-review",
                "model": "gpt-5.6-luna",
                "effort": "low",
                "transport": "mcp",
                "role_class": "ephemeral",
                "brief_ref": "brief.md",
                "spawned_by_role": "Coordinator-Main",
            },
        )
        client = _FakeClient.instances[-1]
        argv = client.kwargs["argv"]
        env = client.kwargs["env"]
        _check(host_ref == "4242" and client.started, "headless spawn starts one persistent client")
        _check(
            "app-server" in argv and "--dangerously-bypass-hook-trust" in argv,
            "headless Codex launches persistent app-server with managed hook trust",
        )
        _check(
            any("model_reasoning_effort=\"low\"" == item for item in argv),
            "Codex effort is applied through Codex's own config surface",
        )
        _check(
            any(
                'mcp_servers.testhom.env.AGENT_INSTANCE_ID="agi-codex-1"' == item
                for item in argv
            ),
            "MCP bridge identity is overridden per managed spawn",
        )
        _check(
            any(
                'mcp_servers.testhom.env.AGENT_ROLE_AUTOBIND="0"' == item
                for item in argv
            ),
            "managed MCP registration preserves label without binding it as a role",
        )
        _check(
            env["AGENT_IDENTITY"] == "codex"
            and env["AGENT_SESSION_ID"] == "ases-agi-codex-1"
            and "AGENT_ROLE" not in env,
            "process env uses Codex identity and grants no role at launch",
        )
        _check(
            client.kwargs["model"] == "gpt-5.6-luna",
            "cheaper Codex model selection reaches thread/start",
        )


def test_codex_provider_overlay_is_refused_loud() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        driver = CodexAppServerHostDriver(
            codex_bin=_executable(root, "codex"),
            solet_bin=_executable(root / "venv" / "bin", "solet-bridge"),
            solet_name="testhom",
            codex_home=_codex_home(root),
            cwd=root,
            python_executable=str(root / "venv" / "bin" / "python3"),
            client_factory=_FakeClient,
        )
        raised = False
        try:
            driver.spawn(
                {
                    "agent_instance_id": "agi-provider-refuse",
                    "transport": "mcp",
                    "provider_env": {"CLAUDE_CODE_USE_BEDROCK": "1"},
                },
            )
        except HostCannotSpawnError as exc:
            raised = True
            _check(
                "provider_unsupported_for_runtime" in str(exc),
                "provider refusal exposes the stable error token",
            )
        _check(raised, "Codex never silently ignores a Claude provider overlay")


def test_codex_environment_does_not_adopt_parent_runtime_or_provider() -> None:
    with patch.dict(
        os.environ,
        {
            "CODEX_THREAD_ID": "parent-thread",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_AUTH_TOKEN": "secret-never-forward",
            "OPENAI_API_KEY": "codex-auth-remains-available",
        },
        clear=True,
    ):
        env = _identity_env(
            agent_instance_id="agi-child",
            agent_session_id="ases-child",
            label="child",
            solet_name="testhom",
            solet_bin="/release/venv/bin/solet-bridge",
            transport="mcp",
        )
    _check(
        "CODEX_THREAD_ID" not in env,
        "managed Codex never adopts the operator's parent thread identity",
    )
    _check(
        "CLAUDE_CODE_USE_BEDROCK" not in env and "ANTHROPIC_AUTH_TOKEN" not in env,
        "managed Codex receives no inherited Claude provider overlay or secret",
    )
    _check(
        env["OPENAI_API_KEY"] == "codex-auth-remains-available",
        "Codex-native authentication remains available after provider isolation",
    )


def test_codex_environment_exposes_resolved_solet_cli() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        venv_bin = Path(tmp) / "release" / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        solet_bin = _executable(venv_bin, "solet-bridge")
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True):
            env = _identity_env(
                agent_instance_id="agi-release-worker",
                agent_session_id="ases-release-worker",
                label="release-worker",
                solet_name="testhom",
                solet_bin=solet_bin,
                transport="watch",
            )
    _check(
        env["AGENT_WAKE_CLI"] == solet_bin,
        "managed Codex receives the resolved release CLI as AGENT_WAKE_CLI",
    )
    _check(
        env["PATH"].split(os.pathsep)[0] == str(venv_bin),
        "managed Codex can invoke plain solet from the release venv on a minimal PATH",
    )


def test_verify_config_fails_loud_without_coordination_plugin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        driver = CodexAppServerHostDriver(
            codex_bin=_executable(root, "codex"),
            solet_bin=_executable(root / "venv" / "bin", "solet-bridge"),
            solet_name="testhom",
            codex_home=_codex_home(root, plugin_enabled=False),
            cwd=root,
            python_executable=str(root / "venv" / "bin" / "python3"),
        )
        remedies = driver.verify_config(transport="mcp")
        _check(
            any("coordination-hooks" in remedy for remedy in remedies),
            "missing/disabled Codex coordination hooks fail loud with a remedy",
        )


def _codex_tree(root: Path, *, helper: str | None) -> str:
    """A fake Codex install; ``helper`` is the code-mode host's shell body."""
    codex_bin = _executable(root / "cask" / "bin", "codex")
    if helper is not None:
        host = Path(codex_bin).parent / "codex-code-mode-host"
        host.write_text(helper)
        host.chmod(0o755)
    return codex_bin


def _codex_driver_kwargs(root: Path, codex_bin: str) -> dict[str, Any]:
    return {
        "codex_bin": codex_bin,
        "solet_bin": _executable(root / "venv" / "bin", "solet-bridge"),
        "solet_name": "testhom",
        "codex_home": _codex_home(root),
        "cwd": root,
        "python_executable": str(root / "venv" / "bin" / "python3"),
        # The production default (5.0s). The probe's discriminator is whether
        # the helper RETURNS, so the "no remedy" direction needs headroom for a
        # loaded host: a healthy `sh` helper execs in ~0.03s solo but was
        # measured past 0.5s under a concurrent smoke battery
        # (lane-fable-parallelize-run-smokes, 2026-08-30). The wedged test
        # overrides this back down — its "remedy present" direction cannot be
        # flipped by load, only made slower.
        "code_mode_probe_seconds": 5.0,
    }


def test_wedged_code_mode_host_refuses_the_spawn_on_both_hosts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        codex_bin = _codex_tree(root, helper="#!/bin/sh\nsleep 30\n")
        kwargs = _codex_driver_kwargs(root, codex_bin)
        # A sleeping helper exceeds ANY budget, so a short probe keeps the
        # wedged tier fast without weakening its discrimination.
        kwargs["code_mode_probe_seconds"] = 0.5
        remedies = CodexAppServerHostDriver(**kwargs).verify_config(transport="mcp")
        _check(
            any("did not return within" in remedy for remedy in remedies),
            "a code-mode host that never returns fails loud with a remedy",
        )
        _check(
            any("codex-code-mode-host" in remedy for remedy in remedies),
            "the remedy names the helper path an operator has to repair",
        )
        tmux_remedies = CodexTmuxHostDriver(**kwargs).verify_config(transport="mcp")
        _check(
            any("did not return within" in remedy for remedy in tmux_remedies),
            "the tmux host inherits the code-mode host preflight, not just headless",
        )
        raised = False
        try:
            CodexAppServerHostDriver(**kwargs, client_factory=_FakeClient).spawn(
                {"agent_instance_id": "agi-code-mode-wedge", "transport": "mcp"},
            )
        except HostCannotSpawnError:
            raised = True
        _check(raised, "a lane is never spawned into a silently wedged code-mode host")


def test_healthy_and_absent_code_mode_hosts_do_not_block_a_spawn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        healthy = _codex_tree(
            root / "healthy", helper="#!/bin/sh\necho 'Usage: codex-code-mode-host'\n",
        )
        _check(
            not [
                remedy
                for remedy in CodexAppServerHostDriver(
                    **_codex_driver_kwargs(root / "healthy", healthy),
                ).verify_config(transport="mcp")
                if "code-mode-host" in remedy
            ],
            "a helper that returns promptly raises no code-mode remedy",
        )
        absent = _codex_tree(root / "absent", helper=None)
        _check(
            not [
                remedy
                for remedy in CodexAppServerHostDriver(
                    **_codex_driver_kwargs(root / "absent", absent),
                ).verify_config(transport="mcp")
                if "code-mode-host" in remedy
            ],
            "an install with no code-mode helper is not invented into a failure",
        )


def test_dangling_code_mode_host_symlink_fails_loud() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        codex_bin = _codex_tree(root, helper=None)
        host = Path(codex_bin).parent / "codex-code-mode-host"
        host.symlink_to(Path(codex_bin).parent / "codex-code-mode-host.real")
        remedies = CodexAppServerHostDriver(
            **_codex_driver_kwargs(root, codex_bin),
        ).verify_config(transport="mcp")
        _check(
            any("dangling symlink" in remedy for remedy in remedies),
            "a broken symlink repair is reported, not silently skipped as absent",
        )


def test_solet_cli_resolves_from_active_venv_when_path_is_minimal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        venv_bin = Path(tmp) / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        sibling = _executable(venv_bin, "solet-bridge")
        # Patch target followed the function to solet_cli (2026-08-14 hoist).
        with patch("agent_messaging_plugin.solet_cli.shutil.which", return_value=None):
            resolved = _resolve_solet_bin(
                None,
                python_executable=str(venv_bin / "python3"),
            )
        _check(
            resolved == sibling,
            "managed Codex resolves solet beside the active venv Python when PATH omits venv/bin",
        )
        _check(
            _resolve_solet_bin(
                sibling, python_executable=str(venv_bin / "python3"),
            ) == sibling,
            "an explicitly injected private solet path remains authoritative",
        )


def test_tmux_driver_resolves_codex_and_tmux_from_local_bin_when_path_is_minimal() -> None:
    """LaunchAgent PATH omits ~/.local/bin even though Codex is installed there."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        local_bin = home / ".local" / "bin"
        codex = _executable(local_bin, "codex")
        tmux = _executable(local_bin, "tmux")
        with (
            patch("agent_messaging_plugin.codex_tmux.shutil.which", return_value=None),
            patch.dict(os.environ, {"HOME": str(home)}, clear=False),
        ):
            driver = CodexTmuxHostDriver()
        _check(
            driver._codex_bin == codex,  # noqa: SLF001
            "Codex tmux driver resolves codex from ~/.local/bin when PATH misses it",
        )
        _check(
            driver._tmux_bin == tmux,  # noqa: SLF001
            "Codex tmux driver resolves tmux from ~/.local/bin when PATH misses it",
        )


def test_app_server_channel_translates_clear_compact_and_active_turn() -> None:
    client = _CodexAppServerClient(
        argv=["codex"],
        cwd=Path("/tmp"),
        env={},
        developer_instructions="authority",
        model="gpt-5.6-luna",
    )
    calls: list[tuple[str, dict[str, object]]] = []
    next_thread = iter(("thread-after-clear",))

    def fake_request(method: str, params: Any) -> dict[str, Any]:
        calls.append((method, dict(params)))
        if method == "thread/start":
            return {"thread": {"id": next(next_thread)}}
        return {}

    client._request = fake_request  # type: ignore[method-assign]  # noqa: SLF001
    client._thread_id = "thread-original"  # noqa: SLF001
    client.send("/clear")
    _check(
        client._thread_id == "thread-after-clear",  # noqa: SLF001
        "/clear rotates only the Codex backend thread inside the same channel object",
    )
    client.send("/compact")
    _check(
        calls[-1] == ("thread/compact/start", {"threadId": "thread-after-clear"}),
        "/compact maps to Codex thread/compact/start",
    )
    client._active_turn_id = "turn-active"  # noqa: SLF001
    client.send("follow-up")
    _check(
        calls[-1][0] == "turn/steer"
        and calls[-1][1]["expectedTurnId"] == "turn-active",
        "an active app-server Codex turn receives explicit work via turn/steer",
    )


class _PaneRun:
    def __init__(self, captures: list[str]) -> None:
        self.captures = captures
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> Any:
        self.calls.append(argv)
        if "capture-pane" in argv:
            value = self.captures.pop(0) if len(self.captures) > 1 else self.captures[0]
            return SimpleNamespace(returncode=0, stdout=value, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        self.value += 0.1
        return self.value

    def sleep(self, _seconds: float) -> None:
        self.value += 0.1


def test_tmux_channel_verifies_styled_pickup() -> None:
    """Updated for the baseline-gate fix (driver-channel strand fix,
    2026-08-14): the pre-send idle screen and the post-render composed
    screen must now be DISTINCT strings — under the fix, ``_wait_until_ready``'s
    return value becomes the baseline ``_wait_until_stable`` refuses to
    declare "stable" against, so a fixture that reused the same string for
    both (the pre-fix version of this test) would never see the composed
    text and only ever observe the fail-closed timeout, not a real pickup."""
    idle = "› Ask Codex to do anything"
    composed = "› review this"
    run = _PaneRun([idle, idle, composed, composed, "Working\n›"])
    clock = _Clock()
    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux",
        session="codex-test",
        run_fn=run,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
        stable_samples=1,
        verify_timeout_seconds=2.0,
    )
    channel.send("review this")
    literal_index = next(i for i, call in enumerate(run.calls) if "-l" in call)
    enter_index = next(i for i, call in enumerate(run.calls) if call[-1] == "Enter")
    _check(literal_index < enter_index, "tmux text and Enter are separate ordered operations")
    _check(
        any("capture-pane" in call and "-e" in call for call in run.calls),
        "tmux pickup verification uses styled capture-pane -e",
    )


def test_tmux_channel_enter_waits_for_baseline_change() -> None:
    """RED-FIRST (driver-channel strand fix, 2026-08-14, hermetically
    reproduced against the unmodified class — workbench/2026-08-14_driver_
    channel_strand_fix_report_lane_d.md): capture-pane returns the pre-send
    idle screen until a TIME threshold (not gated on Enter, so neither the
    pre-fix nor the fixed algorithm's own control flow can move it), then
    the real composed text, then a third distinct screen confirming
    submission. Enter must only fire once the pane has actually shown the
    composed text — never while it still shows the idle screen the pre-fix
    code could mistake for "stable".

    FAILING MUTATION: drop the ``current != baseline`` conjunct from
    ``_wait_until_stable`` (i.e. revert to the pre-fix comparison) — the
    unfixed code declares stability against 3 identical idle-screen samples
    well before the render threshold, and the assertion below reds (the
    pane content observed immediately before Enter is the idle screen, not
    the composed one).
    """
    idle = "› Ask Codex to do anything"
    composed = "› a slow paste"
    submitted = "Working\n›"
    render_at_t = 1.0
    submit_confirm_at_t = 3.0
    clock = {"t": 0.0}
    last_capture: dict[str, str | None] = {"value": None}
    last_capture_before_enter: dict[str, str | None] = {"value": None}

    def now_fn() -> float:
        return clock["t"]

    def sleep_fn(seconds: float) -> None:
        clock["t"] += seconds

    def run_fn(cmd: list[str], **_kw: Any) -> Any:
        if "capture-pane" in cmd:
            if clock["t"] >= submit_confirm_at_t:
                content = submitted
            elif clock["t"] >= render_at_t:
                content = composed
            else:
                content = idle
            last_capture["value"] = content
            return SimpleNamespace(returncode=0, stdout=content, stderr="")
        if cmd[-1:] == ["Enter"]:
            last_capture_before_enter["value"] = last_capture["value"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux",
        session="codex-baseline-gate",
        run_fn=run_fn,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
        stable_samples=3,
        verify_timeout_seconds=10.0,
    )
    channel.send("a slow paste")
    _check(
        last_capture_before_enter["value"] == composed,
        "Enter is sent only once the pane shows the ACTUAL composed text, "
        f"never the pre-send idle screen (observed: {last_capture_before_enter['value']!r})",
    )


def test_tmux_channel_refuses_two_enter_noops() -> None:
    """Updated for the baseline-gate fix (driver-channel strand fix,
    2026-08-14): a composer that NEVER visibly changes from the pre-send
    idle screen (the "dim ghost" case) is now refused inside
    ``_wait_until_stable`` itself — Enter is never sent at all, rather than
    the pre-fix behavior of blindly pressing Enter twice against unconfirmed
    content before giving up. Strictly safer: the class's own fail-closed
    contract ("never call ghost/composed text a delivered turn") now also
    covers the Enter keypress itself, not just the post-Enter confirmation."""
    run = _PaneRun(["› Ask Codex to do anything"])
    clock = _Clock()
    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux",
        session="codex-noop",
        run_fn=run,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
        stable_samples=1,
        verify_timeout_seconds=0.5,
    )
    raised = False
    try:
        channel.send("stranded")
    except DriverChannelSendError:
        raised = True
    enters = [call for call in run.calls if call[-1] == "Enter"]
    _check(
        len(enters) == 0,
        "a composer that never visibly differs from the pre-send baseline "
        "refuses BEFORE ever pressing Enter, not after two blind attempts",
    )
    _check(raised, "a pane that never stabilizes past its own baseline fails loud")


LIVE_CODEX_BUSY_STATUS_LINE = (
    "\u2022 Working (12m 07s \u00b7 esc to interrupt) \u00b7 Checking peer inbox before ending turn"
)
"""Verbatim from a real pane, ``tmux capture-pane -p`` on
``fleet-lane-cdx-fix-lm-studio-provisioning-2026-09-08-f0f3ca54`` (Codex
0.153.4) at 2026-09-09T04:0xZ, while that lane was 12 minutes into a turn."""


def _idle_pane(transcript: str) -> str:
    """An idle pane: some transcript, then Codex's empty-composer placeholder.

    The placeholder is NOT an idle signal by itself -- 0.153.4 renders it
    while working too (see :func:`test_ready_gate_reads_the_status_line_not_
    the_placeholder`), which is why these fixtures carry a transcript and a
    status line separately rather than toggling one string.
    """
    return f"{transcript}\n\n\u203a Ask Codex to do anything\n\n  gpt-5.6-terra high"


def test_idle_pane_is_driveable_though_its_transcript_says_working() -> None:
    """RED-FIRST (2026-09-09, iss_af7f6968): the permanent-wedge half.

    The readiness check used to reject a pane if the bare substring
    ``Working`` appeared ANYWHERE in the visible pane. That word is ordinary
    English and ordinary transcript text, so a genuinely idle, healthy pane
    became permanently undriveable the moment it happened to display it --
    silently, while the lane stayed registered, kept heartbeating, and read
    ``live``. The transcript line below is not invented: it is this lane's own
    dispatch brief, which every worker on this unit is told to read.

    FAILING MUTATION: put the bare substring back -- match ``"Working"``
    against the visible pane instead of :data:`_CODEX_BUSY_STATUS_RE`. The
    pane below never stops containing that word, so readiness can never be
    satisfied and ``insert`` raises instead of pasting.
    """
    idle = _idle_pane("**Working dir:** ~/Workspace/example")
    composed = _idle_pane("**Working dir:** ~/Workspace/example").replace(
        "\u203a Ask Codex to do anything", "\u203a dispatch text",
    )
    run = _PaneRun([idle, idle, composed, composed, "\u203a dispatch text submitted"])
    clock = _Clock()
    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux", session="codex-working-in-transcript", run_fn=run,
        sleep_fn=clock.sleep, now_fn=clock.now,
        stable_samples=1, verify_timeout_seconds=2.0,
    )
    pasted = ""
    try:
        channel.insert("dispatch text")
        pasted = "ok"
    except DriverChannelSendError as exc:
        pasted = f"RAISED: {exc}"
    _check(
        pasted == "ok",
        "an idle pane whose transcript merely contains the word 'Working' is "
        "still driveable (no permanent wedge)",
    )
    literal = [c for c in run.calls if "send-keys" in c and "-l" in c]
    _check(
        [c[-1] for c in literal] == ["dispatch text"],
        "the wedge fix still actually pastes the text, rather than passing by "
        "never attempting the send",
    )


def test_ready_gate_reads_the_status_line_not_the_placeholder() -> None:
    """CONTROL for the test above -- the true-positive that must NOT be lost.

    Narrowing a busy check is the risky direction, so this pins the other
    side: a genuinely busy pane must still refuse the paste. It also pins the
    measurement that forced the redesign -- Codex 0.153.4 renders its idle
    composer placeholder AND its busy status line AT THE SAME TIME, so the
    placeholder alone can never decide readiness and the status line is
    load-bearing.

    FAILING MUTATION: drop the busy check from ``_wait_until_ready`` (return
    on the placeholder alone). The fixture below shows the placeholder on
    every capture, so readiness passes immediately and text is pasted into a
    pane that is 12 minutes into someone else's turn.
    """
    busy = _idle_pane(f"prior output\n{LIVE_CODEX_BUSY_STATUS_LINE}")
    _check(
        "\u203a Ask Codex to do anything" in busy,
        "the live busy fixture really does show the idle placeholder too "
        "(the measurement this redesign rests on)",
    )
    run = _PaneRun([busy])
    clock = _Clock()
    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux", session="codex-mid-turn", run_fn=run,
        sleep_fn=clock.sleep, now_fn=clock.now,
        stable_samples=1, verify_timeout_seconds=2.0,
    )
    failure = ""
    try:
        channel.insert("must not paste")
    except DriverChannelSendError as exc:
        failure = str(exc)
    _check(bool(failure), "a genuinely mid-turn pane still refuses the paste")
    _check(
        [c for c in run.calls if "send-keys" in c] == [],
        "a mid-turn refusal sends no keystrokes at all",
    )


def test_busy_pane_failure_says_busy_not_broken() -> None:
    """RED-FIRST (2026-09-09): the half that blocked Project-Solet-Main.

    ``spawn_session`` delivers the bootstrap first turn itself, so a
    ``drive_session`` issued straight afterwards races a pane that is busy BY
    CONSTRUCTION for as long as that turn runs -- minutes, against a 10s
    readiness budget. Every readiness timeout used to raise the same sentence
    whatever the cause, so a healthy lane working normally was reported to the
    caller as a delivery failure indistinguishable from a wedged or dead pane.
    PS-Main read that as 9/9 broken dispatch.

    FAILING MUTATION: collapse ``_not_ready_detail`` back to the single
    message ``"never reached an idle prompt; text was not pasted."`` -- the
    busy status line then appears nowhere in the error and the caller cannot
    tell a busy lane from a broken one.
    """
    busy = _idle_pane(f"prior output\n{LIVE_CODEX_BUSY_STATUS_LINE}")
    run = _PaneRun([busy])
    clock = _Clock()
    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux", session="codex-mid-turn", run_fn=run,
        sleep_fn=clock.sleep, now_fn=clock.now,
        stable_samples=1, verify_timeout_seconds=2.0,
    )
    failure = ""
    try:
        channel.insert("must not paste")
    except DriverChannelSendError as exc:
        failure = str(exc)
    _check("still mid-turn" in failure, "a busy pane is reported as busy, not as unreachable")
    _check(
        "esc to interrupt" in failure,
        "the busy report quotes the real status line it measured, so a steward "
        "can see WHAT the pane was doing",
    )
    _check(
        "retry" in failure,
        "the busy report tells the caller the lane is healthy and the send is "
        "worth retrying",
    )


def test_tmux_channel_ready_check_survives_banner_scroll() -> None:
    """RED-FIRST (Lane M, 2026-08-23, workbench/2026-08-23_dispatch_lane_m_
    codex_drive_idle_detector.md): live-measured against a real running Codex
    tmux pane (``fleet-lane-c-inspect-target-3ab14add``, Codex v0.149.0)
    whose startup ``OpenAI Codex (v...)`` banner box had scrolled out of the
    visible pane after several turns of output, while the pane sat genuinely
    idle at ``› Ask Codex to do anything``. The pre-fix ``_wait_until_ready``
    required the literal banner text to be visible; once it scrolls off that
    condition can never be satisfied again, and a real idle pane is reported
    undriveable (``driver_delivery_failed``) forever.

    FAILING MUTATION: restore the old condition in ``_wait_until_ready``
    (``"OpenAI Codex" in visible and "›" in visible and not any busy
    marker``) — the fixture below never puts the banner text back on screen,
    so the unfixed code times out and raises here regardless of tuning.
    A fixture whose pane text still contained the banner would pass before
    and after the fix, proving nothing; this one only passes under the fix.
    """
    scrolled_idle = (
        "some prior turn's output, several screens below the banner box\n"
        "• Ran 1 command\n"
        "\n"
        "› Ask Codex to do anything\n"
        "\n"
        "  gpt-5.6-terra high · ~/Workspace/example"
    )
    composed = scrolled_idle.replace(
        "› Ask Codex to do anything", "› banner has scrolled off",
    )
    run = _PaneRun([scrolled_idle, scrolled_idle, composed, composed, "Working\n›"])
    clock = _Clock()
    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux",
        session="codex-banner-scrolled",
        run_fn=run,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
        stable_samples=1,
        verify_timeout_seconds=2.0,
    )
    channel.send("banner has scrolled off")
    enters = [call for call in run.calls if call[-1] == "Enter"]
    _check(len(enters) == 1, "a genuinely idle, banner-scrolled-off pane is driveable")


class _RegisterOnlyClient:
    def __init__(self) -> None:
        self.registered: list[dict[str, str]] = []

    def peer_register(self, **kwargs: str) -> dict[str, object]:
        self.registered.append(kwargs)
        return {"registered": True}

    def peer_claim_role(self, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("--no-claim must not call peer_claim_role")


class _FakeWatcherProcess:
    def __init__(self, pid: int = 5151) -> None:
        self.pid = pid
        self.stdout = None
        self.stderr = None


def test_headless_watch_transport_watcher_keeps_spool() -> None:
    """CDX-06 (2026-08-24): the watch-transport sidecar's own ``solet watch``
    invocation must NOT carry --no-spool. codex-0147-dead-spool-retirement
    (2026-08-13) disabled it because stock Codex then had no Stop-hook
    consumer at all; the plugin now ships ``inbox_consumer.py``, a
    SYNCHRONOUS Stop hook that observes this exact spool at every turn
    boundary, so an armed spool is no longer dead weight. Named failing
    mutation: re-adding ``--no-spool`` to ``_start_watcher``'s argv reds
    this leg."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _FakeClient.instances.clear()
        watcher_calls: list[list[str]] = []

        def fake_popen(argv: list[str], **_kwargs: Any) -> _FakeWatcherProcess:
            watcher_calls.append(argv)
            return _FakeWatcherProcess()

        driver = CodexAppServerHostDriver(
            codex_bin=_executable(root, "codex"),
            solet_bin=_executable(root / "venv" / "bin", "solet-bridge"),
            solet_name="testhom",
            codex_home=_codex_home(root),
            cwd=root,
            python_executable=str(root / "venv" / "bin" / "python3"),
            client_factory=_FakeClient,
            popen_fn=fake_popen,
        )
        driver.spawn(
            {
                "agent_instance_id": "agi-codex-watch-1",
                "lane_id": "watch-lane",
                "transport": "watch",
                "role_class": "ephemeral",
                "brief_ref": "brief.md",
                "spawned_by_role": "Coordinator-Main",
            },
        )
        _check(len(watcher_calls) == 1, "watch transport spawns exactly one watcher subprocess")
        argv = watcher_calls[0]
        _check(
            "--no-spool" not in argv,
            f"watcher argv does NOT arm --no-spool (got {argv!r})",
        )
        _check(
            argv.count("--no-claim") == 1,
            "watcher argv still arms --no-claim exactly once (unrelated flag left untouched)",
        )


def test_tmux_watch_transport_pane_command_keeps_spool() -> None:
    """codex_tmux.py's watch-transport counterpart (CDX-06, 2026-08-24): the
    backgrounded ``solet watch`` sidecar launched inside the pane command
    must NOT carry --no-spool. Named failing mutation: re-adding
    ``--no-spool`` to ``_pane_command``'s watch_cmd argv reds this leg."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: Any) -> Any:
            calls.append(argv)
            if argv[1:2] == ["-V"]:
                return SimpleNamespace(returncode=0, stdout="tmux 3.3a", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        driver = CodexTmuxHostDriver(
            codex_bin=_executable(root, "codex"),
            tmux_bin=_executable(root, "tmux"),
            solet_bin=_executable(root / "venv" / "bin", "solet-bridge"),
            solet_name="testhom",
            codex_home=_codex_home(root),
            cwd=root,
            python_executable=str(root / "venv" / "bin" / "python3"),
            run_fn=fake_run,
        )
        driver.spawn(
            {
                "agent_instance_id": "agi-codex-tmux-watch-1",
                "lane_id": "tmux-watch-lane",
                "transport": "watch",
                "role_class": "ephemeral",
                "brief_ref": "brief.md",
                "spawned_by_role": "Coordinator-Main",
            },
        )
        new_session_call = next(call for call in calls if "new-session" in call)
        pane_command = new_session_call[-1]
        pane_tokens = shlex.split(pane_command)
        release_cli = str(root / "venv" / "bin" / "solet-bridge")
        codex_path_override = next(
            value for value in pane_tokens
            if value.startswith("shell_environment_policy.set.PATH=")
        )
        path_env = next(
            value for value in new_session_call
            if value.startswith("PATH=")
        )
        wake_cli_env = next(
            value for value in new_session_call
            if value.startswith("AGENT_WAKE_CLI=")
        )
        _check(
            path_env.removeprefix("PATH=").split(os.pathsep)[0]
            == str(root / "venv" / "bin"),
            "tmux receives the resolved CLI directory at the front of PATH",
        )
        _check(
            wake_cli_env == f"AGENT_WAKE_CLI={release_cli}",
            "tmux receives the resolved absolute AGENT_WAKE_CLI",
        )
        _check(
            str(root / "venv" / "bin") in codex_path_override,
            "Codex shell policy receives the release CLI directory",
        )
        _check(
            "--no-spool" not in pane_tokens,
            f"tmux pane command does NOT arm the watch sidecar with --no-spool "
            f"(got {pane_command!r})",
        )
        _check(
            pane_tokens.count("--no-claim") == 1,
            "tmux pane command still arms --no-claim exactly once (unrelated flag left untouched)",
        )


def test_watch_registration_does_not_claim_label() -> None:
    client = _RegisterOnlyClient()
    result = _register_without_claim(
        client,  # type: ignore[arg-type]
        WatchIdentity(
            role="lane-label-not-a-role",
            agent_id="codex",
            agent_session_id="ases-agi-codex-watch",
            agent_instance_id="agi-watch-codex",
        ),
    )
    _check(len(client.registered) == 1, "--no-claim still registers durable presence")
    _check(
        result == {"claimed": False, "reason": "managed_registration_only"},
        "--no-claim returns an explicit non-claim result",
    )


def test_real_tmux_parked_composer_requires_interrupt_before_drive() -> None:
    """A parked Codex-like pane strands a drive until an Escape recovery.

    RED FIRST: current ``_CodexTmuxDriverChannel`` exposes no interruption
    seam.  The first send therefore leaves its text visibly composed and
    raises the established ``did not stabilize before Enter`` error.  The
    post-repair leg can issue exactly one Escape, wait for the real idle
    placeholder, and then prove the subsequent text is consumed.

    The refusal samples are scripted to be byte-distinct, so the assertion
    cannot depend on a Python spinner thread winning a scheduling race.  The
    stranded-composer and post-Escape recovery observations remain a real,
    uniquely named tmux session with a disposable raw-terminal stand-in; it
    never targets a fleet pane or starts a Codex model process.
    Failing mutation: remove ``interrupt_park`` or call it after ``send``.
    """
    tmux_bin = shutil.which("tmux")
    if tmux_bin is None:
        # AMBIENT DEPENDENCE, NOT A DEFECT. This leg needs a real tmux binary on
        # the host; every other leg here is hermetic. Failing hard made this smoke
        # 1 of the 14 BLOCKING rows in the r21 born-clone verdict (sealed
        # 254700866ab0…) with "real-tmux parked-composer proof requires a tmux
        # binary" — 66 legs passed, 1 failed, and the seed was refused for an
        # absent host tool rather than anything about the seed. The sibling
        # tmux_adapter_smoke.py has always handled the identical condition by
        # skipping, which is why it sits in that same verdict's TOLERATED
        # environment-dependent rows. Repaired to match it (Architect
        # arm-149e5889 A): the remaining legs still run and report their counts,
        # any real failure among them still exits 1, and only an otherwise-clean
        # run degrades to the gate's exit-77 skip.
        global _real_tmux_tier_skipped
        _real_tmux_tier_skipped = True
        print("  SKIP  real-tmux parked-composer proof: no tmux binary on this host")
        return
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        parked_stub = root / "parked-codex-stub.py"
        parked_stub.write_text(
            "import os\n"
            "import sys\n"
            "import termios\n"
            "import threading\n"
            "import time\n"
            "import tty\n"
            "fd = sys.stdin.fileno()\n"
            "prior = termios.tcgetattr(fd)\n"
            "tty.setraw(fd)\n"
            "def emit(text):\n"
            "    os.write(sys.stdout.fileno(), text.encode())\n"
            "parked = True\n"
            "composer = ''\n"
            "emit('Checking peer inbox before ending turn\\r\\n› Ask Codex to do anything')\n"
            "def spin():\n"
            "    marker = 0\n"
            "    while parked:\n"
            "        emit('\\r\\nPark poll ' + str(marker % 2))\n"
            "        marker += 1\n"
            "        time.sleep(0.02)\n"
            "threading.Thread(target=spin, daemon=True).start()\n"
            "try:\n"
            "    while True:\n"
            "        key = os.read(fd, 1)\n"
            "        if key == b'\\x1b':\n"
            "            parked = False\n"
            "            composer = ''\n"
            "            emit('\\x1b[2J\\x1b[H› Ask Codex to do anything\\r\\n')\n"
            "        elif parked:\n"
            "            if key not in (b'\\r', b'\\n'):\n"
            "                composer += key.decode(errors='replace')\n"
            "                emit('\\x1b[2K\\r› ' + composer)\n"
            "        elif key == b'\\r':\n"
            "            emit('\\r\\nWorking\\r\\n')\n"
            "        elif key != b'\\n':\n"
            "            composer += key.decode(errors='replace')\n"
            "            emit('\\x1b[2K\\r› ' + composer)\n"
            "finally:\n"
            "    termios.tcsetattr(fd, termios.TCSADRAIN, prior)\n",
            encoding="utf-8",
        )
        session = f"park-drive-red-{os.getpid()}-{time.monotonic_ns()}"
        subprocess.run(
            [tmux_bin, "new-session", "-d", "-s", session, sys.executable, str(parked_stub)],
            check=True, capture_output=True, text=True, timeout=5,
        )
        try:
            scripted_parked_captures = False
            refusal_scripted = False
            capture_marker = 0

            def run_fn(argv: list[str], **kwargs: Any) -> Any:
                """Make the refusal proof deterministic without faking recovery."""
                nonlocal capture_marker, refusal_scripted, scripted_parked_captures
                if "-l" in argv and not refusal_scripted:
                    scripted_parked_captures = True
                    refusal_scripted = True
                if scripted_parked_captures and "capture-pane" in argv:
                    capture_marker += 1
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"Park poll scripted-{capture_marker}",
                        stderr="",
                    )
                return subprocess.run(argv, **kwargs)

            channel = _CodexTmuxDriverChannel(
                tmux_bin=tmux_bin, session=session, run_fn=run_fn,
                verify_timeout_seconds=0.4, poll_interval_seconds=0.05, stable_samples=1,
            )
            stranded = False
            try:
                channel.send("parked composer proof")
            except DriverChannelSendError as exc:
                stranded = "did not stabilize before Enter" in str(exc)
            scripted_parked_captures = False
            capture = subprocess.run(
                [tmux_bin, "capture-pane", "-p", "-t", session],
                check=True, capture_output=True, text=True, timeout=5,
            )
            _check(stranded, "RED proof: parked pane refuses the pre-repair drive")
            _check(
                "parked composer proof" in capture.stdout,
                "RED proof: failed drive leaves its text visibly stranded in the composer",
            )
            interrupt_park = getattr(channel, "interrupt_park", None)
            if not callable(interrupt_park):
                _check(False, "RED proof: Codex tmux channel has no parked-pane interrupt seam")
                return
            interrupt_park()
            channel.send("recovered parked drive")
            capture = subprocess.run(
                [tmux_bin, "capture-pane", "-p", "-t", session],
                check=True, capture_output=True, text=True, timeout=5,
            )
            _check(
                "Working" in capture.stdout,
                "Escape recovery reaches a consumed post-park drive",
            )
            _check(
                capture.stdout.rstrip().endswith("Working"),
                "the recovered drive leaves no active composer text after submission",
            )
        finally:
            subprocess.run(
                [tmux_bin, "kill-session", "-t", session],
                check=False, capture_output=True, text=True, timeout=5,
            )


def main() -> int:
    tests = [
        test_runtime_registry_is_orthogonal,
        test_claude_headless_and_tmux_use_the_same_composite_state_machine,
        test_local_name_reaches_codex_registration_identity_on_both_hosts,
        test_codex_headless_composite_requires_model_ack,
        test_codex_tmux_disappearance_converges,
        test_runtime_is_schema_and_restart_sticky,
        test_headless_spawn_uses_codex_native_config_and_identity,
        test_codex_provider_overlay_is_refused_loud,
        test_codex_environment_does_not_adopt_parent_runtime_or_provider,
        test_codex_environment_exposes_resolved_solet_cli,
        test_verify_config_fails_loud_without_coordination_plugin,
        test_wedged_code_mode_host_refuses_the_spawn_on_both_hosts,
        test_healthy_and_absent_code_mode_hosts_do_not_block_a_spawn,
        test_dangling_code_mode_host_symlink_fails_loud,
        test_solet_cli_resolves_from_active_venv_when_path_is_minimal,
        test_tmux_driver_resolves_codex_and_tmux_from_local_bin_when_path_is_minimal,
        test_app_server_channel_translates_clear_compact_and_active_turn,
        test_tmux_channel_verifies_styled_pickup,
        test_tmux_channel_enter_waits_for_baseline_change,
        test_tmux_channel_refuses_two_enter_noops,
        test_tmux_channel_ready_check_survives_banner_scroll,
        test_idle_pane_is_driveable_though_its_transcript_says_working,
        test_ready_gate_reads_the_status_line_not_the_placeholder,
        test_busy_pane_failure_says_busy_not_broken,
        test_real_tmux_parked_composer_requires_interrupt_before_drive,
        test_headless_watch_transport_watcher_keeps_spool,
        test_tmux_watch_transport_pane_command_keeps_spool,
        test_watch_registration_does_not_claim_label,
    ]
    with tempfile.TemporaryDirectory() as raw:
        with RecordedLaneWorktreeFixture(Path(raw)) as fixture:
            for test in tests:
                test()
            provisioned_roles = {call.role_name for call in fixture.provisioning_calls}
            _check(
                fixture.has_recorded_provisioning()
                and {
                    "mdp-claude-headless-Builder",
                    "mdp-claude-tmux-Builder",
                    "mdp-codex-headless-Builder",
                    "mdp-codex-tmux-Builder",
                }.issubset(provisioned_roles),
                "recorded fixture contains both runtime dispatch paths under its temp root",
            )
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    # Counts are reported FIRST, and a real failure still exits 1: an absent host
    # binary must never mask a genuine regression in the hermetic legs.
    if _failed:
        return 1
    if _real_tmux_tier_skipped:
        print(
            f'{_SKIP_WITNESS_PREFIX}{{"reason": "ambient_executable_missing", "executable": "tmux"}}'
        )
        print(
            "SKIP: no tmux binary on this host -- the real-tmux parked-composer "
            "proof (_case_real_tmux_parked_composer) disclosed the gap rather than "
            f"running; the {_passed} hermetic checks ran and passed."
        )
        return _SKIP_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
