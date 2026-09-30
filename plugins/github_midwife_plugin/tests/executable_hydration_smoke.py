"""Hermetic executable-hydration and installation-doctor contract smoke.

This fixture never invokes live plugin CLIs, launchd, Settings, model services,
or a solet. Its fake runtime records closed command vectors and provides
explicit public responses for each probe.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from executable_hydration_failure_diagnostic_support import (  # noqa: E402
    run_genesis_diagnostic_regression,
)
from executable_hydration_plugin_list_support import (  # noqa: E402
    prepare_claude_receipt_fixture,
    run_genesis_profile_projection,
    run_plugin_list_output_cap,
    run_public_evidence_shape_regression,
    run_receipt_surface_shape_regression,
    run_session_retrieval_envelope,
)
from executable_hydration_session_source_support import (  # noqa: E402
    run_router_and_qualification_regressions,
    run_session_source_boot_registration_regression,
)
from executable_hydration_structured_output_cap_support import (  # noqa: E402
    run_knowledge_output_cap,
    run_knowledge_readiness_poll,
    run_plugin_roster_output_cap,
)
from github_midwife_plugin import setup_shell_operations  # noqa: E402
from github_midwife_plugin.coordination_hook_installation import build_receipt  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import (  # noqa: E402
    AdapterInputError,
    AdapterRequest,
    JsonObject,
    JsonValue,
    planned_action,
    result,
)
from github_midwife_plugin.setup_adapter_runtime import (  # noqa: E402
    _OUTPUT_LIMIT,
    _STRUCTURED_OUTPUT_LIMIT,
    CommandOutcome,
    bounded_command_outcome,
    executable_fallback_directories,
)
from github_midwife_plugin.setup_operations import operation_handlers  # noqa: E402
from github_midwife_plugin.setup_plugin_operations import _receipt_surfaces  # noqa: E402
from github_midwife_plugin.setup_shell_operations import _render_json  # noqa: E402
from readiness_deadline_scenarios import run_scenarios  # noqa: E402

_CHECKS = 0

# This smoke is the hermetic integration boundary for target-local setup.
# Its groups intentionally cover the following independently mutable contracts:
# - closed request parsing and phase purity;
# - operation handler routing and shell rendering;
# - hook-manifest interpreter rewrites and idempotent replay;
# - safe shell quoting and PATH de-duplication;
# - bounded command output and plugin JSON filtering;
# - selected-agent rather than ambient-agent behavior;
# - source profile projection and generated artifact layout;
# - session retrieval public-envelope handling;
# - adapter/doctor failure boundaries and redaction;
# - readiness deadline propagation;
# - declared coding-client executable preconditions;
# - marketplace command outcome specificity; and
# - flow-level precondition wiring debt.
#
# Keeping those checks together lets the fixture share a fake runtime while
# retaining the production command vectors and closed result shapes. The
# explicit group list also makes it clear why broad-looking helpers below are
# integration controls rather than unscoped unit-test convenience methods.
# A regression should be added to its owning group so red mutations remain
# readable by a maintainer without needing live coding-agent clients.

# Coordinator ruling 2026-09-01 ~15:1xZ: these four pre-existing orphaned
# preconditions are a separately dispatched design disposition. Exact equality
# forces this debt record to be removed when any item is repaired.
_PREEXISTING_ORPHAN_PRECONDITIONS = frozenset()


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class FakeRuntime:
    """Host boundary with no real external side effects."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.home.mkdir(parents=True)
        self.commands: list[tuple[str, ...]] = []
        self.command_environments: list[dict[str, str]] = []
        self.command_output_limits: list[int] = []
        self.writes: list[Path] = []
        self.responses: dict[tuple[str, ...], CommandOutcome] = {}
        self.response_sequences: dict[tuple[str, ...], list[CommandOutcome]] = {}
        self.http_responses: dict[str, tuple[int, JsonValue]] = {}

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> CommandOutcome:
        del timeout_seconds, cwd, input_text
        self.commands.append(argv)
        self.command_environments.append(dict(extra_env or {}))
        self.command_output_limits.append(output_limit)
        sequence = self.response_sequences.get(argv)
        if sequence:
            return self._bounded(sequence.pop(0), output_limit)
        configured = self.responses.get(argv)
        if configured is not None:
            return self._bounded(configured, output_limit)
        if argv[:1] == ("/usr/bin/which",) and len(argv) == 2:
            return self._bounded(
                CommandOutcome(0, False, 1, f"/fixture/{argv[1]}\n", ""), output_limit
            )
        return self._bounded(CommandOutcome(0, False, 1, "", ""), output_limit)

    def executable_absent(self, name: str, duration_ms: int = 1) -> None:
        """Answer ``/usr/bin/which`` for ``name`` on PATH and in every fixed fallback directory the resolver tries."""
        for target in (name, *(f"{directory}/{name}" for directory in executable_fallback_directories(self))):
            self.responses[("/usr/bin/which", target)] = CommandOutcome(1, False, duration_ms, "", "")

    @staticmethod
    def _bounded(outcome: CommandOutcome, output_limit: int) -> CommandOutcome:
        if outcome.stdout_truncated or outcome.stderr_truncated:
            return outcome
        return bounded_command_outcome(
            returncode=outcome.returncode,
            timed_out=outcome.timed_out,
            duration_ms=outcome.duration_ms,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            executable_missing=outcome.executable_missing,
            launch_error=outcome.launch_error,
            output_limit=output_limit,
        )

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: JsonObject | None = None,
    ) -> tuple[int, JsonValue]:
        del timeout_seconds, payload
        return self.http_responses.get(url, (503, None))

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        self.writes.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)


