"""Coding-agent plugin and lifecycle-roster qualification probes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ananta.core.plugins.profile_manifest import load_manifest_plugin_set

from .coordination_hook_installation import receipt_matches_hook_root, receipt_path
from .installation_doctor import (
    _boolean_probe,
    _call_payload,
    _evidence,
    _solet_call,
    blocked,
    truncated_solet_call_output,
)
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, evidence, result
from .setup_adapter_runtime import CommandOutcome, Runtime, read_json_object, resolve_executable
from .setup_plugin_operations import (
    command_output_truncated,
    plugin_list_rows,
    plugin_list_vector,
    plugin_row_visible,
    truncated_output_repair,
)


def plugin_visible(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    cli = "claude" if "claude" in request.operation_ref else "codex"
    selector = f"coordination-hooks@{request.name.replace('_', '-')}"
    executable = resolve_executable(runtime, cli)
    if executable is None:
        return blocked(
            request,
            f"{cli}_cli_missing",
            f"Install and authenticate the user-managed {cli} CLI before retrying.",
        )
    list_vector, listed = _plugin_list_outcome(request, runtime, cli, executable)
    if command_output_truncated(listed):
        return blocked(request, "output_truncated", truncated_output_repair(list_vector, listed))
    root = _installed_plugin_root(request, runtime, cli, selector, listed)
    manifest = _hook_manifest(request, cli, root)
    bound = _manifest_absolute(manifest, request.target)
    receipt_ok = cli != "claude" or (
        root is not None
        and _receipt_matches_request(request, root)
    )
    return _boolean_probe(
        request,
        evidence_id=f"{cli}_plugin_visible",
        ok=root is not None and bound and receipt_ok,
        observed=root is not None and bound and receipt_ok,
        source=str(manifest),
        repair=f"Install {selector}, bind its Python hooks, and repair its owner-qualified receipt.",
    )


def hook_behavior(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    cli = "claude" if "claude" in request.operation_ref else "codex"
    selector = f"coordination-hooks@{request.name.replace('_', '-')}"
    executable = resolve_executable(runtime, cli)
    if executable is None:
        return blocked(
            request,
            f"{cli}_cli_missing",
            f"Install and authenticate the user-managed {cli} CLI before retrying.",
        )
    list_vector, listed = _plugin_list_outcome(request, runtime, cli, executable)
    if command_output_truncated(listed):
        return blocked(request, "output_truncated", truncated_output_repair(list_vector, listed))
    root = _installed_plugin_root(request, runtime, cli, selector, listed)
    if root is None:
        return blocked(
            request,
            f"{cli}_plugin_cache_missing",
            f"Install {selector} through the supported {cli} plugin route.",
        )
    manifest = _hook_manifest(request, cli, root)
    receipt_failure = _receipt_failure(request, cli, root)
    if receipt_failure is not None:
        return receipt_failure
    vector = _behavior_vector(request, runtime, cli, manifest)
    if vector is None:
        return _boolean_probe(
            request, evidence_id=f"{cli}_hook_behavior", ok=False, observed=False,
            source="executable:node unresolved", repair="Resolve Node before proving the Codex hook behavior.",
        )
    outcome = runtime.run(
        vector,
        timeout_seconds=10,
        extra_env={"AGENT_SESSION_LABEL": "Doctor-Fixture", "SOLET_NAME": request.name},
        input_text="{}",
    )
    if command_output_truncated(outcome):
        return blocked(request, "output_truncated", truncated_output_repair(vector, outcome))
    active = outcome.ok and bool(outcome.stdout.strip())
    return _boolean_probe(
        request,
        evidence_id=f"{cli}_hook_behavior",
        ok=active,
        observed=active,
        source=f"{vector[1]}; executable:{vector[0]}",
        repair=f"Repair {cli} plugin cache and prove the Step Zero hook behaviorally.",
    )


def _receipt_failure(request: AdapterRequest, cli: str, root: Path) -> JsonObject | None:
    if cli != "claude" or _receipt_matches_request(request, root):
        return None
    return blocked(
        request,
        "coordination_receipt_drift",
        "Repair the selected Claude plugin through the supported installer; cache bytes do not match its receipt.",
    )


def _receipt_matches_request(request: AdapterRequest, root: Path) -> bool:
    return receipt_matches_hook_root(
        receipt_path(request.target / "profile"),
        root / "hooks",
        solet_name=request.name,
        app_home=request.target / "profile",
        plugin_selector=f"coordination-hooks@{request.name.replace('_', '-')}",
        interpreter=request.target / ".venv/bin/python3",
    )


def _behavior_vector(
    request: AdapterRequest, runtime: Runtime, cli: str, manifest: Path,
) -> tuple[str, str] | None:
    hook = manifest.parent / ("step_zero_reminder.py" if cli == "claude" else "step_zero_reminder.js")
    if cli == "claude":
        return str(request.target / ".venv/bin/python3"), str(hook)
    node = resolve_executable(runtime, "node")
    return (node, str(hook)) if node is not None else None


def _hook_manifest(request: AdapterRequest, cli: str, root: Path | None = None) -> Path:
    if root is not None:
        return root / "hooks/hooks.json"
    return (
        request.target
        / f"plugins/github_midwife_plugin/{cli}_plugin/coordination-hooks/hooks/hooks.json"
    )


def _plugin_list_outcome(
    request: AdapterRequest,
    runtime: Runtime,
    cli: str,
    executable: str,
) -> tuple[tuple[str, ...], CommandOutcome]:
    vector = plugin_list_vector(cli, request.name.replace("_", "-"), executable)
    return vector, runtime.run(vector, timeout_seconds=10)


def _installed_plugin_root(
    request: AdapterRequest,
    runtime: Runtime,
    cli: str,
    selector: str,
    listed: CommandOutcome,
) -> Path | None:
    rows = plugin_list_rows(cli, listed)
    if rows is None or not any(plugin_row_visible(cli, row, selector) for row in rows):
        return None
    if cli == "claude":
        return _claude_plugin_root(runtime.home, selector)
    return _codex_plugin_root(rows, selector, request.target)


def _claude_plugin_root(home: Path, selector: str) -> Path | None:
    registry = read_json_object(home / ".claude/plugins/installed_plugins.json")
    plugins = registry.get("plugins") if registry is not None else None
    rows = plugins.get(selector) if isinstance(plugins, dict) else None
    if not isinstance(rows, list):
        return None
    roots = [
        Path(install_path)
        for row in cast(list[JsonValue], rows)
        for install_path in [row.get("installPath") if isinstance(row, dict) else None]
        if isinstance(install_path, str) and Path(install_path).is_dir()
    ]
    return roots[0] if len(roots) == 1 else None


def _codex_plugin_root(
    installed: list[JsonObject],
    selector: str,
    target: Path,
) -> Path | None:
    expected = target / "plugins/github_midwife_plugin/codex_plugin/coordination-hooks"
    for row in installed:
        if _codex_plugin_row_matches(row, selector, expected):
            return expected
    return None


def _codex_plugin_row_matches(row: object, selector: str, expected: Path) -> bool:
    if not isinstance(row, dict):
        return False
    source = row.get("source")
    source_path = source.get("path") if isinstance(source, dict) else None
    return (
        row.get("pluginId") == selector
        and row.get("installed") is True
        and row.get("enabled") is True
        and source_path == str(expected)
        and expected.is_dir()
    )


def _manifest_absolute(path: Path, target: Path) -> bool:
    manifest = read_json_object(path)
    if manifest is None:
        return False
    expected = str(target / ".venv/bin/python3")
    commands = _manifest_commands(manifest)
    python_commands = [command for command in commands if "python3" in command]
    return bool(python_commands) and all(
        command.startswith(expected) for command in python_commands
    )


def _manifest_commands(manifest: JsonObject) -> list[str]:
    hooks = manifest.get("hooks")
    if not isinstance(hooks, dict):
        return []
    commands: list[str] = []
    for event_entries in hooks.values():
        commands.extend(_event_commands(event_entries))
    return commands


def _event_commands(event_entries: JsonValue) -> list[str]:
    if not isinstance(event_entries, list):
        return []
    commands: list[str] = []
    for entry in event_entries:
        nested = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(nested, list):
            continue
        commands.extend(
            str(hook["command"])
            for hook in nested
            if isinstance(hook, dict) and isinstance(hook.get("command"), str)
        )
    return commands


def selected_plugins(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    selected = _selected_coding_agents(request)
    if selected is None:
        return blocked(
            request,
            "coding_agent_selection_invalid",
            "Resolve one or both supported coding-agent selections before probing them.",
        )
    for agent in selected:
        cli = "claude" if agent == "claude_code" else agent
        reference = f"hydration::{cli}.probe_plugin"
        child = AdapterRequest(
            request.request_id,
            request.operation_id,
            reference,
            request.phase,
            request.probe_purpose,
            request.attempt,
            request.name,
            request.target,
            request.flow_source_revision,
            request.answers_fingerprint,
            request.approval_fingerprint,
            request.dry_run,
            request.timeout_seconds,
            request.public_inputs,
        )
        child_result = plugin_visible(child, runtime)
        child_error = child_result["error_kind"]
        if child_error in {"output_truncated", "codex_cli_missing", "claude_cli_missing"}:
            return blocked(request, child_error, str(child_result["repair"]))
        if child_result["checkpoint_status"] != "verified":
            return blocked(
                request, "coding_agent_plugin_missing", "Repair selected coding-agent plugins."
            )
    return _boolean_probe(request, "coding_agent_plugins", True, True, str(request.target), "")


def selected_hooks(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    selected = _selected_coding_agents(request)
    if selected is None:
        return blocked(
            request,
            "coding_agent_selection_invalid",
            "Resolve one or both supported coding-agent selections before probing them.",
        )
    for agent in selected:
        cli = "claude" if agent == "claude_code" else agent
        reference = f"hydration::{cli}.probe_fresh_session"
        child = AdapterRequest(
            request.request_id,
            request.operation_id,
            reference,
            request.phase,
            request.probe_purpose,
            request.attempt,
            request.name,
            request.target,
            request.flow_source_revision,
            request.answers_fingerprint,
            request.approval_fingerprint,
            request.dry_run,
            request.timeout_seconds,
            request.public_inputs,
        )
        child_result = hook_behavior(child, runtime)
        if child_result["error_kind"] == "output_truncated":
            return blocked(request, "output_truncated", str(child_result["repair"]))
        if child_result["checkpoint_status"] != "verified":
            return blocked(request, "fresh_session_hook_failed", "Repair fresh-session hooks.")
    return _boolean_probe(request, "fresh_session_hooks", True, True, str(request.target), "")


def _selected_coding_agents(request: AdapterRequest) -> tuple[str, ...] | None:
    value = request.public_inputs.get("selected_coding_agents")
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(item, str) and item in {"codex", "claude_code"}
            for item in value
        )
        or len(value) != len(set(value))
    ):
        return None
    return tuple(cast(str, item) for item in value)


def plugin_roster(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    desired = _desired_plugins(request)
    if desired is None:
        return blocked(
            request,
            "plugin_manifest_invalid",
            "Repair the generated profile/config/manifest.yaml before resuming.",
        )
    outcome = _solet_call(
        request, runtime, "service_interface::lifecycle_management_service::list_plugins", {}
    )
    if command_output_truncated(outcome):
        return truncated_solet_call_output(
            request,
            "service_interface::lifecycle_management_service::list_plugins",
            outcome,
        )
    payload = _call_payload(outcome)
    observed = _observed_plugins(payload.get("plugins"))
    if observed is None:
        return blocked(
            request,
            "plugin_roster_invalid",
            "Repair lifecycle plugin inventory until it returns a complete typed roster.",
        )
    exact = observed.actual == desired and not observed.unready
    error_kind = "plugin_roster_unready" if observed.unready else "plugin_roster_mismatch"
    return result(
        request,
        status="verified" if exact else "blocked",
        error_kind=None if exact else error_kind,
        retry_safe=True,
        evidence_items=_roster_evidence(observed, desired, exact),
        repair=None if exact else _roster_repair(observed),
    )


_ROSTER_SOURCE = "profile/config/manifest.yaml + lifecycle_management_service::list_plugins"


def _roster_evidence(observed: _RosterObservation, desired: set[str], exact: bool) -> list[JsonObject]:
    """Name every unready plugin with its reason, and keep degraded plugins' warnings visible."""
    unready = sorted(observed.unready)
    items = [
        _evidence("plugin_roster", exact, _ROSTER_SOURCE, sorted(observed.actual), sorted(desired)),
        _evidence("plugin_roster_unready", not unready, _ROSTER_SOURCE, unready, []),
    ]
    if observed.warnings:
        items.append(evidence(
            evidence_id="plugin_roster_warnings",
            kind="readiness",
            status="warning",
            summary="ready plugins report a degraded capability",
            observed=sorted(observed.warnings),
            expected=[],
            source=_ROSTER_SOURCE,
        ))
    return items


