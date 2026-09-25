"""Reviewed, resumable LM Studio provisioning operations and their probes."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Literal

from .lm_studio_deadline import WAIT_PURPOSES, ServedDeadline, run_with_deadline
from .lm_studio_index import (
    IndexReadback,
    VerifiedLocalSource,
    index_failure,
    inference_index,
    inference_observation,
    loaded_result,
    local_artifact_result,
    local_mode_requested,
    run_terminal_inference,
    same_index,
    terminal_loaded_result,
    wait_inference,
)
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
    embedding_observation,
    model_loaded,
    reviewed_models,
    selected_roles,
    served_models,
)
from .lm_studio_pull import PullContext as _PullContext
from .lm_studio_pull import command_failure as _failure
from .lm_studio_pull import command_failure_reason as _command_failure_reason
from .lm_studio_pull import command_outcome_evidence as _command_outcome_evidence
from .lm_studio_pull import pull_model as _pull
from .lm_studio_pull import repair_hint as _repair
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
    "ensure_index_inference": "inference_model_indexed",
    "load_inference": "inference_model_served",
    "install_login_agent": "login_agent_valid",
}
INSTALLER_URL = "https://lmstudio.ai/install.sh"
INSTALLER_VERSION = "0.0.23-1"
type Handler = Callable[[AdapterRequest, Runtime], JsonObject]
type Observation = bool | None | Literal["gui_session_absent"]


def operation_handlers() -> dict[str, Handler]:
    return {f"setup::lm_studio.{name}": provision for name in _OPERATIONS}


def probe_handlers() -> dict[str, Handler]:
    return {f"setup::lm_studio.{name}": probe for name in (*_OPERATIONS.values(), "jit_disabled")}


def _inputs_valid(request: AdapterRequest) -> bool:
    return request.public_inputs.get("lm_studio_base_url") in {BASE_URL, "http://localhost:1234/v1"} and bool(selected_roles(request.public_inputs))


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


def probe(request: AdapterRequest, runtime: Runtime, *, local_source: VerifiedLocalSource | None = None) -> JsonObject:
    """Every probe is passive, including calls before or after an apply."""

    if not _inputs_valid(request):
        return result(request, status="blocked", error_kind="lm_studio_inputs_invalid", repair=_repair(request))
    suffix = request.operation_ref.rsplit(".", 1)[1]
    models = reviewed_models(request.target)
    if suffix == "inference_artifact_present" and local_mode_requested(request, local_source):
        return local_artifact_result(request, runtime, models["inference"], local_source, _repair(request))
    if suffix == "inference_model_indexed":
        return inference_index(request, runtime, models["inference"], _repair(request), local_source)
    if suffix in {"inference_model_served", "embedding_model_served"}:
        return _served_operation(request, runtime, models[_role(suffix)], local_source)
    return _observed_result(request, suffix, _observation(suffix, runtime, models))


def provision(request: AdapterRequest, runtime: Runtime, *, local_source: VerifiedLocalSource | None = None) -> JsonObject:
    """Re-probe at apply time; preserve complete and partial host artifacts."""

    if not _inputs_valid(request):
        return result(request, status="blocked", error_kind="lm_studio_inputs_invalid", repair=_repair(request))
    suffix = request.operation_ref.rsplit(".", 1)[1]
    models = reviewed_models(request.target)
    # The Qwen branch binds a verified artifact to an index key and then to
    # the configured API identifier. Generic model load would skip both reads.
    special = _special_inference_operation(request, runtime, suffix, models["inference"], local_source)
    if special is not None:
        return special
    if suffix == "load_embedding":
        return _served_operation(request, runtime, models["embeddings"], local_source)
    observed = _operation_satisfied(suffix, runtime, models)
    if observed == "gui_session_absent":
        return _gui_session_required(request)
    if observed is None and request.phase == "apply":
        return _observed_result(request, _OPERATIONS[suffix], None)
    if observed:
        return result(request, status="verified" if request.phase == "probe" else "applied")
    return _provision_missing(request, runtime, suffix, models)


def _special_inference_operation(
    request: AdapterRequest,
    runtime: Runtime,
    suffix: str,
    model: ModelArtifact,
    local_source: VerifiedLocalSource | None,
) -> JsonObject | None:
    """Handle only the three inference operations that need source identity.

    The vendor pull remains in Fix A's generic path unless a Manager-issued
    local source was supplied for this exact transaction.
    """
    if suffix == "pull_inference" and local_mode_requested(request, local_source):
        return local_artifact_result(request, runtime, model, local_source, _repair(request))
    if suffix == "ensure_index_inference":
        return inference_index(request, runtime, model, _repair(request), local_source)
    if suffix == "load_inference":
        return _served_operation(request, runtime, model, local_source)
    return None


def _provision_missing(request: AdapterRequest, runtime: Runtime, suffix: str, models: dict[str, ModelArtifact]) -> JsonObject:
    if request.phase == "probe":
        return result(request, status="pending", actions=[_action(request, runtime, suffix, models)], repair=_repair(request))
    if suffix == "install":
        return _install(request, runtime)
    if not cli_available(runtime.home):
        return result(request, status="blocked", error_kind="lm_studio_cli_unavailable", repair=_repair(request))
    return _apply(request, runtime, suffix, models)


def _gui_session_required(request: AdapterRequest) -> JsonObject:
    return result(
        request,
        status="blocked",
        error_kind="lm_studio_login_agent_gui_session_required",
        repair=(
            "Log into the macOS graphical desktop as the account running setup, "
            f"then run solet create {request.name} --dry-run --json and review "
            "the new fingerprint before resuming."
        ),
    )


def _operation_satisfied(suffix: str, runtime: Runtime, models: dict[str, ModelArtifact]) -> Observation:
    if suffix == "install_login_agent":
        classification = login_classification(runtime, models)
        if classification == "gui_session_absent":
            return "gui_session_absent"
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


def _served_operation(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, local_source: VerifiedLocalSource | None) -> JsonObject:
    if model.role == "inference":
        return run_terminal_inference(request, lambda deadline: _inference_load(request, runtime, model, local_source, deadline), _repair(request))
    return run_with_deadline(request, lambda deadline: _embedding_load(request, runtime, model, deadline), _repair(request))


def _inference_load(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, local_source: VerifiedLocalSource | None, deadline: ServedDeadline | None) -> JsonObject:
    source, indexed, loaded = inference_observation(request, runtime, model, local_source, deadline)
    if source is None or indexed.status != "indexed":
        return index_failure(request, indexed, _repair(request))
    if (terminal := terminal_loaded_result(request, model, indexed, loaded, _repair(request))) is not None:
        return terminal
    if not cli_available(runtime.home):
        return result(request, status="blocked", error_kind="lm_studio_cli_unavailable", repair=_repair(request))
    if request.phase == "probe" and request.probe_purpose in WAIT_PURPOSES:
        assert deadline is not None
        return wait_inference(request, runtime, model, local_source, indexed, loaded, deadline, _repair(request))
    observed = loaded_result(request, model, source, indexed, loaded, _repair(request))
    if observed is not None:
        return observed
    assert deadline is not None
    return _load_absent_inference(request, runtime, model, local_source, indexed, deadline)


def _load_absent_inference(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, local_source: VerifiedLocalSource | None, indexed: IndexReadback, deadline: ServedDeadline) -> JsonObject:
    command = (str(cli_path(runtime.home)), "load", indexed.model_key or "", "--gpu", "off", "--context-length", "8192", "--identifier", model.api_identifier, "--yes")
    outcome = runtime.run(command, timeout_seconds=deadline.call_budget(min(290, request.timeout_seconds - 10), reserve=25))
    deadline.record_command(outcome)
    if not outcome.ok or outcome.stdout_truncated or outcome.stderr_truncated:
        return _failure(request, outcome, "lm_studio_load_inference_failed", command)
    deadline.load_succeeded = True
    deadline.check()
    source, after, served = inference_observation(request, runtime, model, local_source, deadline)
    if (terminal := terminal_loaded_result(request, model, after, served, _repair(request))) is not None:
        return terminal
    if source is None or not same_index(indexed, after):
        return result(request, status="blocked", error_kind="lm_studio_index_source_changed", retry_safe=False, repair=_repair(request))
    answer = wait_inference(request, runtime, model, local_source, indexed, served, deadline, _repair(request))
    items = answer["evidence"]
    assert isinstance(items, list)
    items.append(_command_outcome_evidence(outcome, command))
    return answer


def _embedding_load(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, deadline: ServedDeadline | None) -> JsonObject:
    observed = embedding_observation(runtime, model, deadline)
    if request.phase == "apply" and observed == "absent":
        assert deadline is not None
        failed = _load_absent_embedding(request, runtime, model, deadline)
        if failed is not None:
            return failed
        observed = embedding_observation(runtime, model, deadline)
    waiting = request.probe_purpose in WAIT_PURPOSES or (deadline is not None and deadline.load_succeeded)
    if waiting:
        assert deadline is not None
        while observed in {"absent", "transport_unknown"}:
            deadline.pause()
            observed = embedding_observation(runtime, model, deadline)
    return _embedding_result(request, runtime, model, observed)


def _embedding_result(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, observed: str) -> JsonObject:
    if observed in {"protocol_error", "transport_unknown"}:
        return result(request, status="blocked", error_kind=f"lm_studio_served_{observed}", retry_safe=False, repair=_repair(request))
    if observed == "absent":
        actions = [_action(request, runtime, "load_embedding", {"embeddings": model})] if request.probe_purpose in {"preview", "pre_apply"} else []
        return result(request, status="pending", actions=actions, repair=_repair(request))
    return result(request, status="applied" if request.phase == "apply" else "verified")


def _load_absent_embedding(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, deadline: ServedDeadline) -> JsonObject | None:
    if not artifact_present(model, runtime.home, deadline) or not cli_available(runtime.home):
        return result(request, status="blocked", error_kind="lm_studio_embedding_artifact_invalid", retry_safe=False, repair=_repair(request))
    command = (str(cli_path(runtime.home)), *model.load_argv)
    outcome = runtime.run(command, timeout_seconds=deadline.call_budget(min(290, request.timeout_seconds - 10), reserve=25))
    deadline.record_command(outcome)
    if not outcome.ok or outcome.stdout_truncated or outcome.stderr_truncated:
        return _failure(request, outcome, "lm_studio_load_embedding_failed", command)
    deadline.load_succeeded = True
    deadline.check()
    return None


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
    budget = max(1, request.timeout_seconds - 10)
    deadline = time.monotonic() + budget
    if suffix.startswith("pull_"):
        return _pull(_PullContext(request, runtime, suffix, model, command, deadline))
    outcome = runtime.run(command, timeout_seconds=budget)
    if not outcome.ok:
        return _failure(request, outcome, f"lm_studio_{suffix}_failed", command)
    return result(request, status="applied", duration_ms=outcome.duration_ms)


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