class ControlledReadinessRuntime(FakeRuntime):
    """Hold target health closed until the test explicitly releases it."""

    def __init__(self, root: Path, target: Path) -> None:
        super().__init__(root)
        self.target = target
        self.ready = threading.Event()
        self.observations: queue.Queue[str] = queue.Queue()

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> CommandOutcome:
        health_vector = (str(self.target / ".venv/bin/solet-bridge"), "health")
        embedding_vector = (
            str(self.target / ".venv/bin/solet-bridge"),
            "call",
            "service_interface::embedding_service::get_embedding_dimension",
            "{}",
        )
        if argv == health_vector:
            self.commands.append(argv)
            self.command_environments.append(dict(extra_env or {}))
            if not self.ready.is_set():
                self.observations.put("health_not_ready")
                return self._bounded(
                    CommandOutcome(
                        0,
                        False,
                        1,
                        json.dumps(
                            {
                                "status": "degraded",
                                "action_path": {"action_path_stalled": True},
                            }
                        ),
                        "",
                    ),
                    output_limit,
                )
            return self._bounded(
                CommandOutcome(
                    0,
                    False,
                    1,
                    json.dumps(
                        {"status": "healthy", "action_path": {"action_path_stalled": False}}
                    ),
                    "",
                ),
                output_limit,
            )
        if argv == embedding_vector:
            self.observations.put(
                "probe_after_ready" if self.ready.is_set() else "probe_before_ready"
            )
            if not self.ready.is_set():
                return self._bounded(
                    CommandOutcome(1, False, 1, "", "readiness latch is closed"), output_limit
                )
            return self._bounded(
                CommandOutcome(
                    0,
                    False,
                    1,
                    json.dumps(
                        {
                            "result": {
                                "success": True,
                                "error": None,
                                "data": {"result": {"dimension": 768, "model": "fixture"}},
                            }
                        }
                    ),
                    "",
                ),
                output_limit,
            )
        return super().run(
            argv,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            extra_env=extra_env,
            input_text=input_text,
            output_limit=output_limit,
        )


def _raw_request(
    target: Path,
    *,
    operation_id: str = "install_shell_integration",
    operation_ref: str = "hydration::shell.install",
    phase: str = "probe",
    purpose: str | None = "preview",
    public_inputs: JsonObject | None = None,
) -> dict[str, object]:
    apply = phase == "apply"
    return {
        "protocol_version": 1,
        "kind": "operation_request",
        "request_id": "8f2f3ed3-03fc-4f58-915e-eb400a172a67",
        "operation_id": operation_id,
        "operation_ref": operation_ref,
        "phase": phase,
        "probe_purpose": purpose,
        "attempt": 1,
        "name": "iris",
        "target": str(target),
        "flow_id": "macos.repository_setup",
        "flow_source_revision": "a" * 40,
        "answers_fingerprint": "sha256:" + "b" * 64,
        "approval_fingerprint": "sha256:" + "c" * 64 if apply else None,
        "dry_run": not apply,
        "timeout_seconds": 30,
        "public_inputs": {} if public_inputs is None else public_inputs,
    }


def _readiness_inputs(*, purpose: str, parent_seconds: int) -> JsonObject:
    return {
        "startup_readiness_contract_version": 1,
        "startup_readiness_contract_digest": "sha256:" + "d" * 64,
        "startup_readiness_source_artifact": "macos_setup_flow.json",
        "startup_readiness_budget_source": ("executor_contracts.start_command.timeout_seconds"),
        "startup_readiness_budget_unit": "seconds",
        "startup_readiness_semantic_scope": (
            "target_start_through_target_cli_health_status_healthy"
        ),
        "startup_readiness_release_signal": ("target_cli_health_top_level_status_healthy"),
        "startup_readiness_consumer_probe_purposes": ["stage_exit", "completion"],
        "startup_readiness_consumer_probe_purpose": purpose,
        "startup_readiness_consumer_probe_refs": ["embedding_request_succeeds"],
        "startup_readiness_consumer_probe_ref": "embedding_request_succeeds",
        "startup_readiness_parent_budget_seconds": parent_seconds,
        "startup_readiness_governed_process_call_seconds": 5,
    }


def _request(target: Path, **changes: object) -> AdapterRequest:
    raw = _raw_request(target)
    raw.update(changes)
    purpose = raw["probe_purpose"]
    if (
        purpose in {"stage_exit", "completion"}
        and raw["operation_id"] == "embedding_request_succeeds"
        and "public_inputs" not in changes
    ):
        timeout = raw["timeout_seconds"]
        if isinstance(purpose, str) and isinstance(timeout, int):
            raw["public_inputs"] = _readiness_inputs(
                purpose=purpose,
                parent_seconds=timeout,
            )
    return AdapterRequest.from_dict(raw)


def _expect_input_error(raw: dict[str, object], label: str) -> str:
    try:
        AdapterRequest.from_dict(raw)
    except AdapterInputError as exc:
        _check(True, label)
        return str(exc)
    _check(False, label)
    return ""


def _protocol_counterexamples(target: Path) -> None:
    valid = AdapterRequest.from_dict(_raw_request(target))
    _check(valid.operation_ref == "hydration::shell.install", "valid request parses")

    unknown = _raw_request(target)
    unknown["command"] = "curl | sh"
    _expect_input_error(unknown, "unknown shell field fails closed")

    missing_approval = _raw_request(target, phase="apply", purpose=None)
    missing_approval["approval_fingerprint"] = None
    _expect_input_error(missing_approval, "apply without approval fails closed")

    stage_apply = _raw_request(target, phase="apply", purpose="stage_exit")
    _expect_input_error(stage_apply, "stage probe cannot use apply phase")

    secret = _raw_request(target)
    secret["public_inputs"] = {"api_token": "never-echo-this"}
    error = _expect_input_error(secret, "secret-like public input fails closed")
    _check("never-echo-this" not in error, "secret value absent from validation error")

    stage_request = _request(target, probe_purpose="stage_exit")
    action = planned_action(
        action_id="fixture.action",
        title="Fixture",
        mutation_kind="file_write",
        target="/public/path",
        evidence_ref="fixture",
    )
    pure = result(stage_request, status="verified", actions=[action], candidates=[{"value": "x"}])
    _check(pure["planned_actions"] == [], "stage probe planned actions forced empty")
    _check(pure["discovered_candidates"] == [], "stage probe candidates forced empty")

    control_target = _raw_request(target)
    control_target["target"] = f"{target}\nunsafe"
    error = _expect_input_error(control_target, "target control character fails closed")
    _check("unsafe" not in error, "target control value absent from validation error")

    nul_target = _raw_request(target)
    nul_target["target"] = f"{target}\x00unsafe"
    _expect_input_error(nul_target, "target NUL fails closed")