def _roster_repair(observed: _RosterObservation) -> str:
    repair = "Apply the generated manifest and repair every unready plugin."
    return f"{repair} Unready: {'; '.join(sorted(observed.unready))}" if observed.unready else repair


def _desired_plugins(request: AdapterRequest) -> set[str] | None:
    try:
        desired = load_manifest_plugin_set(request.target / "profile")
    except (OSError, ValueError):
        return None
    return desired if desired else None


_EVIDENCE_LINE_LIMIT = 240


@dataclass(frozen=True)
class _RosterObservation:
    """Loaded names, plus one ``name: status: reason`` line per unready or degraded plugin."""

    actual: set[str]
    unready: set[str]
    warnings: set[str]


@dataclass(frozen=True)
class _PluginRow:
    name: str
    ready: bool
    detail: str
    warning: str | None


def _observed_plugins(value: JsonValue) -> _RosterObservation | None:
    if not isinstance(value, list) or not value:
        return None
    observation = _RosterObservation(set(), set(), set())
    for row in value:
        normalized = _plugin_row(row)
        if normalized is None or normalized.name in observation.actual:
            return None
        observation.actual.add(normalized.name)
        if not normalized.ready:
            observation.unready.add(f"{normalized.name}: {normalized.detail}"[:_EVIDENCE_LINE_LIMIT])
        elif normalized.warning is not None:
            observation.warnings.add(f"{normalized.name}: {normalized.warning}"[:_EVIDENCE_LINE_LIMIT])
    return observation


