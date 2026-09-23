"""Executable, idempotent setup operations behind the frozen adapter protocol."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import cast

import yaml

from .launchagent_status import launchagent_health
from .setup_adapter_contract import (
    AdapterRequest,
    JsonObject,
    evidence,
    planned_action,
    public_string,
    result,
)
from .setup_adapter_runtime import (
    CommandOutcome,
    HomebrewAcquisition,
    Runtime,
    homebrew_install_plan_error,
    read_json_object,
    resolve_executable,
)
from .steps import GENESIS_STEP_RUNNERS

type OperationHandler = Callable[[AdapterRequest, Runtime], JsonObject]

_TEMPLATES = Path(__file__).resolve().parents[2] / "knowledge_base" / "hydration_templates"
_CLAUDE_HOOKS = Path("plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json")
_CODEX_HOOKS = Path("plugins/github_midwife_plugin/codex_plugin/coordination-hooks/hooks/hooks.json")
_MODEL_CONFIGS = {
    "setup::models.configure_lm_studio_embeddings": Path("profile/config/plugins/openai_embeddings_plugin.json"),
    "setup::models.configure_lm_studio_inference": Path("profile/config/plugins/default_inference_plugin.json"),
}
_SESSION_ROOTS = {
    "hydration::sessions.register_codex_filesystem": (("codex_local", Path(".codex/sessions")),),
    "hydration::sessions.register_claude_filesystem": (
        ("claude_code_local", Path(".claude/projects")),
        ("claude_code_history", Path(".claude/history.jsonl")),
        ("claude_code_tasks", Path(".claude/tasks")),
    ),
}
_SETTINGS_URLS = {
    "macos::settings.background_items": "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
    "macos::settings.files_and_folders": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
}
_TMUX_RETURN_KEY_BLOCK = (
    "# BEGIN SOLET TERMINAL RETURN KEYS\n"
    "set -s extended-keys on\n"
    'set -as terminal-features "xterm*:extkeys"\n'
    "# END SOLET TERMINAL RETURN KEYS\n"
)
_ITERM_RETURN_KEY_PROFILE = {
    "Profiles": [
        {
            "Name": "Solet Claude Code Return keys",
            "Guid": "A2C5A9EE-4C24-4BCB-8266-CDB03CBA5E43",
            "Option Key Sends": 2,
        }
    ]
}
_ITERM_RETURN_KEY_PROFILE_RELATIVE = Path(
    "Library/Application Support/iTerm2/DynamicProfiles/solet-claude-return-keys.json"
)
_GENESIS_COMPLETED_STEPS = tuple(step_name for step_name, _runner in GENESIS_STEP_RUNNERS)
_SOLET_RESULT_ENVELOPE_KEYS = frozenset(
    {
        "success",
        "action_status",
        "actions",
        "error",
        "timestamp",
        "provider_type",
        "data",
    }
)
_FAILURE_STDERR_DIAGNOSTIC_LIMIT = 1024
_CREDENTIAL_KEY_NAME = (
    r"[a-z0-9_-]*(?:password|passwd|secret|token|key|bearer|credential)[a-z0-9_-]*"
)
_CREDENTIAL_VALUE = re.compile(
    r"""(?ix)
    (?P<label>\b""" + _CREDENTIAL_KEY_NAME + r"""\b)
    (?P<separator>\s*(?:=|:)\s*)
    (?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)
    """
)
_AUTHORIZATION_BEARER_VALUE = re.compile(
    r"(?i)(?P<label>\bauthorization\b)(?P<separator>\s*:\s*bearer\s+)(?P<value>\S+)"
)


def operation_handlers() -> dict[str, OperationHandler]:
    """Return the closed registry of target-local mutation handlers."""

    from .lm_studio_provisioning import operation_handlers as lm_studio_handlers
    from .setup_plugin_operations import plugin_install
    from .setup_session_operations import session_source
    from .setup_shell_operations import shell

    return {
        **lm_studio_handlers(),
        "setup::tmux.install": _tmux,
        "setup::terminal.configure_return_keys": _terminal_return_keys,
        "setup::coding_agents.install_codex": _coding_agent_cli,
        "setup::coding_agents.install_claude": _coding_agent_cli,
        "setup::coding_agents.install_node": _coding_agent_cli,
        "genesis::solet.run": _genesis,
        "hydration::shell.install": shell,
        "genesis::autostart.install": _genesis,
        "macos::settings.background_items": _settings,
        "macos::settings.files_and_folders": _settings,
        "setup::models.configure_lm_studio_embeddings": _model_config,
        "setup::models.configure_lm_studio_inference": _model_config,
        "hydration::codex.install_plugin": plugin_install,
        "hydration::claude.install_plugin": plugin_install,
        "hydration::sessions.register_codex_filesystem": session_source,
        "hydration::sessions.register_claude_filesystem": session_source,
        "lifecycle::start": _start,
        "plugin::g_suite_plugin.configure": _manual_connector,
        "plugin::jira_plugin.configure": _manual_connector,
        "plugin::marketo_plugin.configure": _manual_connector,
        "plugin::salesforce_plugin.configure": _manual_connector,
        "plugin::schwab_market_data_plugin.configure": _manual_connector,
        "plugin::snowflake_plugin.configure": _manual_connector,
        "plugin::zuora_plugin.configure": _manual_connector,
        "plugin::external_postgres_plugin.configure_connection": _manual_connector,
    }


def _tmux(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Provision tmux through the shared probe-first Homebrew contract."""

    return _homebrew_provisioner(
        request,
        runtime,
        executable_name="tmux",
        acquisition=HomebrewAcquisition("formula", "tmux"),
    )