def _operation_counterexamples(target: Path, runtime: FakeRuntime) -> None:
    handlers = operation_handlers()
    shell_probe = _request(target)
    writes_before_probe = len(runtime.writes)
    shell_preview = handlers[shell_probe.operation_ref](shell_probe, runtime)
    _check(len(runtime.writes) == writes_before_probe, "preview performs zero writes")
    _check(
        bool(cast(list[JsonValue], shell_preview["planned_actions"])),
        "preview enumerates conditional mutations",
    )

    missing = _request(
        target,
        operation_id="configure_lm_studio_embeddings",
        operation_ref="setup::models.configure_lm_studio_embeddings",
    )
    blocked = handlers[missing.operation_ref](missing, runtime)
    _check(blocked["checkpoint_status"] == "blocked", "missing model carrier blocks")
    _check(blocked["error_kind"] == "operation_input_missing", "missing model stable kind")

    configured = AdapterRequest.from_dict(
        _raw_request(
            target,
            operation_id="configure_lm_studio_embeddings",
            operation_ref="setup::models.configure_lm_studio_embeddings",
            public_inputs={
                "lm_studio_base_url": "http://localhost:1234/v1",
                "model": "embedding-model",
            },
        )
    )
    preview = handlers[configured.operation_ref](configured, runtime)
    _check(preview["checkpoint_status"] == "pending", "model preview does not mutate")
    _check(len(cast(list[JsonValue], preview["planned_actions"])) == 1, "model mutation enumerated")

    configured_apply = AdapterRequest.from_dict(
        _raw_request(
            target,
            operation_id="configure_lm_studio_embeddings",
            operation_ref="setup::models.configure_lm_studio_embeddings",
            phase="apply",
            purpose=None,
            public_inputs={
                "lm_studio_base_url": "http://localhost:1234/v1",
                "model": "embedding-model",
            },
        )
    )
    applied = handlers[configured_apply.operation_ref](configured_apply, runtime)
    _check(applied["checkpoint_status"] == "applied", "model apply never claims verified")
    config = json.loads(
        (target / "profile/config/plugins/openai_embeddings_plugin.json").read_text(
            encoding="utf-8"
        )
    )
    _check(
        config == {"base_url": "http://localhost:1234/v1", "model": "embedding-model"},
        "exact model config",
    )

    start_probe = _request(target, operation_id="lifecycle.start", operation_ref="lifecycle::start")
    _check(
        handlers[start_probe.operation_ref](start_probe, runtime)["checkpoint_status"] == "blocked",
        "lifecycle start probe is refused",
    )
    start_apply = AdapterRequest.from_dict(
        _raw_request(
            target,
            operation_id="lifecycle.start",
            operation_ref="lifecycle::start",
            phase="apply",
            purpose=None,
        )
    )
    _check(
        handlers[start_apply.operation_ref](start_apply, runtime)["checkpoint_status"] == "applied",
        "lifecycle start apply is applied only",
    )

    settings = _request(
        target,
        operation_id="open_background_items_settings",
        operation_ref="macos::settings.background_items",
    )
    commands_before_settings = len(runtime.commands)
    settings_preview = handlers[settings.operation_ref](settings, runtime)
    _check(
        settings_preview["checkpoint_status"] == "awaiting_user",
        "opening a TCC pane is never verification",
    )
    _check(
        len(runtime.commands) == commands_before_settings,
        "TCC preview does not open Settings",
    )

    runtime.executable_absent("tmux")
    timed_out_apply = AdapterRequest.from_dict(
        _raw_request(
            target,
            operation_id="install_tmux",
            operation_ref="setup::tmux.install",
            phase="apply",
            purpose=None,
        )
    )
    runtime.responses[("/fixture/brew", "install", "--dry-run", "tmux")] = CommandOutcome(
        0, False, 1, "Would install 1 formula:\ntmux\n", ""
    )
    runtime.responses[("/fixture/brew", "install", "tmux")] = CommandOutcome(
        None,
        True,
        30_000,
        "public-but-suppressed",
        "secret=secret-value-must-not-escape",
    )
    timed_out = handlers[timed_out_apply.operation_ref](timed_out_apply, runtime)
    runtime.responses[("/usr/bin/which", "tmux")] = CommandOutcome(1, False, 1, "", "")
    tmux_preview = handlers[
        _request(
            target,
            operation_id="install_tmux",
            operation_ref="setup::tmux.install",
        ).operation_ref
    ](
        _request(
            target,
            operation_id="install_tmux",
            operation_ref="setup::tmux.install",
        ),
        runtime,
    )
    serialized = json.dumps(timed_out, sort_keys=True)
    _check(timed_out["error_kind"] == "adapter_timeout", "timeout fails closed")
    _check(
        "secret-value-must-not-escape" not in serialized,
        "command streams cannot leak secrets",
    )
    tmux_action = cast(list[JsonObject], tmux_preview["planned_actions"])[0]
    _check(
        tmux_action["target"] == "/fixture/brew:tmux",
        "tmux preview binds the resolved Homebrew executable into approved action data",
    )


