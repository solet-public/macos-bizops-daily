"""Typed target-local probes consumed by create, start, and ``solet doctor``."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .launchagent_status import launchagent_health
from .readiness_support import (
    ReadinessBudget as _ReadinessBudget,
)
from .readiness_support import (
    ReadinessObservation as _ReadinessObservation,
)
from .readiness_support import (
    readiness_evidence as _readiness_evidence,
)
from .readiness_support import (
    readiness_timeout_result as _readiness_timeout_result,
)
from .setup_adapter_contract import (
    AdapterRequest,
    JsonObject,
    JsonValue,
    PublicEvidenceValue,
    evidence,
    result,
)
from .setup_adapter_runtime import (
    _STRUCTURED_OUTPUT_LIMIT,
    CommandOutcome,
    Runtime,
    resolve_executable,
)
from .setup_operations import genesis_artifacts_valid

type ProbeHandler = Callable[[AdapterRequest, Runtime], JsonObject]

_READINESS_GATED_PURPOSES = frozenset({"stage_exit", "completion"})
_READINESS_POLL_SECONDS = 0.5
_READINESS_PROBE_TIMEOUT_SECONDS = 2
_READINESS_INPUT_PREFIX = "startup_readiness_"
_READINESS_BUDGET_SOURCE = "executor_contracts.start_command.timeout_seconds"
_READINESS_BUDGET_UNIT = "seconds"
_READINESS_SEMANTIC_SCOPE = "target_start_through_target_cli_health_status_healthy"
_READINESS_RELEASE_SIGNAL = "target_cli_health_top_level_status_healthy"
_READINESS_SOURCE_ARTIFACT = "macos_setup_flow.json"
_READINESS_CONSUMERS = ("stage_exit", "completion")
_READINESS_CONSUMER_PROBES = ("embedding_request_succeeds",)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENVELOPE_KEYS = frozenset(
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
READINESS_PUBLIC_INPUT_KEYS = frozenset(
    {
        "startup_readiness_contract_version",
        "startup_readiness_contract_digest",
        "startup_readiness_source_artifact",
        "startup_readiness_budget_source",
        "startup_readiness_budget_unit",
        "startup_readiness_semantic_scope",
        "startup_readiness_release_signal",
        "startup_readiness_consumer_probe_purposes",
        "startup_readiness_consumer_probe_purpose",
        "startup_readiness_consumer_probe_refs",
        "startup_readiness_consumer_probe_ref",
        "startup_readiness_parent_budget_seconds",
        "startup_readiness_governed_process_call_seconds",
    }
)


def probe_handlers() -> dict[str, ProbeHandler]:
    """Return the closed registry of reviewed post-venv probes."""

    from .apple_setup_adapter import (
        asset_verified,
        embedding_config_valid,
        host_eligible,
        inference_config_valid,
        model_availability,
    )
    from .installation_model_doctor import (
        embedding_qualification,
        inference_qualification,
        model_discovery,
        ollama_discovery,
        shell_path,
        shell_python,
    )
    from .installation_plugin_doctor import (
        hook_behavior,
        plugin_roster,
        plugin_visible,
        selected_hooks,
        selected_plugins,
    )
    from .installation_state_doctor import (
        journal_resume,
        knowledge,
        peer_identity,
        router,
        session_retrieval,
        session_roots,
    )
    from .lm_studio_provisioning import probe_handlers as lm_studio_handlers

    handlers: dict[str, ProbeHandler] = {
        "setup::apple.host_eligible": host_eligible,
        "setup::apple.asset_verified": asset_verified,
        "setup::apple.embedding_config_valid": embedding_config_valid,
        "setup::apple.embedding_request_succeeds": _apple_embedding_request,
        "setup::apple.inference_config_valid": inference_config_valid,
        "setup::apple.model_availability": model_availability,
        **lm_studio_handlers(),
        "setup::tmux.probe": _tmux,
        "hydration::shell.probe_path": shell_path,
        "hydration::shell.probe_python": shell_python,
        "setup::models.discover_lm_studio": model_discovery,
        "setup::models.discover_ollama": ollama_discovery,
        "setup::models.discover_embeddings": model_discovery,
        "setup::models.discover_inference": model_discovery,
        "setup::models.qualify_embedding": embedding_qualification,
        "setup::models.qualify_structured_actions": inference_qualification,
        "setup::models.qualify_representative_inference": inference_qualification,
        "genesis::solet.verify": _genesis,
        "genesis::autostart.verify": _autostart,
        "hydration::codex.probe_cli": _cli_available,
        "hydration::claude.probe_cli": _cli_available,
        "setup::node.probe": _node_available,
        "hydration::codex.probe_plugin": plugin_visible,
        "hydration::claude.probe_plugin": plugin_visible,
        "hydration::codex.probe_fresh_session": hook_behavior,
        "hydration::claude.probe_fresh_session": hook_behavior,
        "setup::coding_agents.verify_plugins": selected_plugins,
        "setup::coding_agents.verify_hooks": selected_hooks,
        "service_interface::lifecycle_management_service.list_plugins": plugin_roster,
        "hydration::sessions.probe_codex_roots": session_roots,
        "hydration::sessions.probe_claude_roots": session_roots,
        "service_interface::session_ledger_service.qualify_selected_sources": session_retrieval,
        "service_interface::local_self_deployment_service.swap_status": router,
        "plugin::agent_messaging_plugin.peer_identity": peer_identity,
        "service_interface::knowledge_service.search": knowledge,
        "setup::journal.install_state_projection_matches": journal_resume,
        "plugin::salesforce_plugin.probe_cli": _salesforce_cli,
    }
    for reference in _GENERIC_PROCESS_REFS:
        handlers[reference] = _generic_process
    return handlers


_GENERIC_PROCESS_REFS = {
    "service_interface::embedding_service.get_embedding_dimension",
    "service_interface::inference_service.qualify",
    "service_interface::vault_service.qualify_keychain",
    "plugin::macos_vault_plugin.check_keychain",
    "plugin::macos_vault_plugin.qualify",
    "plugin::agent_messaging_plugin.qualify_fleet",
    "plugin::codex_filesystem_session_source_plugin.qualify",
    "plugin::claude_code_filesystem_session_source_plugin.qualify",
    "plugin::g_suite_plugin.test_connection",
    "plugin::jira_plugin.test_connection",
    "plugin::marketo_plugin.check_setup",
    "plugin::salesforce_plugin.test_connection",
    "plugin::salesforce_plugin.provision_cli",
    "plugin::schwab_market_data_plugin.test_connection",
    "plugin::snowflake_plugin.test_connection",
    "plugin::zuora_plugin.test_connection",
    "plugin::external_postgres_plugin.test_connection",
}


def _tmux(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    outcome = runtime.run(("/usr/bin/which", "tmux"), timeout_seconds=5)
    return _command_probe(request, outcome, "tmux_available", "Install tmux and resume.")


def _genesis(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    marker = request.target / ".solet/genesis.json"
    valid = genesis_artifacts_valid(request, runtime)
    return _boolean_probe(
        request,
        evidence_id="genesis_artifacts",
        ok=valid,
        observed=valid,
        source=str(marker),
        repair="Resume genesis until its final marker and declared target artifacts are valid.",
    )


def _autostart(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    if request.public_inputs.get("autostart") == "disabled":
        return result(
            request,
            status="not_applicable",
            evidence_items=[
                _evidence(
                    "launchagent_running",
                    True,
                    "resolved_autostart_decision",
                    "disabled",
                    "disabled",
                )
            ],
        )
    outcome = runtime.run(
        ("/bin/launchctl", "print", f"gui/{_uid()}/local.solet.{request.name}"),
        timeout_seconds=10,
    )
    healthy, observed, error_kind = launchagent_health(outcome)
    return _boolean_probe(
        request,
        "launchagent_running",
        healthy,
        observed,
        "launchctl:print",
        "Install or repair the LaunchAgent, then confirm it remains running without repeated failed exits.",
        duration_ms=outcome.duration_ms,
        error_kind=error_kind,
    )


def _uid() -> int:
    import os

    return os.getuid()


_CASK_LINK_RETRY_ATTEMPTS = 5
_CASK_LINK_RETRY_DELAY_SECONDS = 0.5


def _resolve_cli_post_apply(runtime: Runtime, cli: str) -> str | None:
    """Retry resolution on a post-apply verification of a just-installed cask.

    Both codex and claude ship as Homebrew casks; their post-apply probe can
    run before the linked executable is stat-visible to this freshly spawned
    process (measured live, iss_9d12ff3f/iss_e1d5285b) even though the cask
    genuinely installed. A pre-install probe (the tool legitimately absent)
    never reaches this path -- only probe_purpose == 'post_apply' does."""
    for attempt in range(_CASK_LINK_RETRY_ATTEMPTS):
        executable = resolve_executable(runtime, cli)
        if executable is not None:
            return executable
        if attempt < _CASK_LINK_RETRY_ATTEMPTS - 1:
            time.sleep(_CASK_LINK_RETRY_DELAY_SECONDS)
    return None


def _cli_available(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    cli = "claude" if "claude" in request.operation_ref else "codex"
    executable = (
        _resolve_cli_post_apply(runtime, cli)
        if request.probe_purpose == "post_apply"
        else resolve_executable(runtime, cli)
    )
    if executable is None:
        return _boolean_probe(
            request,
            f"{cli}_cli",
            False,
            False,
            f"executable:{cli} unresolved",
            f"Install the selected {cli} CLI through its provisioning operation.",
        )
    outcome = runtime.run((executable, "--version"), timeout_seconds=10)
    return _command_probe(
        request,
        outcome,
        f"{cli}_cli",
        f"Install the selected {cli} CLI through its provisioning operation.",
        source=f"executable:{executable}",
    )


def _node_available(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    executable = resolve_executable(runtime, "node")
    if executable is None:
        return _boolean_probe(
            request,
            "node_available",
            False,
            False,
            "executable:node unresolved",
            "Install Node through the Codex-selected provisioning operation.",
        )
    outcome = runtime.run((executable, "--version"), timeout_seconds=10)
    return _command_probe(
        request,
        outcome,
        "node_available",
        "Install Node through the Codex-selected provisioning operation.",
        source=f"executable:{executable}",
    )


def _generic_process(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    readiness = _generic_process_readiness(request, runtime)
    if isinstance(readiness, dict):
        return readiness
    key = _callable_to_process_key(request.operation_ref)
    arguments = {
        key: value
        for key, value in request.public_inputs.items()
        if not key.startswith(_READINESS_INPUT_PREFIX)
    }
    outcome = _solet_call(
        request,
        runtime,
        key,
        arguments,
        timeout_seconds=(
            readiness.budget.governed_process_call_seconds if readiness is not None else None
        ),
    )
    if outcome.stdout_truncated or outcome.stderr_truncated:
        return truncated_solet_call_output(request, key, outcome)
    valid = _call_succeeded(outcome)
    repair = (
        f"Expected {key} to complete; found no successful process result. Verify the embedding_service binding, configured endpoint, and model, then retry setup."
        if key == "service_interface::embedding_service::get_embedding_dimension"
        else (
            f"Expected {key} to complete; found no successful process result. Verify the declared provider is registered and ready, repair its configured dependency, then retry setup."
        )
    )
    return _boolean_probe(
        request,
        request.operation_id,
        valid,
        valid,
        key,
        repair,
        duration_ms=outcome.duration_ms,
        extra_evidence=(
            [_readiness_evidence(request, readiness, process_called=True)]
            if readiness is not None
            else None
        ),
    )


def _apple_embedding_request(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Prove the configured Core AI binding produces one usable vector."""

    from .apple_setup_adapter import _MODEL_ID, embedding_config_valid

    config = embedding_config_valid(request, runtime)
    if config["checkpoint_status"] != "verified":
        return blocked(
            request,
            "coreai_embedding_config_invalid",
            "Repair the pinned Core AI asset and plugin config, then retry the behavioral probe.",
        )
    if not _coreai_binding_valid(request.target):
        return blocked(
            request,
            "coreai_embedding_binding_invalid",
            "Bind embedding_service to the selected coreai_embeddings_plugin, then restart and retry.",
        )

    key = "service_interface::embedding_service::generate_embeddings"
    outcome = _solet_call(
        request,
        runtime,
        key,
        {
            "inputs": ["Apple embedding readiness probe"],
            "model": _MODEL_ID,
            "input_type": "text",
        },
    )
    if outcome.stdout_truncated or outcome.stderr_truncated:
        return truncated_solet_call_output(request, key, outcome)
    completed, vector_result = _apple_inference_result(outcome)
    if not completed:
        return blocked(
            request,
            "coreai_embedding_inference_failed",
            "The selected Core AI provider did not produce an embedding; repair its runtime and retry.",
        )
    if not _valid_apple_embedding_result(vector_result, _MODEL_ID):
        return blocked(
            request,
            "coreai_embedding_vector_invalid",
            "The selected Core AI provider returned no finite 768-element vector; repair its runtime and retry.",
        )
    return _boolean_probe(
        request,
        "coreai_embedding_behavior",
        True,
        "one finite 768-element vector",
        key,
        "The selected Core AI provider must return one finite 768-element vector.",
        duration_ms=outcome.duration_ms,
    )