def _plugin_row(value: JsonValue) -> _PluginRow | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    status = value.get("status")
    flags = (value.get("enabled"), value.get("lifecycle_managed"), value.get("is_running"))
    texts = (value.get("last_error"), value.get("warning"))
    if not (_valid_plugin_identity(name, status) and _boolean_flags(flags) and _optional_texts(texts)):
        return None
    enabled_flag, managed_flag, running_flag = cast(tuple[bool, bool, bool], flags)
    last_error, warning = cast(tuple[str | None, str | None], texts)
    ready = enabled_flag and status == "ready" and (not managed_flag or running_flag)
    detail = _unready_detail(cast(str, status), enabled_flag, managed_flag, running_flag, last_error)
    return _PluginRow(cast(str, name), ready, detail, warning)


def _optional_texts(texts: tuple[object, object]) -> bool:
    """``last_error`` and ``warning`` are absent or a nonempty string, never another type."""
    return all(item is None or (isinstance(item, str) and bool(item)) for item in texts)


def _unready_detail(status: str, enabled: bool, managed: bool, running: bool, last_error: str | None) -> str:
    state = status if enabled else f"{status}, disabled"
    if managed and not running:
        state = f"{state}, not running"
    return f"{state}: {last_error or 'no last_error reported'}"


def _valid_plugin_identity(name: object, status: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and status
        in {
            "ready",
            "uninitialized",
            "error",
        }
    )


def _boolean_flags(flags: tuple[object, object, object]) -> bool:
    return all(isinstance(flag, bool) for flag in flags)