def _shell_and_hook_counterexamples(target: Path, runtime: FakeRuntime) -> None:
    shell_apply = AdapterRequest.from_dict(_raw_request(target, phase="apply", purpose=None))
    shell_result = operation_handlers()[shell_apply.operation_ref](shell_apply, runtime)
    _check(shell_result["checkpoint_status"] == "applied", "shell apply reports applied")
    rendered_shell = (target / "client/iris.zsh").read_text(encoding="utf-8")
    _check("client/bin" in rendered_shell, "fresh shell launcher path rendered")
    _check(".local/bin" in rendered_shell, "named launcher path rendered")
    for launcher_name in ("claude-iris", "codex-iris"):
        launcher = (target / "client" / "bin" / launcher_name).read_text(encoding="utf-8")
        _check(
            "GIT_CONTROLLER_NAME=" not in launcher, f"{launcher_name} solo render omits the gate"
        )
    _check(
        "BEGIN SOLET iris" in (runtime.home / ".zshrc").read_text(encoding="utf-8"),
        "managed zsh block",
    )
    _fresh_shell_stage_exit_replays(target, runtime, shell_apply)
    _path_prepend_guard_counterexample(target)
    _fresh_zsh_path_is_unique(target, runtime)
    _shell_target_quoting_counterexample(target, runtime)
    armed_raw = _raw_request(
        target, phase="apply", purpose=None, public_inputs={"git_controller_name": "Git-Controller"}
    )
    armed = AdapterRequest.from_dict(armed_raw)
    operation_handlers()[armed.operation_ref](armed, runtime)
    for launcher_name in ("claude-iris", "codex-iris"):
        launcher = (target / "client" / "bin" / launcher_name).read_text(encoding="utf-8")
        _check(
            'GIT_CONTROLLER_NAME="Git-Controller"' in launcher,
            f"{launcher_name} arms named controller",
        )
    _check(
        (target / "client" / "iris-fleet.zsh").is_file(),
        "designated controller renders fleet functions",
    )


