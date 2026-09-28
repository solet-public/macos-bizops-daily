"""Closed target-local adapter for the executable macOS setup flow."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Protocol, TextIO

from .existing_install_operations import EXISTING_ALLOWED_PUBLIC_INPUTS
from .existing_install_operations import operation_handlers as existing_operation_handlers
from .installation_doctor import READINESS_PUBLIC_INPUT_KEYS, probe_handlers
from .lm_studio_deadline import PARENT_DEADLINE_KEY, SERVED_REFS
from .lm_studio_index import LOCAL_SOURCE_REF_KEY, VerifiedLocalSource
from .lm_studio_provisioning import PUBLIC_INPUT_KEYS as LM_STUDIO_PUBLIC_INPUT_KEYS
from .lm_studio_provisioning import operation_handlers as lm_studio_operations
from .lm_studio_provisioning import probe as probe_lm_studio
from .lm_studio_provisioning import probe_handlers as lm_studio_probes
from .lm_studio_provisioning import provision as provision_lm_studio
from .profile_implementations import IMPLEMENTATION_DECISIONS
from .setup_adapter_contract import (
    AdapterInputError,
    AdapterRequest,
    JsonObject,
    evidence,
    result,
)
from .setup_adapter_runtime import Runtime, SystemRuntime
from .setup_operations import operation_handlers
from .target_reconciliation import (
    TargetReconciliationRequest,
    is_reconciliation_payload,
    reconciliation_envelope,
)

_IDENTITY_INPUT_REFS = {
    "genesis::solet.run",
    "hydration::claude.install_plugin",
    "hydration::codex.install_plugin",
}
_LOCAL_SOURCE_OPERATION_REFS = frozenset({
    "setup::lm_studio.pull_inference",
    "setup::lm_studio.ensure_index_inference",
    "setup::lm_studio.load_inference",
})
_LOCAL_SOURCE_PROBE_REFS = frozenset({
    "setup::lm_studio.inference_artifact_present",
    "setup::lm_studio.inference_model_indexed",
    "setup::lm_studio.inference_model_served",
})
_LOCAL_SOURCE_REFS = _LOCAL_SOURCE_OPERATION_REFS | _LOCAL_SOURCE_PROBE_REFS
_ALLOWED_PUBLIC_INPUTS: dict[str, frozenset[str]] = {
    **dict.fromkeys((*lm_studio_operations(), *lm_studio_probes()), LM_STUDIO_PUBLIC_INPUT_KEYS),
    **dict.fromkeys(_LOCAL_SOURCE_REFS, LM_STUDIO_PUBLIC_INPUT_KEYS | {LOCAL_SOURCE_REF_KEY}),
    **{ref: LM_STUDIO_PUBLIC_INPUT_KEYS | {PARENT_DEADLINE_KEY} | ({LOCAL_SOURCE_REF_KEY} if ref in _LOCAL_SOURCE_REFS else set()) for ref in SERVED_REFS},
    # The existing-install flow's closed handler rows (design section 3.2); the
    # create flow never reaches them and they never reach a genesis handler.
    **EXISTING_ALLOWED_PUBLIC_INPUTS,
    "hydration::shell.install": frozenset({"git_controller_name"}),
    "service_interface::embedding_service.get_embedding_dimension": (
        READINESS_PUBLIC_INPUT_KEYS
    ),
    "genesis::solet.run": frozenset(
        {"solet_name", "clone_directory", "setup_profile", "autostart", *IMPLEMENTATION_DECISIONS}
    ),
    # The llama.cpp service and model callables serve only the roles those decisions selected.
    **dict.fromkeys(
        (
            "setup::llama_cpp.install_services",
            "setup::llama_cpp.models_present",
            "setup::llama_cpp.services_current",
        ),
        frozenset(IMPLEMENTATION_DECISIONS),
    ),
    "genesis::autostart.install": frozenset({"setup_profile", "autostart"}),
    "genesis::solet.verify": frozenset({"autostart"}),
    "genesis::autostart.verify": frozenset({"autostart"}),
    "setup::coding_agents.verify_plugins": frozenset({"selected_coding_agents"}),
    "setup::coding_agents.verify_hooks": frozenset({"selected_coding_agents"}),
    "plugin::agent_messaging_plugin.peer_identity": frozenset({"selected_coding_agents"}),
    "hydration::claude.install_plugin": frozenset({"solet_name", "clone_directory"}),
    "hydration::codex.install_plugin": frozenset({"solet_name", "clone_directory"}),
    "setup::models.configure_lm_studio_embeddings": frozenset(
        {"lm_studio_base_url", "model"}
    ),
    "setup::models.configure_lm_studio_inference": frozenset(
        {"lm_studio_base_url", "model"}
    ),
    "setup::models.discover_lm_studio": frozenset(
        {"decision_id", "lm_studio_base_url"}
    ),
    "setup::models.discover_ollama": frozenset({"decision_id"}),
    "setup::models.discover_embeddings": frozenset({"decision_id"}),
    "setup::models.discover_inference": frozenset({"decision_id"}),
    "setup::models.qualify_embedding": frozenset({"decision_id", "candidate_id"}),
    "setup::models.qualify_structured_actions": frozenset(
        {"decision_id", "candidate_id"}
    ),
    "setup::models.qualify_representative_inference": frozenset(
        {"decision_id", "candidate_id"}
    ),
    "plugin::g_suite_plugin.configure": frozenset({"google_oauth_client_id"}),
    "plugin::jira_plugin.configure": frozenset({"jira_base_url", "jira_email"}),
    "plugin::marketo_plugin.configure": frozenset({"marketo_client_id"}),
    "plugin::salesforce_plugin.configure": frozenset(
        {"salesforce_alias", "salesforce_instance_url"}
    ),
    "plugin::salesforce_plugin.provision_cli": frozenset({"acknowledge_system_change"}),
    "plugin::schwab_market_data_plugin.configure": frozenset(
        {"schwab_client_id", "schwab_callback_url"}
    ),
    "plugin::snowflake_plugin.configure": frozenset(
        {"snowflake_account", "snowflake_user", "snowflake_role"}
    ),
    "plugin::zuora_plugin.configure": frozenset({"zuora_base_url", "zuora_client_id"}),
    "plugin::external_postgres_plugin.configure_connection": frozenset(
        {"external_postgres_name", "external_postgres_host", "external_postgres_user"}
    ),
}


class LocalSourceResolver(Protocol):
    """B2 injects a Manager-record resolver; the public ref is never a path."""

    def resolve(self, ref: str, request: AdapterRequest) -> VerifiedLocalSource:
        """Return a checked typed source or raise on invalid input."""
        ...


def dispatch_request(
    request: AdapterRequest,
    runtime: Runtime,
    *,
    local_source_resolver: LocalSourceResolver | None = None,
) -> JsonObject:
    """Dispatch one validated request through the closed reviewed registries."""

    input_error = _validate_operation_inputs(request)
    if input_error is not None:
        return input_error
    if LOCAL_SOURCE_REF_KEY in request.public_inputs:
        return _dispatch_local_source(request, runtime, local_source_resolver)
    if request.operation_ref.startswith("existing::"):
        # The existing-install vocabulary dispatches through its own closed
        # table and never touches the create registries (design section 3.2),
        # so an existing-install request loads none of the create flow's
        # doctor or platform imports.
        handler = existing_operation_handlers().get(request.operation_ref)
    else:
        operation = operation_handlers().get(request.operation_ref)
        handler = operation
        if request.phase == "probe":
            handler = operation or probe_handlers().get(request.operation_ref)
    if handler is not None:
        try:
            return handler(request, runtime)
        except (OSError, RuntimeError) as exc:
            return _runtime_failure(request, exc)
    return result(
        request,
        status="blocked",
        error_kind="adapter_missing",
        retry_safe=False,
        exit_code=None,
        repair="Add a reviewed handler for the exact flow-declared callable reference.",
    )


def _dispatch_local_source(
    request: AdapterRequest,
    runtime: Runtime,
    resolver: LocalSourceResolver | None,
) -> JsonObject:
    ref = request.public_inputs[LOCAL_SOURCE_REF_KEY]
    if not isinstance(ref, str) or not ref or resolver is None:
        return _local_source_invalid(request)
    try:
        resolved = _checked_local_source(resolver.resolve(ref, request))
    except (OSError, RuntimeError, TypeError, ValueError):
        return _local_source_invalid(request)
    if resolved is None:
        return _local_source_invalid(request)
    try:
        if request.operation_ref in _LOCAL_SOURCE_OPERATION_REFS:
            return provision_lm_studio(request, runtime, local_source=resolved)
        if request.operation_ref in _LOCAL_SOURCE_PROBE_REFS:
            return probe_lm_studio(request, runtime, local_source=resolved)
    except (OSError, RuntimeError) as exc:
        return _runtime_failure(request, exc)
    return _protocol_failure(request)


def _checked_local_source(value: object) -> VerifiedLocalSource | None:
    return value if isinstance(value, VerifiedLocalSource) else None


def _local_source_invalid(request: AdapterRequest) -> JsonObject:
    return result(
        request,
        status="blocked",
        error_kind="lm_studio_local_source_invalid",
        retry_safe=False,
        repair="Re-preview and approve the verified local Qwen source before resuming.",
    )


def _runtime_failure(request: AdapterRequest, exc: OSError | RuntimeError) -> JsonObject:
    """Return bounded evidence for a non-specific reviewed runtime failure.

    Handler-specific transport and response errors belong in their handlers so
    their actionable result contracts take priority.  Type and value errors are
    intentionally not caught here: they are programming failures, not an
    adapter result that can truthfully be repaired by retrying a service.
    """

    exception_type = type(exc).__name__
    exception_summary = str(exc)[:256]
    return result(
        request,
        status="failed",
        error_kind="adapter_runtime_error",
        retry_safe=False,
        exit_code=None,
        evidence_items=[
            evidence(
                evidence_id="adapter_runtime_error",
                kind="runtime",
                status="failed",
                summary=f"reviewed handler raised {exception_type}: {exception_summary}",
                observed=[
                    f"exception_summary={json.dumps(exception_summary, separators=(',', ':'))}",
                    f"exception_type={json.dumps(exception_type, separators=(',', ':'))}",
                ],
                expected="handler returns one closed operation result",
                source=request.operation_ref,
            )
        ],
        repair="Inspect the recorded handler runtime failure before resuming.",
    )


def _validate_operation_inputs(request: AdapterRequest) -> JsonObject | None:
    allowed = _ALLOWED_PUBLIC_INPUTS.get(request.operation_ref, frozenset())
    if set(request.public_inputs) - allowed:
        return _protocol_failure(request)
    if request.operation_ref == "genesis::autostart.install":
        return _validate_autostart_install_inputs(request)
    if request.operation_ref not in _IDENTITY_INPUT_REFS:
        return None
    expected: JsonObject = {
        "solet_name": request.name,
        "clone_directory": str(request.target),
    }
    if request.operation_ref == "genesis::solet.run" and not _genesis_run_inputs_valid(request, expected):
        return _protocol_failure(request)
    if request.public_inputs != expected:
        return _protocol_failure(request)
    return None


def _genesis_run_inputs_valid(request: AdapterRequest, expected: JsonObject) -> bool:
    """Extend ``expected`` with genesis's profile, autostart and any implementation decisions."""
    expected["setup_profile"] = request.public_inputs.get("setup_profile")
    expected["autostart"] = request.public_inputs.get("autostart")
    if not isinstance(expected["setup_profile"], str) or not expected["setup_profile"]:
        return False
    if expected["autostart"] not in {"enabled", "disabled"}:
        return False
    for decision in IMPLEMENTATION_DECISIONS:
        value = request.public_inputs.get(decision)
        if decision in request.public_inputs and (not isinstance(value, str) or not value):
            return False
        if decision in request.public_inputs:
            expected[decision] = value
    return True