def _terminal_return_keys(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Offer persistent tmux and iTerm2 configuration for Claude Code newlines."""

    tmux_path = runtime.home / ".tmux.conf"
    profile_path = runtime.home / _ITERM_RETURN_KEY_PROFILE_RELATIVE
    tmux_current = _read_user_text(tmux_path)
    tmux_desired = _merge_terminal_return_key_block(tmux_current)
    profile_desired = json.dumps(_ITERM_RETURN_KEY_PROFILE, indent=2) + "\n"
    tmux_changed = tmux_desired != tmux_current
    profile_changed = _read_user_text(profile_path) != profile_desired
    if request.phase == "probe":
        if not tmux_changed and not profile_changed:
            return _verified(
                request,
                "terminal_return_keys",
                "managed tmux and iTerm2 Return-key configuration is current",
                str(tmux_path),
            )
        actions: list[JsonObject] = []
        if tmux_changed:
            actions.append(
                planned_action(
                    action_id="terminal.merge_tmux_return_key_block",
                    title="Merge the managed tmux modified-Return block",
                    mutation_kind="file_write",
                    target=str(tmux_path),
                    evidence_ref="tmux_return_key_config_drift",
                )
            )
        if profile_changed:
            actions.append(
                planned_action(
                    action_id="terminal.write_iterm_option_return_profile",
                    title="Write the iTerm2 Option+Return dynamic profile",
                    mutation_kind="file_write",
                    target=str(profile_path),
                    evidence_ref="iterm_option_return_profile_drift",
                )
            )
        return result(
            request,
            status="pending",
            actions=actions,
            repair=("Approve the managed tmux block and iTerm2 profile. Restart iTerm2, then select the Solet Claude Code Return keys profile before opening a new seat."),
        )
    if tmux_changed:
        runtime.atomic_write(tmux_path, tmux_desired, mode=0o644)
    if profile_changed:
        runtime.atomic_write(profile_path, profile_desired, mode=0o644)
    return result(
        request,
        status="applied",
        repair=("Restart iTerm2, then select the Solet Claude Code Return keys profile. Use backslash followed by Return as a fallback newline chord."),
    )


def _read_user_text(path: Path) -> str:
    """Read user-owned config without blindly overwriting an unreadable file."""

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise RuntimeError(f"cannot read managed terminal configuration {path}: {exc}") from exc


def _merge_terminal_return_key_block(existing: str) -> str:
    begin = "# BEGIN SOLET TERMINAL RETURN KEYS"
    end = "# END SOLET TERMINAL RETURN KEYS"
    start = existing.find(begin)
    finish = existing.find(end)
    if start >= 0 and finish >= start:
        return existing[:start] + _TMUX_RETURN_KEY_BLOCK + existing[finish + len(end) :].lstrip("\n")
    separator = "" if not existing or existing.endswith("\n") else "\n"
    return existing + separator + _TMUX_RETURN_KEY_BLOCK


def _homebrew_provisioner(
    request: AdapterRequest,
    runtime: Runtime,
    *,
    executable_name: str,
    acquisition: HomebrewAcquisition,
) -> JsonObject:
    """Execute the one consented probe, exact-plan, and install contract."""

    executable = resolve_executable(runtime, executable_name)
    brew = resolve_executable(runtime, "brew")
    executable_evidence = _homebrew_executable_evidence(executable_name, brew)
    if request.phase == "probe":
        return _homebrew_provisioning_probe(
            request,
            executable_name=executable_name,
            acquisition=acquisition,
            executable=executable,
            brew=brew,
            evidence_items=executable_evidence,
        )
    if executable is not None:
        return result(request, status="verified", evidence_items=executable_evidence)
    if brew is None:
        return _blocked(
            request,
            "homebrew_missing",
            f"Resolve Homebrew before applying the approved {acquisition.name} installation.",
        )
    return _apply_homebrew_provisioner(
        request,
        runtime,
        brew=brew,
        executable_name=executable_name,
        acquisition=acquisition,
        evidence_items=executable_evidence,
    )


def _homebrew_executable_evidence(executable_name: str, brew: str | None) -> list[JsonObject]:
    """Describe the resolved Homebrew executable without performing a mutation."""

    return [
        evidence(
            evidence_id=f"{executable_name}.homebrew_executable",
            kind="executable_resolution",
            status="verified" if brew is not None else "blocked",
            summary="Homebrew was resolved before its approved package action can run.",
            observed=brew,
            expected="absolute executable path",
            source=brew or "unresolved",
        )
    ]


def _homebrew_provisioning_probe(
    request: AdapterRequest,
    *,
    executable_name: str,
    acquisition: HomebrewAcquisition,
    executable: str | None,
    brew: str | None,
    evidence_items: list[JsonObject],
) -> JsonObject:
    """Return a no-op verification or the exact package action for review."""

    if executable is not None:
        return result(
            request,
            status="verified",
            evidence_items=[
                _boolean_evidence(f"{executable_name}_available", True, executable),
                *evidence_items,
            ],
        )
    return result(
        request,
        status="pending",
        actions=[
            planned_action(
                action_id=f"{executable_name}.install_homebrew_package",
                title=f"Install {acquisition.name} with Homebrew",
                mutation_kind="package_install",
                target=f"{brew or 'unresolved'}:{acquisition.name}",
                evidence_ref=f"{executable_name}_missing",
            )
        ],
        evidence_items=[
            _boolean_evidence(f"{executable_name}_available", False, "unresolved"),
            *evidence_items,
        ],
        repair=f"Approve the exact Homebrew {acquisition.name} installation action.",
    )


def _apply_homebrew_provisioner(
    request: AdapterRequest,
    runtime: Runtime,
    *,
    brew: str,
    executable_name: str,
    acquisition: HomebrewAcquisition,
    evidence_items: list[JsonObject],
) -> JsonObject:
    """Dry-run, validate, and run exactly one reviewed Homebrew installation."""

    install_args, dry_run_args = _homebrew_install_args(acquisition)
    plan = runtime.run((brew, *dry_run_args), timeout_seconds=request.timeout_seconds)
    plan_error = _acquisition_plan_error(plan, acquisition)
    if plan_error is not None:
        return result(
            request,
            status="failed",
            error_kind="homebrew_install_plan_rejected",
            retry_safe=True,
            evidence_items=evidence_items,
            repair=plan_error,
        )
    outcome = runtime.run((brew, *install_args), timeout_seconds=request.timeout_seconds)
    return _apply_outcome(request, outcome, f"{executable_name}_install_failed")


def _homebrew_install_args(acquisition: HomebrewAcquisition) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Build the kind-aware install and dry-run vectors for one acquisition."""

    if acquisition.kind == "cask":
        return (
            ("install", "--cask", acquisition.name),
            ("install", "--dry-run", "--cask", acquisition.name),
        )
    return ("install", acquisition.name), ("install", "--dry-run", acquisition.name)


def _coding_agent_cli(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Probe, plan-guard, and install one consented coding-agent dependency."""

    acquisitions = {
        "setup::coding_agents.install_codex": ("codex", HomebrewAcquisition("cask", "codex")),
        "setup::coding_agents.install_claude": (
            "claude",
            HomebrewAcquisition("cask", "claude-code"),
        ),
        "setup::coding_agents.install_node": ("node", HomebrewAcquisition("formula", "node")),
    }
    executable_name, acquisition = acquisitions[request.operation_ref]
    return _homebrew_provisioner(
        request,
        runtime,
        executable_name=executable_name,
        acquisition=acquisition,
    )


def _acquisition_plan_error(outcome: CommandOutcome, acquisition: HomebrewAcquisition) -> str | None:
    """Apply the reviewed acquisition closure to a Homebrew dry-run result."""

    return homebrew_install_plan_error(outcome, acquisition)


def _genesis(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    marker = request.target / ".solet" / "genesis.json"
    plist = runtime.home / "Library" / "LaunchAgents" / f"local.solet.{request.name}.plist"
    autostart = _autostart_enabled(request)
    if autostart is None:
        return _blocked(
            request,
            "operation_input_missing",
            "Resolve the selected autostart topology before running genesis.",
        )
    artifacts_valid = genesis_artifacts_valid(request, runtime)
    satisfied = artifacts_valid and (
        not autostart
        or (_launchagent_running(request, runtime) and plist.is_file())
    )
    if request.phase == "probe":
        if satisfied:
            return _verified(request, "genesis_artifacts", "genesis artifacts are present", str(marker))
        actions = _genesis_actions(request, runtime, marker, plist)
        return result(request, status="pending", actions=actions, repair="Approve genesis materialization.")
    python = request.target / ".venv" / "bin" / "python3"
    if not python.is_file():
        return _blocked(request, "dependency_closure_missing", "Complete the target venv first.")
    setup_profile = public_string(request, "setup_profile")
    if setup_profile is None:
        return _blocked(
            request,
            "operation_input_missing",
            "Resolve the selected setup profile before running genesis.",
        )
    outcome = runtime.run(
        (str(python), "-m", "github_midwife_plugin.genesis"),
        timeout_seconds=request.timeout_seconds,
        cwd=request.target,
        extra_env={
            "SOLET_NAME": request.name,
            "SOLET_ASSUME_YES": "1",
            "SOLET_PROFILE": setup_profile,
            "SOLET_AUTOSTART": "enabled" if autostart else "disabled",
            "SOLET_OPERATION_REF": request.operation_ref,
        },
    )
    return _apply_outcome(request, outcome, "genesis_failed")


def _launchagent_running(request: AdapterRequest, runtime: Runtime) -> bool:
    """Require launchd health before a LaunchAgent repair preview verifies."""

    outcome = runtime.run(
        ("/bin/launchctl", "print", f"gui/{os.getuid()}/local.solet.{request.name}"),
        timeout_seconds=10,
    )
    healthy, _observed, _error_kind = launchagent_health(outcome)
    return healthy


def _genesis_actions(
    request: AdapterRequest,
    runtime: Runtime,
    marker: Path,
    plist: Path,
) -> list[JsonObject]:
    if request.operation_ref == "genesis::autostart.install":
        return [_genesis_action("install_launchagent", "LaunchAgent", "launchagent_install", plist)]
    targets: tuple[tuple[str, str, str, Path | str], ...] = (
        (
            "install_allowlisted_plugins",
            "allowlisted target plugins",
            "plugin_install",
            request.target / ".venv",
        ),
        (
            "materialize_configuration",
            "target configuration",
            "config_write",
            request.target / "profile/config",
        ),
        (
            "seed_keychain_credential",
            "target database credential",
            "keychain_write",
            f"keychain:{request.name}-postgres",
        ),
        (
            "seed_vault_passphrase",
            "target vault passphrase",
            "secret_file_create",
            request.target / "profile/data/vault",
        ),
        (
            "materialize_knowledge_links",
            "knowledge-base links",
            "symlink_write",
            request.target / "knowledge_bases",
        ),
        (
            "install_router",
            "identity-aware blue-green router",
            "service_install",
            f"launchagent:local.solet.{request.name}.router",
        ),
        (
            "install_named_launcher",
            "named no-MCP launcher",
            "symlink_write",
            runtime.home / ".local/bin" / request.name,
        ),
        (
            "configure_codex_mcp_server",
            "Codex MCP server configuration",
            "config_append",
            runtime.home / ".codex" / "config.toml",
        ),
        ("finalize_genesis_marker", "genesis transaction marker", "state_write", marker),
    )
    if _autostart_enabled(request) is True:
        targets = (
            *targets[:5],
            ("install_launchagent", "LaunchAgent", "launchagent_install", plist),
            *targets[5:],
        )
    return [_genesis_action(action_id, title, mutation_kind, target) for action_id, title, mutation_kind, target in targets]


def genesis_artifacts_valid(request: AdapterRequest, runtime: Runtime) -> bool:
    """Validate final Genesis state rather than accepting a marker's existence.

    ``.solet/genesis.json`` is emitted only after the successful CLI path
    returns. Its schema therefore records the completed spine, while the
    target artifacts below provide the independent proof that the marker still
    describes the installed target rather than a stale or truncated file.
    """

    marker = read_json_object(request.target / ".solet" / "genesis.json")
    if not _final_genesis_marker_valid(marker, request.name):
        return False
    bindings = read_json_object(request.target / "profile/config/service_bindings.json")
    if bindings is None:
        return False
    manifest_path = request.target / "profile/config/manifest.yaml"
    if not _yaml_object(manifest_path):
        return False
    required_files = (
        request.target / ".venv/bin/python3",
        request.target / "profile/config/plugins/macos_vault_plugin/passphrase",
    )
    if not all(path.is_file() for path in required_files):
        return False
    launcher = runtime.home / ".local/bin" / request.name
    expected_launcher = request.target / ".venv/bin/solet-bridge"
    try:
        return launcher.resolve(strict=True) == expected_launcher.resolve(strict=True)
    except OSError:
        return False


def _final_genesis_marker_valid(marker: JsonObject | None, name: str) -> bool:
    return marker is not None and _marker_identity_valid(marker, name) and _marker_steps_valid(marker)


def _marker_identity_valid(marker: JsonObject, name: str) -> bool:
    profile = marker.get("profile")
    completed_at = marker.get("completed_at")
    return all(
        (
            marker.get("schema_version") == 1,
            marker.get("solet_name") == name,
            isinstance(profile, str) and bool(profile.strip()),
            isinstance(completed_at, str) and bool(completed_at.strip()),
        )
    )


def _marker_steps_valid(marker: JsonObject) -> bool:
    steps = marker.get("steps")
    if not isinstance(steps, list) or len(steps) != len(_GENESIS_COMPLETED_STEPS):
        return False
    return all(isinstance(step, dict) and step.get("step_name") == expected_name and step.get("status") == "completed" for expected_name, step in zip(_GENESIS_COMPLETED_STEPS, steps, strict=True))


def _yaml_object(path: Path) -> bool:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(value, dict)


def _autostart_enabled(request: AdapterRequest) -> bool | None:
    if request.operation_ref == "genesis::autostart.install":
        return True
    value = public_string(request, "autostart")
    if value == "enabled":
        return True
    if value == "disabled":
        return False
    return None


def _genesis_action(
    action_id: str,
    title: str,
    mutation_kind: str,
    target: Path | str,
) -> JsonObject:
    return planned_action(
        action_id=f"genesis.{action_id}",
        title=f"Ensure {title}",
        mutation_kind=mutation_kind,
        target=str(target),
        evidence_ref="genesis_artifacts_missing",
    )


def _settings(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    url = _SETTINGS_URLS[request.operation_ref]
    if request.phase == "probe":
        return result(
            request,
            status="awaiting_user",
            actions=[
                planned_action(
                    action_id="settings.open_reviewed_pane",
                    title="Open the reviewed macOS Settings pane",
                    mutation_kind="settings_open",
                    target=url,
                    evidence_ref="permission_not_positive",
                )
            ],
            error_kind="permission_not_positive",
            repair="Approve opening Settings, then grant access and resume; opening is not verification.",
        )
    outcome = runtime.run(("/usr/bin/open", url), timeout_seconds=10)
    if not outcome.ok:
        return _failed_outcome(request, outcome, "settings_open_failed")
    return result(
        request,
        status="awaiting_user",
        error_kind="permission_not_positive",
        retry_safe=True,
        repair="Grant access in the opened pane, then resume for a positive probe.",
    )


def _model_config(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    base_url = public_string(request, "lm_studio_base_url")
    model = public_string(request, "model")
    if base_url is None or model is None:
        return _blocked(
            request,
            "operation_input_missing",
            "The reviewed lm_studio_base_url and qualified model are both required.",
        )
    path = request.target / _MODEL_CONFIGS[request.operation_ref]
    current = read_json_object(path) or {}
    satisfied = current.get("base_url") == base_url and current.get("model") == model
    if request.phase == "probe":
        if satisfied:
            return _verified(request, "model_config", "qualified model is configured", str(path))
        return result(
            request,
            status="pending",
            actions=[
                planned_action(
                    action_id="models.write_qualified_selection",
                    title="Write the qualified model selection",
                    mutation_kind="config_write",
                    target=str(path),
                    evidence_ref="model_config_drift",
                )
            ],
            repair="Approve the exact non-secret model configuration shown.",
        )
    updated = dict(current)
    updated.update({"base_url": base_url, "model": model})
    runtime.atomic_write(path, json.dumps(updated, indent=2, sort_keys=True) + "\n", mode=0o600)
    return result(request, status="applied", retry_safe=True)


def _start(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    if request.phase == "probe":
        return _blocked(request, "protocol_error", "lifecycle::start is apply-only.")
    outcome = _kickstart(request, runtime)
    return _apply_outcome(request, outcome, "lifecycle_start_failed")


def _kickstart(request: AdapterRequest, runtime: Runtime) -> CommandOutcome:
    return runtime.run(
        ("/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/local.solet.{request.name}"),
        timeout_seconds=min(request.timeout_seconds, 60),
    )


def _manual_connector(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    del runtime
    actions = []
    if request.phase == "probe":
        actions = [
            planned_action(
                action_id="connector.capture_agent_blind_credential",
                title="Capture the connector credential through its agent-blind broker",
                mutation_kind="keychain_write",
                target=f"keychain:{request.operation_ref.rsplit('.', maxsplit=1)[0]}",
                evidence_ref="operator_secret_required",
            )
        ]
    return result(
        request,
        status="awaiting_user",
        error_kind="operator_secret_required",
        actions=actions,
        repair="Complete the connector's reviewed agent-blind credential flow, then resume.",
    )


def solet_call(
    request: AdapterRequest,
    runtime: Runtime,
    process_key: str,
    arguments: JsonObject,
) -> CommandOutcome:
    cli = request.target / ".venv/bin/solet-bridge"
    return runtime.run(
        (str(cli), "call", process_key, json.dumps(arguments, separators=(",", ":"))),
        timeout_seconds=min(request.timeout_seconds, 60),
        cwd=request.target,
        extra_env={"SOLET_NAME": request.name},
    )


def solet_call_succeeded(outcome: CommandOutcome) -> bool:
    payload = _solet_result(outcome)
    return payload is not None and payload.get("success") is True and payload.get("error") is None


def solet_data_string(outcome: CommandOutcome, key: str) -> str | None:
    payload = _solet_success_payload(outcome)
    value = payload.get(key) if payload is not None else None
    return value if isinstance(value, str) and value else None


def _solet_success_payload(outcome: CommandOutcome) -> JsonObject | None:
    """Normalize one unambiguous successful bridge result payload.

    Service-interface verbs may serialize their result fields directly beside
    the bridge envelope, while plugin verbs conventionally nest those fields
    under ``data``.  Treat a response carrying both forms as invalid: choosing
    either value could silently accept contradictory target state.
    """

    result_payload = _solet_result(outcome)
    if (
        result_payload is None
        or result_payload.get("success") is not True
        or result_payload.get("error") is not None
    ):
        return None
    business_fields = {
        key: value
        for key, value in result_payload.items()
        if key not in _SOLET_RESULT_ENVELOPE_KEYS
    }
    if "data" not in result_payload:
        return business_fields if business_fields else None
    data = result_payload["data"]
    if business_fields or not isinstance(data, dict):
        return None
    return cast(JsonObject, data)


def _solet_result(outcome: CommandOutcome) -> JsonObject | None:
    if not outcome.ok:
        return None
    try:
        raw: object = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        return None
    outer = raw.get("result") if isinstance(raw, dict) else None
    return cast(JsonObject, outer) if isinstance(outer, dict) else None


def _verified(request: AdapterRequest, evidence_id: str, summary: str, source: str) -> JsonObject:
    return result(
        request,
        status="verified",
        evidence_items=[
            evidence(
                evidence_id=evidence_id,
                kind="behavior",
                status="verified",
                summary=summary,
                observed=True,
                expected=True,
                source=source,
            )
        ],
    )


def _boolean_evidence(evidence_id: str, observed: bool, source: str) -> JsonObject:
    return evidence(
        evidence_id=evidence_id,
        kind="behavior",
        status="verified" if observed else "blocked",
        summary=f"{evidence_id}={'true' if observed else 'false'}",
        observed=observed,
        expected=True,
        source=source,
    )


def _apply_outcome(request: AdapterRequest, outcome: CommandOutcome, error_kind: str) -> JsonObject:
    if outcome.ok:
        return result(request, status="applied", duration_ms=outcome.duration_ms, retry_safe=True)
    return _failed_outcome(request, outcome, error_kind)


def _failed_outcome(
    request: AdapterRequest,
    outcome: CommandOutcome,
    error_kind: str,
) -> JsonObject:
    return result(
        request,
        status="failed",
        error_kind="adapter_timeout" if outcome.timed_out else error_kind,
        retry_safe=False,
        exit_code=outcome.returncode,
        timed_out=outcome.timed_out,
        duration_ms=outcome.duration_ms,
        reason=command_failure_reason(outcome),
        repair="Inspect the target-local public diagnostics and repair before resuming.",
    )


def command_failure_reason(outcome: CommandOutcome) -> JsonObject:
    """Return bounded, redacted failure metadata for a failed command.

    The adapter's top-level command streams intentionally remain closed because
    arbitrary child-process output can include credentials. A nonzero command
    can nevertheless be the only diagnosis when a target dies before writing a
    public marker, so expose a small stderr-only diagnostic here after removing
    conventional ``name=value`` and ``name: value`` secret forms.
    """

    outcome_class = "executable_missing" if outcome.executable_missing else "timeout" if outcome.timed_out else "launch_error" if outcome.launch_error is not None else "nonzero_exit"
    reason: JsonObject = {
        "outcome_class": outcome_class,
        "exit_code": outcome.returncode,
        "duration_ms": max(0, outcome.duration_ms),
        "timed_out": outcome.timed_out,
        "stdout_bytes": outcome.stdout_bytes,
        "stderr_bytes": outcome.stderr_bytes,
        "stdout_truncated": outcome.stdout_truncated,
        "stderr_truncated": outcome.stderr_truncated,
    }
    if outcome.stderr:
        diagnostic, diagnostic_truncated = _public_stderr_diagnostic(outcome)
        reason["stderr_diagnostic"] = diagnostic
        reason["stderr_diagnostic_truncated"] = diagnostic_truncated
    return reason


def _public_stderr_diagnostic(outcome: CommandOutcome) -> tuple[str, bool]:
    """Bound and redact one failed command's stderr for the public reason."""

    encoded = outcome.stderr.encode("utf-8")
    captured = encoded[:_FAILURE_STDERR_DIAGNOSTIC_LIMIT]
    diagnostic = captured.decode("utf-8", errors="ignore")
    return (
        _CREDENTIAL_VALUE.sub(
            r"\g<label>\g<separator><redacted>",
            _AUTHORIZATION_BEARER_VALUE.sub(
                r"\g<label>\g<separator><redacted>", diagnostic
            ),
        ),
        outcome.stderr_truncated or len(encoded) > len(captured),
    )


def _blocked(request: AdapterRequest, error_kind: str, repair: str) -> JsonObject:
    return result(
        request,
        status="blocked",
        error_kind=error_kind,
        retry_safe=True,
        repair=repair,
    )


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


__all__ = ["OperationHandler", "operation_handlers"]
