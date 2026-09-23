"""Seven reviewed, resumable LM Studio provisioning operations and their probes."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from .lm_studio_login_agent import (
    install_login_agent,
    login_classification,
    login_definition_current,
    login_paths,
)
from .lm_studio_models import (
    BASE_URL,
    ModelArtifact,
    artifact_present,
    cli_available,
    cli_path,
    model_loaded,
    partial_bytes,
    reviewed_models,
    selected_roles,
    served_models,
)
from .lm_studio_settings import disable_jit, jit_disabled
from .setup_adapter_contract import AdapterRequest, JsonObject, evidence, planned_action, result
from .setup_adapter_runtime import CommandOutcome, Runtime

PUBLIC_INPUT_KEYS = frozenset({"embeddings_implementation", "inference_implementation", "lm_studio_base_url"})
_OPERATIONS = {
    "install": "cli_available",
    "start_server": "server_ready",
    "pull_embedding": "embedding_artifact_present",
    "load_embedding": "embedding_model_served",
    "pull_inference": "inference_artifact_present",
    "load_inference": "inference_model_served",
    "install_login_agent": "login_agent_valid",
}
INSTALLER_URL = "https://lmstudio.ai/install.sh"
INSTALLER_VERSION = "0.0.23-1"
_VENDOR_DOWNLOAD_TIMEOUT = "Download failed: Timed-out. Please try to resume."
type Handler = Callable[[AdapterRequest, Runtime], JsonObject]


def operation_handlers() -> dict[str, Handler]:
    return {f"setup::lm_studio.{name}": provision for name in _OPERATIONS}


def probe_handlers() -> dict[str, Handler]:
    return {f"setup::lm_studio.{name}": probe for name in (*_OPERATIONS.values(), "jit_disabled")}


def _inputs_valid(request: AdapterRequest) -> bool:
    return request.public_inputs.get("lm_studio_base_url") in {BASE_URL, "http://localhost:1234/v1"} and bool(selected_roles(request.public_inputs))


def _repair(request: AdapterRequest) -> str:
    return f"For incomplete setup, run solet create {request.name} --dry-run --json and review the new fingerprint before resuming. After restoring a completed host, run solet doctor {request.name} --json."


def _role(suffix: str) -> str:
    return "embeddings" if "embedding" in suffix else "inference"


def _observation(suffix: str, runtime: Runtime, models: dict[str, ModelArtifact]) -> bool | None:
    if suffix == "cli_available":
        return cli_available(runtime.home)
    if suffix == "server_ready":
        return served_models(runtime) is not None
    if suffix == "jit_disabled":
        return jit_disabled(runtime.home)
    if suffix == "login_agent_valid":
        return login_definition_current(runtime.home, models)
    model = models[_role(suffix)]
    if suffix.endswith("artifact_present"):
        return artifact_present(model, runtime.home)
    return model_loaded(runtime, model.api_identifier)


def _observed_result(request: AdapterRequest, suffix: str, observed: bool | None) -> JsonObject:
    status = "verified" if observed is True else "unknown" if observed is None else "pending"
    return result(
        request,
        status="blocked" if observed is None else status,
        error_kind=f"lm_studio_{suffix}_unknown" if observed is None else None,
        evidence_items=[evidence(evidence_id=f"lm_studio_{suffix}", kind="host", status=status, summary=f"LM Studio {suffix}: {status}", observed=observed, expected=True, source="target_runtime_lm_studio")],
        repair=None if observed is True else _repair(request),
    )


def probe(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Every probe is passive, including calls before or after an apply."""

    if not _inputs_valid(request):
        return result(request, status="blocked", error_kind="lm_studio_inputs_invalid", repair=_repair(request))
    suffix = request.operation_ref.rsplit(".", 1)[1]
    return _observed_result(request, suffix, _observation(suffix, runtime, reviewed_models(request.target)))