def _coreai_binding_valid(target: Path) -> bool:
    path = target / "profile/config/service_bindings.json"
    if path.is_symlink() or not path.is_file():
        return False
    try:
        binding: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(binding, dict)
        and binding.get("embedding_service") == "coreai_embeddings_plugin"
    )


def _apple_inference_result(outcome: CommandOutcome) -> tuple[bool, object]:
    payload = _call_result(outcome)
    if payload is None or payload.get("action_status") != "completed":
        return False, None
    if payload.get("error") is not None or (
        "success" in payload and payload["success"] is not True
    ):
        return False, None
    data = payload.get("data")
    return True, data.get("result") if isinstance(data, dict) else None


def _valid_apple_embedding_result(value: object, model: str) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("model") != model or type(value.get("dimension")) is not int:
        return False
    if value["dimension"] != 768:
        return False
    vectors = value.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != 1:
        return False
    return _finite_apple_vector(vectors[0])


def _finite_apple_vector(vector: object) -> bool:
    if not isinstance(vector, list) or len(vector) != 768:
        return False
    try:
        return all(type(item) in (int, float) and math.isfinite(item) for item in vector)
    except (OverflowError, TypeError, ValueError):
        return False


def _generic_process_readiness(
    request: AdapterRequest,
    runtime: Runtime,
) -> _ReadinessObservation | JsonObject | None:
    if (
        request.probe_purpose in _READINESS_GATED_PURPOSES
        and request.operation_id in _READINESS_CONSUMER_PROBES
    ):
        return _wait_for_target_readiness(request, runtime)
    return None


