"""Qualify operator-owned Codex and Claude panes for the native tmux driver.

This is an explicit local registration declaration, not a spawn or role claim.
The pane, watcher ancestry and launcher identity must agree before any ledger
write. Unqualified operator registrations retain their manual host behavior.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.core.config.environment_config import EnvironmentConfig
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_updated

from .schema import SESSION_VISIBILITY_VISIBLE, TABLE_MANAGED_SESSION
from .session_hosts import OPERATOR_HOST
from .session_lifecycle_store import (
    ManagedSessionSpec,
    SessionNotFoundError,
    backfill_registration,
    insert_managed_session,
    read_managed_session,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

OPERATOR_CHECKOUT_MODE = "operator_existing_checkout"
_HOST_PATTERN = re.compile(r"operator-codex-[a-f0-9]{32}")


class OperatorHostQualificationError(RuntimeError):
    """An explicitly requested driver could not be safely bound."""


@dataclass(frozen=True, slots=True)
class OperatorCodexRegistration:
    """Identity declared by the native watcher on its own bridge."""

    host_ref: str
    agent_id: str
    agent_instance_id: str
    agent_session_id: str
    session_label: str
    parent_pid: int | None
    watcher_declared: bool


@dataclass(frozen=True, slots=True)
class OperatorClaudeRegistration:
    """Identity declared by a launcher-started Claude watcher."""

    host_ref: str
    agent_id: str
    agent_instance_id: str
    agent_session_id: str
    session_label: str
    parent_pid: int | None
    watcher_declared: bool


def _output(argv: list[str]) -> str:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorHostQualificationError(f"host probe failed: {argv[0]}") from exc
    if result.returncode:
        raise OperatorHostQualificationError(f"host probe failed: {argv[0]}")
    return result.stdout.strip()


def _same_owner_process(pid: int) -> tuple[int, str]:
    fields = _output(["/bin/ps", "-p", str(pid), "-o", "ppid=,uid=,comm="]).split(maxsplit=2)
    if len(fields) != 3 or int(fields[1]) != os.getuid():
        raise OperatorHostQualificationError("host process owner could not be verified")
    return int(fields[0]), fields[2]


def _tmux_environment_value(tmux: str, target: str, key: str) -> str:
    output = _output([tmux, "show-environment", "-t", target, key])
    prefix = f"{key}="
    if not output.startswith(prefix):
        raise OperatorHostQualificationError(f"operator host environment value unavailable: {key}")
    return output[len(prefix):]


def _verify_watcher_ancestry(
    watcher_pid: int, pane_pid: int, expected_executable: str,
) -> None:
    """Require the registering watcher to descend from the actual runtime pane."""
    _, executable = _same_owner_process(pane_pid)
    if Path(executable).name != expected_executable:
        raise OperatorHostQualificationError(
            f"operator pane has not exec'd {expected_executable}",
        )
    current = watcher_pid
    for _ in range(8):
        if current == pane_pid:
            return
        current, _ = _same_owner_process(current)
        if current <= 1:
            break
    raise OperatorHostQualificationError(
        f"registering watcher is not a child of the {expected_executable} pane",
    )


def _verify_declaration(request: OperatorCodexRegistration) -> None:
    valid = (
        request.agent_id == "codex"
        and request.watcher_declared
        and request.parent_pid is not None
        and request.parent_pid > 1
        and _HOST_PATTERN.fullmatch(request.host_ref)
        and re.fullmatch(r"agi-[a-f0-9]{32}", request.agent_instance_id)
        and request.agent_session_id.startswith("ases-")
        and request.session_label
    )
    if not valid:
        raise OperatorHostQualificationError("incomplete operator Codex watcher declaration")


def qualify_operator_codex_host(request: OperatorCodexRegistration) -> dict[str, str]:
    """Read only local OS/tmux facts; no identity inference from a role label."""
    _verify_declaration(request)
    tmux = shutil.which("tmux")
    if tmux is None:
        raise OperatorHostQualificationError("tmux is unavailable to the service")
    target = "=" + request.host_ref
    panes = _output([
        tmux, "list-panes", "-s", "-t", target,
        "-F", "#{pane_pid}\t#{pane_dead}\t#{pane_current_path}",
    ]).splitlines()
    if len(panes) != 1:
        raise OperatorHostQualificationError("operator host must contain exactly one pane")
    pane = panes[0].split("\t", maxsplit=2)
    if len(pane) != 3 or pane[1] != "0":
        raise OperatorHostQualificationError("operator host pane is dead or malformed")
    _verify_watcher_ancestry(int(request.parent_pid or 0), int(pane[0]), "codex")
    _verify_tmux_identity(tmux, target, request, verify_instance_id=True)
    return _host_metadata(tmux, target, pane[2])


def _verify_tmux_identity(
    tmux: str,
    target: str,
    request: OperatorCodexRegistration | OperatorClaudeRegistration,
    *,
    verify_instance_id: bool,
) -> None:
    expected = {
        "AGENT_SESSION_ID": request.agent_session_id,
        "AGENT_SESSION_LABEL": request.session_label,
        "FLEET_TRANSPORT": "watch",
        "SOLET_NAME": EnvironmentConfig.solet_name(),
    }
    if verify_instance_id:
        expected["AGENT_INSTANCE_ID"] = request.agent_instance_id
    for key, value in expected.items():
        actual = _tmux_environment_value(tmux, target, key)
        if actual != value:
            raise OperatorHostQualificationError(f"operator host identity mismatch: {key}")


def _host_metadata(tmux: str, target: str, pane_cwd: str) -> dict[str, str]:
    metadata = {
        key: _tmux_environment_value(tmux, target, env_key)
        for key, env_key in {
            "model": "OPERATOR_CODEX_MODEL", "effort": "OPERATOR_CODEX_EFFORT",
            "lane_repo_root": "OPERATOR_CODEX_CWD",
        }.items()
    }
    if not all(metadata.values()) or Path(metadata["lane_repo_root"]).resolve() != Path(pane_cwd).resolve():
        raise OperatorHostQualificationError("operator host model, effort or cwd could not be verified")
    return metadata


def _operator_row(
    state: StateManagementInterface,
    request: OperatorCodexRegistration | OperatorClaudeRegistration,
    metadata: dict[str, str],
    *,
    agent_runtime: str,
) -> dict[str, Any]:
    try:
        row = read_managed_session(state, request.agent_instance_id)
    except SessionNotFoundError:
        insert_managed_session(state, ManagedSessionSpec(
            agent_instance_id=request.agent_instance_id, lane_id="", brief_ref="",
            work_class="", budget_line="", host=OPERATOR_HOST, agent_runtime=agent_runtime,
            local_name=request.session_label, model=metadata["model"], effort=metadata["effort"],
            provisioning_mode=OPERATOR_CHECKOUT_MODE,
            lane_repo_root=metadata["lane_repo_root"], directed_by="operator_registration",
            visibility=SESSION_VISIBILITY_VISIBLE,
        ))
        backfill_registration(
            state, agent_instance_id=request.agent_instance_id,
            agent_id=request.agent_id, agent_session_id=request.agent_session_id,
        )
        row = read_managed_session(state, request.agent_instance_id)
    same_session = row.get("agent_session_id") == request.agent_session_id
    same_mode = row.get("provisioning_mode") == OPERATOR_CHECKOUT_MODE
    same_host = row.get("host") == "tmux" and row.get("host_ref") == request.host_ref
    unbound = row.get("host") == OPERATOR_HOST and not row.get("host_ref")
    if not (same_session and same_mode and (same_host or unbound)):
        raise OperatorHostQualificationError("refusing to adopt an existing or differently bound session")
    if row.get("lifecycle_state") not in {"live", "idle", "parked"}:
        raise OperatorHostQualificationError("operator session is no longer eligible for registration")
    return row


def _register_operator_host(
    state: StateManagementInterface | None,
    request: OperatorCodexRegistration | OperatorClaudeRegistration,
    *,
    agent_runtime: str,
    metadata: dict[str, str],
) -> dict[str, str]:
    """Bind a verified driver, fail closed, and return its exact readback.

    Existing managed workers and legacy manual sessions are never adopted.
    No role assignment, worktree provisioning, report deadline or TTL is added.
    """
    if state is None:
        raise OperatorHostQualificationError("state service is unavailable")
    row = _operator_row(state, request, metadata, agent_runtime=agent_runtime)
    filters = {
        "agent_instance_id": request.agent_instance_id,
        "agent_session_id": request.agent_session_id,
        "provisioning_mode": OPERATOR_CHECKOUT_MODE,
        "host": row["host"], "host_ref": row.get("host_ref") or {"op": "is_null"},
        "lifecycle_state": row["lifecycle_state"], "is_deleted": 0,
    }
    updates = {
        "host": "tmux",
        "host_ref": request.host_ref,
        "agent_runtime": agent_runtime,
    }
    changed = require_updated(state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_SESSION, "filters": filters}, dict[str, object](updates),
    ))
    if changed != 1:
        raise OperatorHostQualificationError("operator host registration lost its state predicate")
    persisted = read_managed_session(state, request.agent_instance_id)
    if any(persisted.get(key) != value for key, value in updates.items()):
        raise OperatorHostQualificationError("operator host registration readback mismatch")
    return {**updates, "qualification": "verified_local_watcher", **metadata}


def register_operator_codex_host(
    state: StateManagementInterface | None, request: OperatorCodexRegistration,
) -> dict[str, str]:
    """Bind a verified Codex driver and return its exact readback."""
    return _register_operator_host(
        state,
        request,
        agent_runtime="codex",
        metadata=qualify_operator_codex_host(request),
    )


def _verify_claude_declaration(request: OperatorClaudeRegistration) -> None:
    valid = (
        request.agent_id == "claude_code"
        and request.parent_pid is not None
        and request.parent_pid > 1
        and request.host_ref == request.session_label
        and "\n" not in request.host_ref
        and re.fullmatch(r"agi-[a-f0-9]{32}", request.agent_instance_id)
        and request.agent_session_id.startswith("ases-")
        and request.session_label
    )
    if not valid:
        raise OperatorHostQualificationError("incomplete operator Claude watcher declaration")


def claude_operator_tmux_host_ref(session_label: str) -> str:
    """Return a Claude launcher's same-named tmux host, if one exists locally.

    Managed Claude workers use a fleet-generated tmux name rather than their
    session label, so they deliberately do not enter the operator qualifier.
    """
    if not session_label or "\n" in session_label:
        return ""
    tmux = shutil.which("tmux")
    if tmux is None:
        return ""
    try:
        result = subprocess.run(
            [tmux, "has-session", "-t", "=" + session_label],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorHostQualificationError("Claude operator host probe failed") from exc
    if result.returncode == 0:
        return session_label
    if result.returncode == 1:
        return ""
    raise OperatorHostQualificationError("Claude operator host probe failed")


def qualify_operator_claude_host(request: OperatorClaudeRegistration) -> dict[str, str]:
    """Verify a launcher-started Claude pane without inferring its identity."""
    _verify_claude_declaration(request)
    tmux = shutil.which("tmux")
    if tmux is None:
        raise OperatorHostQualificationError("tmux is unavailable to the service")
    target = "=" + request.host_ref
    panes = _output([
        tmux, "list-panes", "-s", "-t", target,
        "-F", "#{pane_pid}\t#{pane_dead}\t#{pane_current_path}",
    ]).splitlines()
    if len(panes) != 1:
        raise OperatorHostQualificationError("operator host must contain exactly one pane")
    pane = panes[0].split("\t", maxsplit=2)
    if len(pane) != 3 or pane[1] != "0":
        raise OperatorHostQualificationError("operator host pane is dead or malformed")
    _verify_watcher_ancestry(int(request.parent_pid or 0), int(pane[0]), "claude")
    _verify_tmux_identity(tmux, target, request, verify_instance_id=False)
    return {"model": "", "effort": "", "lane_repo_root": pane[2]}


def register_operator_claude_host(
    state: StateManagementInterface | None, request: OperatorClaudeRegistration,
) -> dict[str, str]:
    """Bind a verified Claude launcher pane and return its exact readback."""
    return _register_operator_host(
        state,
        request,
        agent_runtime="claude_code",
        metadata=qualify_operator_claude_host(request),
    )