def provision(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Re-probe at apply time; preserve complete and partial host artifacts."""

    if not _inputs_valid(request):
        return result(request, status="blocked", error_kind="lm_studio_inputs_invalid", repair=_repair(request))
    suffix = request.operation_ref.rsplit(".", 1)[1]
    models = reviewed_models(request.target)
    observed = _operation_satisfied(suffix, runtime, models)
    if observed is None and request.phase == "apply":
        return _observed_result(request, _OPERATIONS[suffix], None)
    if observed:
        return result(request, status="verified" if request.phase == "probe" else "applied")
    if request.phase == "probe":
        return result(request, status="pending", actions=[_action(request, runtime, suffix, models)], repair=_repair(request))
    if suffix == "install":
        return _install(request, runtime)
    if not cli_available(runtime.home):
        return result(request, status="blocked", error_kind="lm_studio_cli_unavailable", repair=_repair(request))
    return _apply(request, runtime, suffix, models)


def _operation_satisfied(suffix: str, runtime: Runtime, models: dict[str, ModelArtifact]) -> bool | None:
    if suffix == "install_login_agent":
        classification = login_classification(runtime, models)
        return None if classification == "unknown" else classification == "present_already_current"
    observed = _observation(_OPERATIONS[suffix], runtime, models)
    if suffix == "start_server":
        return observed is True and jit_disabled(runtime.home)
    return observed


def _action(request: AdapterRequest, runtime: Runtime, suffix: str, models: dict[str, ModelArtifact]) -> JsonObject:
    targets = {"install": f"{INSTALLER_URL} (llmster {INSTALLER_VERSION}) -> {cli_path(runtime.home)}", "start_server": BASE_URL, "install_login_agent": str(login_paths(runtime.home)[0])}
    target = targets.get(suffix)
    if target is None:
        model = models[_role(suffix)]
        target = str(model.path(runtime.home)) if suffix.startswith("pull_") else f"{BASE_URL}: {model.api_identifier}; GPU off" + ("; context 8192" if model.role == "inference" else "")
    return planned_action(action_id=f"lm_studio.{suffix}", title=f"LM Studio: {suffix.replace('_', ' ')}", mutation_kind="host_provisioning", target=target, evidence_ref=f"lm_studio_{_OPERATIONS[suffix]}")


def _install(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    download_argv = ("/usr/bin/curl", "-fsSL", "--max-time", "30", INSTALLER_URL)
    downloaded = runtime.run(download_argv, timeout_seconds=35, output_limit=1024 * 1024)
    if not downloaded.ok or downloaded.stdout_truncated:
        return _failure(request, downloaded, "lm_studio_installer_download_failed", download_argv)
    script, count = re.subn(r'^APP_VERSION="[^"\r\n]+"$', f'APP_VERSION="{INSTALLER_VERSION}"', downloaded.stdout, flags=re.MULTILINE)
    if count != 1:
        return result(request, status="blocked", error_kind="lm_studio_installer_pin_shape_unknown", repair=_repair(request))
    install_argv = ("/bin/sh", "-s", "--", "--quiet", "--no-modify-path")
    installed = runtime.run(install_argv, timeout_seconds=max(1, request.timeout_seconds - 45), input_text=script)
    if not installed.ok:
        return _failure(request, installed, "lm_studio_install_failed", install_argv)
    if installed.stdout_bytes == 0 and installed.stderr_bytes == 0:
        return _installer_never_ran(request, installed, install_argv)
    if not cli_available(runtime.home):
        return result(
            request,
            status="blocked",
            error_kind="lm_studio_cli_unavailable",
            exit_code=installed.returncode,
            timed_out=installed.timed_out,
            duration_ms=installed.duration_ms,
            evidence_items=[_command_outcome_evidence(installed, install_argv)],
            reason=_command_failure_reason(installed),
            repair=_repair(request),
        )
    return result(request, status="applied")


def _apply(request: AdapterRequest, runtime: Runtime, suffix: str, models: dict[str, ModelArtifact]) -> JsonObject:
    if suffix == "start_server":
        return _start_server(request, runtime)
    if suffix == "install_login_agent":
        if install_login_agent(runtime, models):
            return result(request, status="applied")
        return result(request, status="blocked", error_kind="lm_studio_login_agent_unavailable", repair=_repair(request))
    model = models[_role(suffix)]
    argv = model.get_argv if suffix.startswith("pull_") else model.load_argv
    # Leave time for partial-byte evidence and serialization before the outer
    # adapter process deadline. A full 900-second inner call loses its receipt.
    command = (str(cli_path(runtime.home)), *argv)
    outcome = runtime.run(command, timeout_seconds=max(1, request.timeout_seconds - 10))
    if suffix.startswith("pull_"):
        return _pull_outcome(request, runtime, suffix, model, command, outcome)
    if not outcome.ok:
        return _failure(request, outcome, f"lm_studio_{suffix}_failed", command)
    return result(request, status="applied", duration_ms=outcome.duration_ms)


def _pull_outcome(
    request: AdapterRequest,
    runtime: Runtime,
    suffix: str,
    model: ModelArtifact,
    command: tuple[str, ...],
    outcome: CommandOutcome,
) -> JsonObject:
    if outcome.timed_out:
        return _download_pending(request, runtime, model, command, outcome)
    if outcome.ok:
        return result(request, status="applied", duration_ms=outcome.duration_ms)
    category = _vendor_failure_category(outcome)
    if category is not None and artifact_present(model, runtime.home):
        return result(
            request,
            status="applied",
            exit_code=outcome.returncode,
            duration_ms=outcome.duration_ms,
            evidence_items=[_command_outcome_evidence(outcome, command, category)],
            reason=_command_failure_reason(outcome),
        )
    error_kind = "lm_studio_vendor_download_timeout" if category is not None else f"lm_studio_{suffix}_failed"
    return _failure(request, outcome, error_kind, command, category)


def _download_pending(
    request: AdapterRequest,
    runtime: Runtime,
    model: ModelArtifact,
    command: tuple[str, ...],
    outcome: CommandOutcome,
) -> JsonObject:
    return result(
        request,
        status="pending",
        error_kind="lm_studio_download_pending",
        timed_out=True,
        exit_code=None,
        duration_ms=outcome.duration_ms,
        retry_safe=True,
        evidence_items=[
            _command_outcome_evidence(outcome, command),
            evidence(evidence_id="lm_studio_partial_bytes", kind="filesystem", status="pending", summary="Partial model download retained for an identical resumable retry", observed=partial_bytes(model, runtime.home), expected=model.size_bytes, source=str(model.path(runtime.home))),
        ],
        reason=_command_failure_reason(outcome),
        repair=f"Run solet create {request.name} again after reviewing its dry run. The model pull resumes existing bytes; preserve the partial file.",
    )


def _start_server(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    started = time.monotonic()
    budget = min(60, max(1, request.timeout_seconds - 10))
    lms = str(cli_path(runtime.home))
    # Seed before first start, then patch the materialized file and explicitly
    # restart. This also covers an existing daemon with cached server settings.
    if not disable_jit(runtime):
        return result(request, status="blocked", error_kind="lm_studio_jit_settings_invalid", repair=_repair(request))
    for arguments in (("daemon", "up"), ("server", "start", "--port", "1234", "--bind", "127.0.0.1")):
        outcome = runtime.run((lms, *arguments), timeout_seconds=min(15, budget))
        if not outcome.ok:
            if served_models(runtime) is None:
                return _failure(request, outcome, "lm_studio_server_start_failed", (lms, *arguments))
    failure = _reload_server_settings(request, runtime, lms)
    if failure is not None:
        return failure
    while time.monotonic() - started < budget:
        if _server_ready(runtime):
            return result(request, status="applied")
        time.sleep(0.25)
    return result(request, status="blocked", error_kind="lm_studio_server_unreachable", repair=_repair(request))


def _server_ready(runtime: Runtime) -> bool:
    return served_models(runtime) is not None and jit_disabled(runtime.home)


def _reload_server_settings(request: AdapterRequest, runtime: Runtime, lms: str) -> JsonObject | None:
    if not disable_jit(runtime):
        return result(request, status="blocked", error_kind="lm_studio_jit_settings_invalid", repair=_repair(request))
    for arguments in (("server", "stop"), ("server", "start", "--port", "1234", "--bind", "127.0.0.1")):
        outcome = runtime.run((lms, *arguments), timeout_seconds=15)
        if not outcome.ok:
            return _failure(request, outcome, "lm_studio_jit_restart_failed", (lms, *arguments))
    if not jit_disabled(runtime.home):
        return result(request, status="blocked", error_kind="lm_studio_jit_readback_failed", repair=_repair(request))
    return None


def _failure(
    request: AdapterRequest,
    outcome: CommandOutcome,
    error_kind: str,
    argv: tuple[str, ...],
    vendor_category: str | None = None,
) -> JsonObject:
    return result(
        request,
        status="failed",
        error_kind=error_kind,
        retry_safe=True,
        exit_code=outcome.returncode,
        timed_out=outcome.timed_out,
        duration_ms=outcome.duration_ms,
        evidence_items=[_command_outcome_evidence(outcome, argv, vendor_category)],
        reason=_command_failure_reason(outcome),
        repair=_repair(request),
    )


def _installer_never_ran(
    request: AdapterRequest, outcome: CommandOutcome, argv: tuple[str, ...]
) -> JsonObject:
    return result(
        request,
        status="blocked",
        error_kind="lm_studio_installer_did_not_run",
        retry_safe=False,
        exit_code=outcome.returncode,
        timed_out=outcome.timed_out,
        duration_ms=outcome.duration_ms,
        evidence_items=[_command_outcome_evidence(outcome, argv, evidence_id="lm_studio_installer_outcome")],
        reason=_command_failure_reason(outcome),
        repair=_repair(request),
    )


def _command_outcome_evidence(
    outcome: CommandOutcome,
    argv: tuple[str, ...],
    vendor_category: str | None = None,
    evidence_id: str = "lm_studio_command_outcome",
) -> JsonObject:
    """Expose reviewed command identity and bounded outcome facts, never streams."""

    category = vendor_category or "unclassified"
    return evidence(
        evidence_id=evidence_id,
        kind="command",
        status="observed",
        summary=(
            "LM Studio reviewed command outcome: "
            f"exit_code={outcome.returncode}; timed_out={outcome.timed_out}; "
            f"stdout_bytes={outcome.stdout_bytes}; stderr_bytes={outcome.stderr_bytes}."
        ),
        observed=[
            f"argv={' '.join(argv)}",
            f"exit_code={outcome.returncode}",
            f"timed_out={outcome.timed_out}",
            f"duration_ms={outcome.duration_ms}",
            f"stdout_bytes={outcome.stdout_bytes}",
            f"stderr_bytes={outcome.stderr_bytes}",
            f"stdout_truncated={outcome.stdout_truncated}",
            f"stderr_truncated={outcome.stderr_truncated}",
            f"vendor_failure_category={category}",
        ],
        expected="reviewed command completes without a timeout or nonzero exit",
        source="reviewed_lm_studio_registry",
    )


def _command_failure_reason(outcome: CommandOutcome) -> JsonObject:
    if outcome.timed_out:
        outcome_class = "timeout"
    elif outcome.executable_missing:
        outcome_class = "executable_missing"
    elif outcome.launch_error is not None:
        outcome_class = "launch_error"
    else:
        outcome_class = "nonzero_exit"
    return {
        "outcome_class": outcome_class,
        "exit_code": outcome.returncode,
        "duration_ms": outcome.duration_ms,
        "timed_out": outcome.timed_out,
        "stdout_bytes": outcome.stdout_bytes,
        "stderr_bytes": outcome.stderr_bytes,
        "stdout_truncated": outcome.stdout_truncated,
        "stderr_truncated": outcome.stderr_truncated,
    }


def _vendor_failure_category(outcome: CommandOutcome) -> str | None:
    """Recognize only the pinned vendor diagnostic on a failed model pull."""

    if (
        not outcome.timed_out
        and outcome.returncode not in (None, 0)
        and _VENDOR_DOWNLOAD_TIMEOUT in outcome.stderr
    ):
        return "vendor_download_timeout"
    return None