def _salesforce_cli(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Require the Salesforce CLI's inner availability result, not its envelope."""

    key = "plugin::salesforce_plugin::probe_cli"
    outcome = _solet_call(request, runtime, key, {})
    if outcome.stdout_truncated or outcome.stderr_truncated:
        return truncated_solet_call_output(request, key, outcome)
    payload = _call_payload(outcome)
    valid = _salesforce_cli_valid(outcome, payload)
    observed = _salesforce_cli_observation(payload)
    return result(
        request,
        status="verified" if valid else "blocked",
        error_kind=None if valid else "salesforce_cli_missing",
        retry_safe=True,
        duration_ms=outcome.duration_ms,
        evidence_items=[
            _evidence(
                "salesforce_cli",
                valid,
                key,
                observed,
                "completed envelope with executable=true, configured=true, absolute path, and non-empty version",
            )
        ],
        repair=None
        if valid
        else "Provision the sf CLI through the reviewed Salesforce provisioning operation, then retry Salesforce configuration.",
    )


def _salesforce_cli_valid(outcome: CommandOutcome, payload: JsonObject) -> bool:
    return (
        _call_succeeded(outcome)
        and payload.get("executable") is True
        and payload.get("configured") is True
        and _absolute_path(payload.get("executable_path"))
        and _bounded_version(payload.get("version"))
    )


def _salesforce_cli_observation(payload: JsonObject) -> list[str]:
    executable_path = payload.get("executable_path")
    version = payload.get("version")
    observed: JsonObject = {
        "executable": payload.get("executable") is True,
        "configured": payload.get("configured") is True,
        "executable_path": executable_path if isinstance(executable_path, str) else None,
        "version": version if isinstance(version, str) and len(version) <= 256 else None,
    }
    return [
        f"{key}={json.dumps(value, sort_keys=True, separators=(',', ':'))}"
        for key, value in sorted(observed.items())
    ]


def _absolute_path(value: object) -> bool:
    return isinstance(value, str) and Path(value).is_absolute()


def _bounded_version(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def _wait_for_target_readiness(
    request: AdapterRequest,
    runtime: Runtime,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> _ReadinessObservation | JsonObject:
    """Wait until the target's existing bridge health signal is fully ready."""

    identity_failure = _readiness_target_identity_failure(request)
    if identity_failure is not None:
        return identity_failure
    budget = _readiness_budget(request)
    if isinstance(budget, dict):
        return budget
    wait_seconds = budget.effective_wait_seconds
    started = monotonic()
    deadline = started + wait_seconds
    last_status = "unreachable"
    probe_count = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _readiness_timeout_result(
                request,
                budget=budget,
                monotonic_origin=started,
                monotonic_deadline=deadline,
                observed_at=monotonic(),
                last_status=last_status,
                probe_count=probe_count,
            )
        outcome = runtime.run(
            (str(request.target / ".venv/bin/solet-bridge"), "health"),
            timeout_seconds=max(
                1,
                min(_READINESS_PROBE_TIMEOUT_SECONDS, math.ceil(remaining)),
            ),
            cwd=request.target,
            extra_env={"SOLET_NAME": request.name},
        )
        probe_count += 1
        last_status = _bridge_health_status(outcome)
        observed_at = monotonic()
        if last_status == "healthy" and observed_at < deadline:
            return _ReadinessObservation(
                budget=budget,
                monotonic_origin=started,
                monotonic_deadline=deadline,
                observed_at=observed_at,
                last_status=last_status,
                probe_count=probe_count,
            )
        remaining = deadline - observed_at
        if remaining <= 0:
            continue
        sleep(min(_READINESS_POLL_SECONDS, remaining))


def _readiness_target_identity_failure(request: AdapterRequest) -> JsonObject | None:
    target = request.target
    executable = target / ".venv/bin/solet-bridge"
    expected_shebang = f"#!{target}/.venv/bin/python3"
    try:
        target_valid = (
            target.is_dir() and not target.is_symlink() and target.resolve(strict=True) == target
        )
        executable_valid = executable.is_file() and not executable.is_symlink()
        with executable.open(encoding="utf-8") as stream:
            first_line = stream.readline().rstrip("\n")
    except (OSError, UnicodeError):
        target_valid = False
        executable_valid = False
        first_line = "unavailable"
    if target_valid and executable_valid and first_line == expected_shebang:
        return None
    return result(
        request,
        status="blocked",
        error_kind="target_readiness_identity_invalid",
        retry_safe=False,
        evidence_items=[
            evidence(
                evidence_id="target_readiness_identity",
                kind="readiness",
                status="blocked",
                summary="target-local health executable identity is invalid",
                observed="noncanonical_or_foreign",
                expected=expected_shebang,
                source=str(executable),
            )
        ],
        repair=(
            f"Restore the canonical non-symlink target at {target} and its target-bound launcher {executable}; the governed process was not called."
        ),
    )


def _bridge_health_status(outcome: CommandOutcome) -> str:
    if outcome.timed_out:
        return "unreachable_timeout"
    if not outcome.ok:
        return "unreachable"
    try:
        payload: object = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        return "invalid_response"
    if not isinstance(payload, dict):
        return "invalid_response"
    status = payload.get("status")
    return status if isinstance(status, str) and status else "invalid_response"


def _readiness_budget(request: AdapterRequest) -> _ReadinessBudget | JsonObject:
    values = request.public_inputs
    present_keys = {key for key in values if key.startswith(_READINESS_INPUT_PREFIX)}
    if not present_keys:
        return _readiness_configuration_failure(
            request,
            "startup readiness lineage was not transported to this readiness consumer",
            observed="absent_transport",
            expected="complete_derived_budget_transport",
            repair=(
                "Provide the released startup readiness lineage to this declared "
                "consumer; the governed process was not called."
            ),
        )
    if present_keys != set(READINESS_PUBLIC_INPUT_KEYS):
        return _readiness_configuration_failure(
            request,
            "startup readiness lineage has a missing or unknown field",
        )
    contract_version = values["startup_readiness_contract_version"]
    digest = values["startup_readiness_contract_digest"]
    source_artifact = values["startup_readiness_source_artifact"]
    budget_source = values["startup_readiness_budget_source"]
    budget_unit = values["startup_readiness_budget_unit"]
    semantic_scope = values["startup_readiness_semantic_scope"]
    release_signal = values["startup_readiness_release_signal"]
    raw_consumers = values["startup_readiness_consumer_probe_purposes"]
    consumer = values["startup_readiness_consumer_probe_purpose"]
    raw_consumer_probes = values["startup_readiness_consumer_probe_refs"]
    consumer_probe = values["startup_readiness_consumer_probe_ref"]
    parent = values["startup_readiness_parent_budget_seconds"]
    reserve = values["startup_readiness_governed_process_call_seconds"]
    scalars_valid = _readiness_identity_valid(
        request=request,
        contract_version=contract_version,
        digest=digest,
        source_artifact=source_artifact,
        budget_source=budget_source,
        budget_unit=budget_unit,
        semantic_scope=semantic_scope,
        release_signal=release_signal,
        raw_consumers=raw_consumers,
        consumer=consumer,
        raw_consumer_probes=raw_consumer_probes,
        consumer_probe=consumer_probe,
    )
    arithmetic_valid = _readiness_arithmetic_valid(
        request=request,
        parent=parent,
        reserve=reserve,
    )
    if not scalars_valid or not arithmetic_valid:
        return _readiness_configuration_failure(
            request,
            "startup readiness lineage contradicts its released source",
        )
    return _ReadinessBudget(
        contract_version=cast(int, contract_version),
        contract_digest=cast(str, digest),
        source_artifact=cast(str, source_artifact),
        budget_source=cast(str, budget_source),
        budget_unit=cast(str, budget_unit),
        semantic_scope=cast(str, semantic_scope),
        release_signal=cast(str, release_signal),
        consumer_probe_purposes=tuple(_READINESS_CONSUMERS),
        consumer_probe_refs=tuple(_READINESS_CONSUMER_PROBES),
        parent_budget_seconds=cast(int, parent),
        governed_process_call_seconds=cast(int, reserve),
    )


def _readiness_identity_valid(
    *,
    request: AdapterRequest,
    contract_version: JsonValue,
    digest: JsonValue,
    source_artifact: JsonValue,
    budget_source: JsonValue,
    budget_unit: JsonValue,
    semantic_scope: JsonValue,
    release_signal: JsonValue,
    raw_consumers: JsonValue,
    consumer: JsonValue,
    raw_consumer_probes: JsonValue,
    consumer_probe: JsonValue,
) -> bool:
    version_valid = not isinstance(contract_version, bool) and contract_version == 1
    digest_valid = isinstance(digest, str) and _SHA256.fullmatch(digest) is not None
    source_valid = all(
        (
            source_artifact == _READINESS_SOURCE_ARTIFACT,
            budget_source == _READINESS_BUDGET_SOURCE,
            budget_unit == _READINESS_BUDGET_UNIT,
            semantic_scope == _READINESS_SEMANTIC_SCOPE,
            release_signal == _READINESS_RELEASE_SIGNAL,
        )
    )
    consumer_valid = all(
        (
            raw_consumers == list(_READINESS_CONSUMERS),
            consumer == request.probe_purpose,
            consumer in _READINESS_CONSUMERS,
            raw_consumer_probes == list(_READINESS_CONSUMER_PROBES),
            consumer_probe == request.operation_id,
            consumer_probe in _READINESS_CONSUMER_PROBES,
        )
    )
    return all((version_valid, digest_valid, source_valid, consumer_valid))


def _readiness_arithmetic_valid(
    *,
    request: AdapterRequest,
    parent: JsonValue,
    reserve: JsonValue,
) -> bool:
    return (
        not isinstance(parent, bool)
        and isinstance(parent, int)
        and not isinstance(reserve, bool)
        and isinstance(reserve, int)
        and parent == request.timeout_seconds
        and reserve >= 1
        and reserve < parent
    )


def _readiness_configuration_failure(
    request: AdapterRequest,
    reason: str,
    *,
    observed: str = "invalid",
    expected: str = "finite_positive_derived_budget",
    repair: str | None = None,
) -> JsonObject:
    source = f"{_READINESS_SOURCE_ARTIFACT}:{_READINESS_BUDGET_SOURCE}"
    return result(
        request,
        status="blocked",
        error_kind="target_readiness_configuration_invalid",
        retry_safe=False,
        evidence_items=[
            evidence(
                evidence_id="target_readiness_budget",
                kind="configuration",
                status="blocked",
                summary=reason,
                observed=observed,
                expected=expected,
                source=source,
            )
        ],
        repair=repair
        or (
            f"Restore the validated startup readiness authority at {source}; the governed process was not called."
        ),
    )


def _callable_to_process_key(reference: str) -> str:
    prefix, remainder = reference.split("::", maxsplit=1)
    if "." not in remainder:
        return reference
    provider, function = remainder.rsplit(".", maxsplit=1)
    return f"{prefix}::{provider}::{function}"


def _solet_call(
    request: AdapterRequest,
    runtime: Runtime,
    process_key: str,
    arguments: JsonObject,
    *,
    timeout_seconds: int | None = None,
) -> CommandOutcome:
    return runtime.run(
        (
            str(request.target / ".venv/bin/solet-bridge"),
            "call",
            process_key,
            json.dumps(arguments, separators=(",", ":")),
        ),
        timeout_seconds=(
            min(request.timeout_seconds, 60) if timeout_seconds is None else timeout_seconds
        ),
        cwd=request.target,
        extra_env={"SOLET_NAME": request.name},
        output_limit=_STRUCTURED_OUTPUT_LIMIT,
    )


def _call_payload(outcome: CommandOutcome) -> JsonObject:
    outer = _call_result(outcome)
    if outer is None or outer.get("success") is not True:
        return {}
    data = outer.get("data")
    return data if isinstance(data, dict) else {}


def _merged_call_payload(outcome: CommandOutcome) -> JsonObject:
    """Return the bare service result splatted into a successful call envelope."""

    outer = _call_result(outcome)
    if outer is None or outer.get("success") is not True:
        return {}
    return {key: value for key, value in outer.items() if key not in _ENVELOPE_KEYS}


def _call_succeeded(outcome: CommandOutcome) -> bool:
    outer = _call_result(outcome)
    return outer is not None and outer.get("success") is True and outer.get("error") is None


def _call_result(outcome: CommandOutcome) -> JsonObject | None:
    if not outcome.ok:
        return None
    try:
        raw: object = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    outer = raw.get("result")
    return cast(JsonObject, outer) if isinstance(outer, dict) else None


def nonempty_process_probe(
    request: AdapterRequest,
    outcome: CommandOutcome,
    evidence_id: str,
) -> JsonObject:
    payload = _call_payload(outcome)
    count = payload.get("count")
    results = payload.get("results")
    nonempty = (isinstance(count, int) and count > 0) or (
        isinstance(results, list) and bool(results)
    )
    return _boolean_probe(
        request,
        evidence_id,
        nonempty,
        nonempty,
        evidence_id,
        f"Repair {evidence_id.replace('_', ' ')} and require a non-empty result.",
    )


def _command_probe(
    request: AdapterRequest,
    outcome: CommandOutcome,
    evidence_id: str,
    repair: str,
    *,
    source: str | None = None,
) -> JsonObject:
    return _boolean_probe(
        request,
        evidence_id,
        outcome.ok,
        outcome.ok,
        source or f"command:{evidence_id}",
        repair,
        duration_ms=outcome.duration_ms,
    )


def _boolean_probe(
    request: AdapterRequest,
    evidence_id: str,
    ok: bool,
    observed: PublicEvidenceValue,
    source: str,
    repair: str,
    *,
    duration_ms: int = 0,
    extra_evidence: list[JsonObject] | None = None,
    error_kind: str | None = None,
) -> JsonObject:
    evidence_items = [_evidence(evidence_id, ok, source, observed, True)]
    if extra_evidence is not None:
        evidence_items.extend(extra_evidence)
    return result(
        request,
        status="verified" if ok else "blocked",
        error_kind=None if ok else error_kind or f"{evidence_id}_failed",
        retry_safe=True,
        duration_ms=duration_ms,
        evidence_items=evidence_items,
        repair=None if ok else repair,
    )


def _evidence(
    evidence_id: str,
    ok: bool,
    source: str,
    observed: PublicEvidenceValue,
    expected: PublicEvidenceValue,
) -> JsonObject:
    return evidence(
        evidence_id=evidence_id,
        kind="behavior",
        status="verified" if ok else "blocked",
        summary=f"{evidence_id} {'verified' if ok else 'not verified'}",
        observed=observed,
        expected=expected,
        source=source,
    )


def truncated_solet_call_output(
    request: AdapterRequest,
    process_key: str,
    outcome: CommandOutcome,
) -> JsonObject:
    observed: JsonObject = {
        "stdout_bytes": outcome.stdout_bytes,
        "stderr_bytes": outcome.stderr_bytes,
        "stdout_truncated": outcome.stdout_truncated,
        "stderr_truncated": outcome.stderr_truncated,
    }
    expected: JsonObject = {
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    return blocked(
        request,
        "output_truncated",
        (
            f"{process_key} output was truncated: stdout {outcome.stdout_bytes} bytes (truncated={outcome.stdout_truncated}), stderr {outcome.stderr_bytes} bytes (truncated={outcome.stderr_truncated})."
        ),
        evidence_items=[
            _evidence(
                "output_truncated",
                False,
                process_key,
                [
                    f"{key}={json.dumps(value, sort_keys=True, separators=(',', ':'))}"
                    for key, value in sorted(observed.items())
                ],
                [
                    f"{key}={json.dumps(value, sort_keys=True, separators=(',', ':'))}"
                    for key, value in sorted(expected.items())
                ],
            )
        ],
    )


def blocked(
    request: AdapterRequest,
    error_kind: str,
    repair: str,
    *,
    evidence_items: list[JsonObject] | None = None,
) -> JsonObject:
    return result(
        request,
        status="blocked",
        error_kind=error_kind,
        retry_safe=True,
        evidence_items=evidence_items,
        repair=repair,
    )


__all__ = ["ProbeHandler", "_merged_call_payload", "probe_handlers"]