def _validate_autostart_install_inputs(request: AdapterRequest) -> JsonObject | None:
    setup_profile = request.public_inputs.get("setup_profile")
    autostart = request.public_inputs.get("autostart")
    if not isinstance(setup_profile, str) or not setup_profile:
        return _protocol_failure(request)
    if autostart not in {"enabled", "disabled"}:
        return _protocol_failure(request)
    return None


def _protocol_failure(request: AdapterRequest) -> JsonObject:
    return result(
        request,
        status="blocked",
        error_kind="adapter_protocol_error",
        retry_safe=False,
        exit_code=None,
        repair="Use only the exact flow-declared public inputs for this callable.",
    )


def _resolve_cutover_invoker() -> Callable[[TargetReconciliationRequest], dict[str, object]]:
    """Bind the cutover seam to the REFRESHED target's own plugin code.

    Imported lazily and inside the call, not at module scope: this adapter must
    stay importable on a host with no self-deployment plugin, and — the reason
    that actually matters — the whole point of the adjudicated shape is that
    the controller comes from the bytes the refresh just wrote, not from
    whatever this process imported when it started.

    An absent plugin is reported as a refusal, not a crash: "this target cannot
    perform a cutover" is a fact the manager needs on stdout.
    """

    def invoke(request: TargetReconciliationRequest) -> dict[str, object]:
        try:
            from macos_self_deployment_plugin.target_local_cutover import (  # noqa: PLC0415
                run_target_local_cutover,
            )
        except ImportError as exc:
            raise RuntimeError(
                "target has no macos_self_deployment_plugin; cutover is unavailable here",
            ) from exc
        return run_target_local_cutover(request)

    return invoke


def run_once(
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
    *,
    runtime: Runtime | None = None,
    local_source_resolver: LocalSourceResolver | None = None,
) -> int:
    """Read exactly one request and emit exactly one closed result."""

    raw_text = input_stream.read()
    if is_reconciliation_payload(raw_text):
        # A different flow with a different authority boundary, sharing only
        # this executable (design §3.2). It never enters the setup registries.
        output_stream.write(
            json.dumps(
                reconciliation_envelope(
                    raw_text,
                    home=(SystemRuntime() if runtime is None else runtime).home,
                    invoke_cutover=_resolve_cutover_invoker(),
                ),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        output_stream.write("\n")
        return 0
    try:
        request = AdapterRequest.from_json(raw_text)
    except AdapterInputError:
        error_stream.write("setup adapter rejected malformed input\n")
        return 2
    response = dispatch_request(
        request,
        SystemRuntime() if runtime is None else runtime,
        local_source_resolver=local_source_resolver,
    )
    output_stream.write(json.dumps(response, sort_keys=True, separators=(",", ":")))
    output_stream.write("\n")
    return 0


def main() -> int:
    """Run the target-local one-request adapter protocol."""

    return run_once(sys.stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["LocalSourceResolver", "dispatch_request", "main", "run_once"]