def _shell_target_quoting_counterexample(target: Path, runtime: FakeRuntime) -> None:
    """A legal path with shell metacharacters must render as parseable literal data."""

    special_target = target / "clone $(print compromised) ' quote \\\\ slash"
    special = _raw_request(special_target, phase="apply", purpose=None)
    special_request = AdapterRequest.from_dict(special)
    applied = operation_handlers()[special_request.operation_ref](special_request, runtime)
    _check(
        applied["checkpoint_status"] == "applied", "special-character target shell apply succeeds"
    )
    launchers = (
        special_target / "client" / "bin" / "claude-iris",
        special_target / "client" / "bin" / "codex-iris",
        special_target / "client" / "bin" / "launch-iris",
        special_target / "client" / "iris.zsh",
        runtime.home / ".zshrc",
    )
    for launcher in launchers:
        checked = subprocess.run(
            ["zsh", "-n", str(launcher)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        _check(checked.returncode == 0, f"quoted target leaves {launcher.name} parseable")
        _check(
            "$(print compromised)" in launcher.read_text(encoding="utf-8"),
            f"target remains literal in {launcher.name}",
        )

    rendered = _render_json(
        _PLUGIN_ROOT / "knowledge_base/hydration_templates/marketplace_json.template",
        {"{{MARKETPLACE_NAME}}": 'name with " quote and \\ slash'},
    )
    decoded = json.loads(rendered)
    _check(
        decoded["name"] == 'name with " quote and \\ slash',
        "JSON renderer escapes structural values",
    )


def _fresh_shell_stage_exit_replays(
    target: Path,
    runtime: FakeRuntime,
    shell_apply: AdapterRequest,
) -> None:
    """Exercise the flow's exact fresh-login-shell stage-exit probe across replay."""

    launcher = runtime.home / ".local" / "bin" / shell_apply.name
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.symlink_to(target / ".venv" / "bin" / "solet-bridge")
    _check(launcher.is_symlink(), "genesis-owned bare launcher is a symlink")
    _check(
        launcher.resolve(strict=True) == target / ".venv" / "bin" / "solet-bridge",
        "genesis-owned bare launcher resolves to the target bridge",
    )
    command = 'command -v "$1" && command -v "claude-$1" && command -v "codex-$1"'
    vector = ["zsh", "-lic", command, "solet-path-probe", "iris"]
    environment = {"HOME": str(runtime.home), "PATH": "/usr/bin:/bin"}
    first = subprocess.run(
        vector,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )
    _check(first.returncode == 0, "fresh login shell satisfies the declared path probe")
    writes_before_replay = len(runtime.writes)
    replay = operation_handlers()[shell_apply.operation_ref](shell_apply, runtime)
    _check(replay["checkpoint_status"] == "applied", "shell replay reports applied")
    _check(len(runtime.writes) == writes_before_replay, "shell replay does not rewrite artifacts")
    second = subprocess.run(
        vector,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )
    _check(second.returncode == 0, "fresh login shell probe remains green after replay")


def _fresh_zsh_path_is_unique(target: Path, runtime: FakeRuntime) -> None:
    """Source distinct rendered hydration files twice in a clean zsh process."""
    sources: list[Path] = []
    expected_client_bins: list[str] = []
    for name in ("iris", "lumen", "quartz"):
        solet_target = target / name
        raw = _raw_request(solet_target, phase="apply", purpose=None)
        raw["name"] = name
        request = AdapterRequest.from_dict(raw)
        applied = operation_handlers()[request.operation_ref](request, runtime)
        _check(applied["checkpoint_status"] == "applied", f"{name} shell file renders")
        sources.append(solet_target / "client" / f"{name}.zsh")
        expected_client_bins.append(str(solet_target / "client" / "bin"))

    source_commands = "\n".join(
        f"source {shlex.quote(str(source))}" for source in (*sources, *sources)
    )
    fresh_shell = subprocess.run(
        ["zsh", "-f", "-c", f"{source_commands}\nprint -r -- $PATH"],
        capture_output=True,
        check=False,
        env={"HOME": str(runtime.home), "PATH": "/usr/bin:/bin"},
        text=True,
        timeout=10,
    )
    _check(fresh_shell.returncode == 0, "fresh zsh sources rendered hydration files")
    components = fresh_shell.stdout.strip().split(":")
    for client_bin in expected_client_bins:
        _check(
            components.count(client_bin) == 1,
            f"fresh zsh has one normalized client/bin component: {client_bin}",
        )
    _check(
        components.count(str(runtime.home / ".local" / "bin")) == 1,
        "fresh zsh has one shared .local/bin component",
    )
    source_manifest = _PLUGIN_ROOT / "claude_plugin/coordination-hooks/hooks/hooks.json"
    target_manifest = (
        target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json"
    )
    target_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_manifest, target_manifest)

    plugin_request = _request(
        target,
        operation_id="install_claude_plugin",
        operation_ref="hydration::claude.install_plugin",
        public_inputs={"solet_name": "iris", "clone_directory": str(target)},
    )
    selector = "coordination-hooks@iris"
    runtime.responses[("claude", "plugin", "list")] = CommandOutcome(
        0, False, 1, selector, "system-python=3.9"
    )
    pre_patch = operation_handlers()[plugin_request.operation_ref](plugin_request, runtime)
    _check(pre_patch["checkpoint_status"] == "pending", "bare/system Python hook is non-green")

    prepare_claude_receipt_fixture(
        target, runtime, plugin_root=_PLUGIN_ROOT, selector=selector, command_outcome=CommandOutcome,
    )
    run_receipt_surface_shape_regression(
        target,
        runtime,
        request=plugin_request,
        selector=selector,
        receipt_surfaces=_receipt_surfaces,
        build_receipt=build_receipt,
        check=_check,
    )

    plugin_apply = AdapterRequest.from_dict(
        _raw_request(
            target,
            operation_id="install_claude_plugin",
            operation_ref="hydration::claude.install_plugin",
            phase="apply",
            purpose=None,
            public_inputs={"solet_name": "iris", "clone_directory": str(target)},
        )
    )
    applied = operation_handlers()[plugin_apply.operation_ref](plugin_apply, runtime)
    _check(
        applied["checkpoint_status"] == "blocked",
        "post-apply refuses to bless stale cache bytes when CLI readback did not repair them",
    )
    manifest_text = target_manifest.read_text(encoding="utf-8")
    _check(
        str(target / ".venv/bin/python3") in manifest_text, "hook bound to absolute target Python"
    )
    _check('"command": "python3"' not in manifest_text, "bare Python hook removed")

    target_manifest.write_text(source_manifest.read_text(encoding="utf-8"), encoding="utf-8")
    wrong_cache = operation_handlers()[plugin_request.operation_ref](plugin_request, runtime)
    _check(
        wrong_cache["checkpoint_status"] == "pending",
        "visible plugin with wrong manifest stays non-green",
    )


def _path_prepend_guard_counterexample(target: Path) -> None:
    """Removing the membership guard must make the PATH-uniqueness check red."""

    template_name = "solet.zsh.template"
    guard = '    [[ "$existing_path_entry" == "$path_entry" ]] && return 0\n'
    template = setup_shell_operations._TEMPLATES / template_name
    rendered = template.read_text(encoding="utf-8")
    _check(guard in rendered, "path membership guard is present before mutation")

    with tempfile.TemporaryDirectory(prefix="path-prepend-guard-mutation-") as temporary:
        mutated_templates = Path(temporary) / "hydration_templates"
        shutil.copytree(setup_shell_operations._TEMPLATES, mutated_templates)
        mutated_template = mutated_templates / template_name
        mutated_template.write_text(rendered.replace(guard, "", 1), encoding="utf-8")
        runtime = FakeRuntime(target / "path-prepend-guard-mutation-runtime")
        with patch.object(setup_shell_operations, "_TEMPLATES", mutated_templates):
            try:
                _fresh_zsh_path_is_unique(target / "path-prepend-guard-mutation-target", runtime)
            except AssertionError as error:
                _check(
                    "fresh zsh has one normalized client/bin component" in str(error),
                    "removing the path membership guard makes PATH uniqueness red",
                )
            else:
                raise AssertionError(
                    "removing the path membership guard must make PATH uniqueness red"
                )


def _coding_agent_cli_preconditions(target: Path) -> None:
    """Missing user-managed clients block before planning or manifest mutation."""

    from github_midwife_plugin.installation_plugin_doctor import selected_plugins
    from github_midwife_plugin.setup_operations import _failed_outcome

    for cli, operation_id, operation_ref in (
        ("codex", "install_codex_plugin", "hydration::codex.install_plugin"),
        ("claude", "install_claude_plugin", "hydration::claude.install_plugin"),
    ):
        runtime = FakeRuntime(target / f"missing-{cli}")
        runtime.executable_absent(cli, 2)
        preview = _request(target, operation_id=operation_id, operation_ref=operation_ref)
        preview_result = operation_handlers()[operation_ref](preview, runtime)
        _check(preview_result["checkpoint_status"] == "blocked", f"missing {cli} preview blocks")
        _check(
            preview_result["error_kind"] == f"{cli}_cli_missing",
            f"missing {cli} keeps discriminator",
        )
        _check(preview_result["retry_safe"] is True, f"missing {cli} is a satisfiable precondition")
        _check(
            preview_result["planned_actions"] == [], f"missing {cli} plans no impossible actions"
        )
        _check(
            preview_result["reason"]
            == {
                "outcome_class": "executable_missing",
                "exit_code": None,
                "duration_ms": 0,
                "timed_out": False,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
            f"missing {cli} exposes only its sanitized reason",
        )

        apply = AdapterRequest.from_dict(
            _raw_request(
                target,
                operation_id=operation_id,
                operation_ref=operation_ref,
                phase="apply",
                purpose=None,
            )
        )
        apply_result = operation_handlers()[operation_ref](apply, runtime)
        _check(
            apply_result["planned_actions"] == [], f"missing {cli} apply keeps phase-pure actions"
        )
        _check(not runtime.writes, f"missing {cli} does not patch a hook manifest")
        _check(
            all(
                "marketplace" not in command and "plugin" not in command[1:]
                for command in runtime.commands
            ),
            f"missing {cli} never launches marketplace or plugin commands",
        )

    aggregate_runtime = FakeRuntime(target / "missing-aggregate")
    aggregate_runtime.executable_absent("codex", 2)
    aggregate = _request(
        target,
        operation_id="coding_agent_plugins",
        operation_ref="setup::coding_agents.verify_plugins",
        public_inputs={"selected_coding_agents": ["codex"]},
    )
    aggregate_result = selected_plugins(aggregate, aggregate_runtime)
    _check(
        aggregate_result["error_kind"] == "codex_cli_missing",
        "aggregate preserves child CLI discriminator",
    )

    request = _request(target)
    private = "private-token-should-not-escape"
    for outcome, expected in (
        (CommandOutcome(None, False, 7, "", "", executable_missing=True), "executable_missing"),
        (CommandOutcome(None, False, 8, "", "", launch_error=private), "launch_error"),
        (CommandOutcome(None, True, 9, "", ""), "timeout"),
        (
            bounded_command_outcome(
                returncode=17,
                timed_out=False,
                duration_ms=10,
                stdout="ok",
                stderr=f"token={private}",
            ),
            "nonzero_exit",
        ),
    ):
        failed = _failed_outcome(request, outcome, "fixture_command_failed")
        reason = cast(JsonObject, failed["reason"])
        _check(reason["outcome_class"] == expected, f"{expected} reason remains distinct")
        _check(reason["duration_ms"] == outcome.duration_ms, f"{expected} reason keeps duration")
        _check(
            private not in json.dumps(failed, sort_keys=True),
            f"{expected} reason redacts private stream values",
        )
        _check(failed["stdout"] == failed["stderr"] == "", f"{expected} preserves closed streams")

    run_genesis_diagnostic_regression(
        target=target,
        runtime=runtime,
        raw_request=_raw_request,
        handlers=operation_handlers(),
        check=_check,
    )

    marketplace_runtime = FakeRuntime(target / "marketplace-nonzero")
    manifest = (
        target / "plugins/github_midwife_plugin/codex_plugin/coordination-hooks/hooks/hooks.json"
    )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_PLUGIN_ROOT / "codex_plugin/coordination-hooks/hooks/hooks.json", manifest)
    marketplace_runtime.responses[
        ("/fixture/codex", "plugin", "marketplace", "add", str(target))
    ] = bounded_command_outcome(
        returncode=17, timed_out=False, duration_ms=13, stdout="visible", stderr=f"token={private}"
    )
    failed_marketplace = operation_handlers()["hydration::codex.install_plugin"](
        AdapterRequest.from_dict(
            _raw_request(
                target,
                operation_id="install_codex_plugin",
                operation_ref="hydration::codex.install_plugin",
                phase="apply",
                purpose=None,
            )
        ),
        marketplace_runtime,
    )
    _check(
        failed_marketplace["error_kind"] == "codex_marketplace_failed",
        "nonzero marketplace failure stays specific",
    )
    _check(
        cast(JsonObject, failed_marketplace["reason"])["outcome_class"] == "nonzero_exit",
        "nonzero marketplace reason remains specific",
    )
    _check(
        private not in json.dumps(failed_marketplace, sort_keys=True),
        "marketplace result excludes private stderr",
    )


def _precondition_reference_contract() -> None:
    """Preconditions either gate work or appear in the exact tracked-debt set."""

    flow = json.loads(
        (_PLUGIN_ROOT / "knowledge_base/macos_setup_flow.json").read_text(encoding="utf-8")
    )
    referenced = {
        probe_id
        for operation in cast(dict[str, JsonObject], flow["operations"]).values()
        for probe_id in cast(
            list[str], cast(JsonObject, operation["idempotency"])["precondition_probe_refs"]
        )
    }
    referenced.update(
        probe_id
        for stage in cast(dict[str, JsonObject], flow["stages"]).values()
        for probe_id in cast(list[str], stage.get("entry_probe_refs", []))
    )
    referenced.update(
        probe_id
        for stage in cast(dict[str, JsonObject], flow["stages"]).values()
        for probe_id in cast(list[str], stage.get("exit_probe_refs", []))
    )
    preconditions = {
        probe_id
        for probe_id, definition in cast(dict[str, JsonObject], flow["probes"]).items()
        if definition.get("level") == "precondition"
    }
    _check(
        preconditions - referenced == _PREEXISTING_ORPHAN_PRECONDITIONS,
        "precondition orphans exactly match separately-dispatched tracked debt",
    )


def _controlled_readiness_latch_outcome(
    target: Path,
    request: AdapterRequest,
    dispatch: Callable[[AdapterRequest, ControlledReadinessRuntime], JsonObject],
    attempt: int,
) -> str:
    runtime = ControlledReadinessRuntime(
        target.parent / f"readiness-latch-{attempt}",
        target,
    )
    results: list[JsonObject] = []
    errors: list[BaseException] = []

    def run_probe() -> None:
        try:
            results.append(dispatch(request, runtime))
        except BaseException as exc:  # noqa: BLE001 - surfaced as a deterministic test result
            errors.append(exc)

    worker = threading.Thread(target=run_probe, daemon=True)
    worker.start()
    first_observation = _queued_observation(runtime.observations, timeout=2)
    if first_observation == "health_not_ready":
        runtime.ready.set()
    worker.join(timeout=2)
    return _classify_latch_outcome(
        first_observation,
        runtime.observations,
        results,
        errors,
        worker_alive=worker.is_alive(),
    )


def _queued_observation(observations: queue.Queue[str], *, timeout: float) -> str:
    try:
        return observations.get(timeout=timeout)
    except queue.Empty:
        return "no_readiness_observation"


def _classify_latch_outcome(
    first_observation: str,
    observations: queue.Queue[str],
    results: list[JsonObject],
    errors: list[BaseException],
    *,
    worker_alive: bool,
) -> str:
    if worker_alive:
        return "worker_timeout"
    if errors:
        return f"worker_exception:{type(errors[0]).__name__}"
    if first_observation == "probe_before_ready":
        return first_observation
    final_observation = _queued_observation(observations, timeout=0)
    if final_observation == "no_readiness_observation":
        return "probe_not_observed"
    if (
        first_observation == "health_not_ready"
        and final_observation == "probe_after_ready"
        and results
        and results[0]["checkpoint_status"] == "verified"
    ):
        return "verified"
    return f"unexpected:{first_observation}:{final_observation}"


def _adapter_and_doctor_red_boundary(target: Path, runtime: FakeRuntime) -> None:
    try:
        from github_midwife_plugin.installation_doctor import probe_handlers
        from github_midwife_plugin.setup_adapter import dispatch_request, run_once
    except ImportError as exc:
        raise AssertionError(
            "RED: closed adapter dispatcher and installation-doctor registry are not implemented"
        ) from exc

    unknown = _request(target, operation_ref="setup::unknown.operation")
    unknown_result = dispatch_request(unknown, runtime)
    _check(unknown_result["checkpoint_status"] == "blocked", "unknown operation blocks")
    _check(unknown_result["error_kind"] == "adapter_missing", "unknown operation stable kind")

    injected = _request(target, public_inputs={"shell": "echo unreviewed"})
    injected_result = dispatch_request(injected, runtime)
    _check(
        injected_result["error_kind"] == "adapter_protocol_error",
        "undeclared operation input fails closed",
    )

    probes = probe_handlers()
    required_refs = {
        "hydration::shell.probe_path",
        "hydration::shell.probe_python",
        "hydration::claude.probe_plugin",
        "hydration::claude.probe_fresh_session",
        "service_interface::local_self_deployment_service.swap_status",
        "plugin::agent_messaging_plugin.peer_identity",
        "service_interface::knowledge_service.search",
        "setup::journal.install_state_projection_matches",
    }
    _check(required_refs <= set(probes), "required doctor registry coverage")

    run_plugin_roster_output_cap(
        target,
        runtime,
        request=_request,
        check=_check,
        command_outcome=CommandOutcome,
        default_output_limit=_OUTPUT_LIMIT,
        structured_output_limit=_STRUCTURED_OUTPUT_LIMIT,
    )

    embedding_probe = _request(
        target,
        operation_id="embedding_request_succeeds",
        operation_ref="service_interface::embedding_service.get_embedding_dimension",
        probe_purpose="stage_exit",
        timeout_seconds=120,
    )
    embedding_vector = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "service_interface::embedding_service::get_embedding_dimension",
        "{}",
    )
    latch_outcomes = [
        _controlled_readiness_latch_outcome(
            target,
            embedding_probe,
            dispatch_request,
            attempt,
        )
        for attempt in range(10)
    ]
    _check(
        latch_outcomes == ["verified"] * 10,
        f"controlled readiness latch repeated outcomes: {latch_outcomes}",
    )

    from github_midwife_plugin.installation_doctor import _wait_for_target_readiness

    health_vector = (str(target / ".venv/bin/solet-bridge"), "health")
    healthy = CommandOutcome(
        0,
        False,
        1,
        json.dumps({"status": "healthy", "action_path": {"action_path_stalled": False}}),
        "",
    )

    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    runtime.response_sequences[health_vector] = [
        CommandOutcome(1, False, 1, "", "bridge starting"),
        CommandOutcome(
            0,
            False,
            1,
            json.dumps({"status": "degraded", "action_path": {"action_path_stalled": True}}),
            "",
        ),
        healthy,
    ]
    clock = FakeClock()
    readiness = _wait_for_target_readiness(
        embedding_probe,
        runtime,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    _check(
        not isinstance(readiness, dict) and readiness.last_status == "healthy",
        "readiness wait observes an explicit starting-to-healthy transition",
    )
    _check(
        len(clock.sleeps) == 2,
        "unreachable and degraded states are both observed before readiness succeeds",
    )

    timeout_runtime = FakeRuntime(runtime.home.parent / "readiness-timeout")
    timeout_runtime.responses[health_vector] = CommandOutcome(
        1,
        False,
        1,
        "",
        "bridge unavailable",
    )
    timeout_clock = FakeClock()
    timeout_probe = _request(
        target,
        operation_id="embedding_request_succeeds",
        operation_ref="service_interface::embedding_service.get_embedding_dimension",
        probe_purpose="stage_exit",
        timeout_seconds=6,
    )
    timed_out = _wait_for_target_readiness(
        timeout_probe,
        timeout_runtime,
        monotonic=timeout_clock.monotonic,
        sleep=timeout_clock.sleep,
    )
    _check(
        timed_out is not None
        and timed_out["checkpoint_status"] == "blocked"
        and timed_out["error_kind"] == "target_readiness_timeout",
        "readiness timeout fails closed with a stable error kind",
    )
    timeout_repair = str(timed_out["repair"])
    _check("Waited" in timeout_repair and "health" in timeout_repair, "timeout is actionable")

    runtime.responses[health_vector] = healthy
    runtime.responses[embedding_vector] = CommandOutcome(
        1,
        False,
        1,
        "",
        "process unavailable",
    )
    embedding_failed = dispatch_request(embedding_probe, runtime)
    _check(
        runtime.commands.index(health_vector) < runtime.commands.index(embedding_vector),
        "bridge readiness is observed before the embedding probe runs",
    )
    embedding_repair = str(embedding_failed["repair"])
    _check(
        embedding_failed["checkpoint_status"] != "verified",
        "missing bound embedding process is non-green",
    )
    _check(
        "service_interface::embedding_service::get_embedding_dimension" in embedding_repair,
        "embedding failure names the expected process",
    )
    _check(
        "no successful process result" in embedding_repair,
        "embedding failure states what was found",
    )
    _check(
        "Verify the embedding_service binding" in embedding_repair,
        "embedding failure gives a concrete repair action",
    )
    runtime.responses[embedding_vector] = CommandOutcome(
        0,
        False,
        1,
        json.dumps(
            {
                "result": {
                    "success": True,
                    "error": None,
                    "data": {"result": {"dimension": 768, "model": "fixture"}},
                }
            }
        ),
        "",
    )
    _check(
        dispatch_request(embedding_probe, runtime)["checkpoint_status"] == "verified",
        "registered bound embedding process verifies",
    )

    missing_path = _request(
        target,
        operation_id="fresh_shell_path_valid",
        operation_ref="hydration::shell.probe_path",
        probe_purpose="completion",
    )
    path_vector = (
        "/bin/zsh",
        "-lic",
        'command -v "$1" && command -v "claude-$1" && command -v "codex-$1"',
        "solet-path-probe",
        "iris",
    )
    runtime.responses[path_vector] = CommandOutcome(1, False, 1, "", "not found")
    _check(
        dispatch_request(missing_path, runtime)["checkpoint_status"] != "verified",
        "fresh shell without named launchers is non-green",
    )

    hook = _request(
        target,
        operation_id="claude_fresh_session_hook_active",
        operation_ref="hydration::claude.probe_fresh_session",
        probe_purpose="completion",
    )
    _check(
        dispatch_request(hook, runtime)["checkpoint_status"] != "verified",
        "existing hook without behavioral output is non-green",
    )

    _launchagent_crash_loop_regression(target, runtime, dispatch_request)
    run_router_and_qualification_regressions(
        target,
        runtime,
        request=_request,
        check=_check,
        command_outcome=CommandOutcome,
        dispatch_request=dispatch_request,
        run_once=run_once,
        raw_request=_raw_request,
        run_knowledge_readiness_poll=run_knowledge_readiness_poll,
        run_knowledge_output_cap=run_knowledge_output_cap,
        structured_output_limit=_STRUCTURED_OUTPUT_LIMIT,
    )


def _launchagent_crash_loop_regression(
    target: Path,
    runtime: FakeRuntime,
    dispatch_request: Callable[[AdapterRequest, FakeRuntime], JsonObject],
) -> None:
    autostart = _request(
        target,
        operation_id="launchagent_running",
        operation_ref="genesis::autostart.verify",
        probe_purpose="completion",
        public_inputs={"autostart": "enabled"},
    )
    command = ("/bin/launchctl", "print", f"gui/{os.getuid()}/local.solet.iris")
    runtime.responses[command] = CommandOutcome(
        0, False, 1, "\tstate = running\n\truns = 1\n\tlast exit code = 0\n", ""
    )
    _check(
        dispatch_request(autostart, runtime)["checkpoint_status"] == "verified",
        "running LaunchAgent with one clean start verifies",
    )
    runtime.responses[command] = CommandOutcome(
        0,
        False,
        1,
        "\tstate = running\n\truns = 1\n\tpid = 8814\n\tlast exit code = (never exited)\n",
        "",
    )
    never_exited = dispatch_request(autostart, runtime)
    _check(
        never_exited["checkpoint_status"] == "verified"
        and never_exited["evidence"][0]["observed"]
        == "state=running,last_exit=(never exited),runs=1",
        "receipt-128 running LaunchAgent that never exited verifies",
    )
    runtime.responses[command] = CommandOutcome(
        0, False, 1, "\tstate = running\n\truns = 4\n\tlast exit code = 1\n", ""
    )
    crash_loop = dispatch_request(autostart, runtime)
    _check(
        crash_loop["checkpoint_status"] == "blocked"
        and crash_loop["error_kind"] == "launchagent_crash_loop",
        "loaded crash-looping LaunchAgent blocks with a distinct error kind",
    )
    runtime.responses[command] = CommandOutcome(
        0, False, 1, "\tstate = running\n\truns = 1\n\tlast exit code = unavailable\n", ""
    )
    unreadable = dispatch_request(autostart, runtime)
    _check(
        unreadable["checkpoint_status"] == "blocked"
        and unreadable["error_kind"] == "launchagent_status_unreadable",
        "unrecognized launchctl last-exit form remains unreadable",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="executable-hydration-") as temporary:
        root = Path(temporary)
        target = root / "target"
        target.mkdir()
        target = target.resolve(strict=True)
        solet = target / ".venv/bin/solet-bridge"
        solet.parent.mkdir(parents=True)
        solet.write_text(
            f"#!{target}/.venv/bin/python3\n",
            encoding="utf-8",
        )
        solet.chmod(0o755)
        runtime = FakeRuntime(root)
        _protocol_counterexamples(target)
        _operation_counterexamples(target, runtime)
        run_session_source_boot_registration_regression(
            target,
            fake_runtime=FakeRuntime,
            adapter_request=AdapterRequest,
            raw_request=_raw_request,
            operation_handlers=operation_handlers,
            command_outcome=CommandOutcome,
            check=_check,
        )
        _shell_and_hook_counterexamples(target, runtime)
        run_plugin_list_output_cap(
            target,
            runtime,
            plugin_root=_PLUGIN_ROOT,
            request=_request,
            check=_check,
            command_outcome=CommandOutcome,
            bounded_outcome=bounded_command_outcome,
            operation_handlers=operation_handlers,
        )
        _coding_agent_cli_preconditions(target)
        _precondition_reference_contract()
        run_genesis_profile_projection(target, runtime, request=_request, check=_check)
        session_response = run_session_retrieval_envelope(
            target,
            runtime,
            request=_request,
            check=_check,
            command_outcome=CommandOutcome,
        )
        run_public_evidence_shape_regression(
            target,
            session_response,
            request=_request,
            check=_check,
            command_outcome=CommandOutcome,
        )
        _adapter_and_doctor_red_boundary(target, runtime)
        readiness_report = run_scenarios(emit=False)
        _check(
            set(readiness_report) == {"deterministic", "controls"},
            "derived readiness deadline scenarios are registered",
        )
    print(f"executable_hydration_smoke: {_CHECKS}/{_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
