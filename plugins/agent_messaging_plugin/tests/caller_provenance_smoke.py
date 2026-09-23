#!/usr/bin/env python3
"""Hermetic contract smoke for ``resolve_caller_provenance``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.orchestration.execution_context import (  # noqa: E402
    ExecutionContext,
    PlaceholderResolutionError,
)
from ananta.core.services.call_context import CallContext  # noqa: E402

import agent_messaging_plugin.caller_provenance as provenance  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"PASS {label}")
    else:
        _failed.append(label)
        print(f"FAIL {label}")


def _resolve(
    state: dict[str, Any], *, has_role_claim: bool = True
) -> provenance.CallerProvenance:
    original_managed = provenance.read_managed_session
    original_claim = provenance.read_session_role_claim
    provenance.read_managed_session = lambda _service, instance: {  # type: ignore[assignment]
        "id": "mgs-real-row", "agent_instance_id": instance, "agent_session_id": "ases-one"
    }
    provenance.read_session_role_claim = (  # type: ignore[assignment]
        (lambda _service, _session: {"id": "src-one"})
        if has_role_claim
        else (lambda _service, _session: None)
    )
    try:
        return provenance.resolve_caller_provenance(object(), state)  # type: ignore[arg-type]
    finally:
        provenance.read_managed_session = original_managed  # type: ignore[assignment]
        provenance.read_session_role_claim = original_claim  # type: ignore[assignment]


def test_refuses_without_server_context() -> None:
    try:
        _resolve({"inference_vertex_session_id": "agi-one"})
    except provenance.CallerProvenanceError as exc:
        _check(exc.code == "caller_provenance_unavailable", "no CallContext refuses")
    else:
        _check(False, "no CallContext refuses")


def test_uses_server_identity_and_actual_row_id() -> None:
    result = _resolve({
        "call_context": CallContext.for_operator(),
        "inference_vertex_session_id": "agi-one",
    })
    _check(result.managed_session_id == "mgs-real-row", "uses measured ledger row id")
    _check(result.agent_session_id == "ases-one", "returns managed-session agent id")
    _check(result.session_role_claim_id == "src-one", "includes resolvable role claim")
    _check(result.directed_by == "operator", "reuses shared directed_by renderer")


def test_normalizes_non_grammar_directed_by() -> None:
    result = _resolve({
        "call_context": CallContext.for_external("bad+principal"),
        "caller_attribution_instance_id": "agi-attributed",
    })
    _check(result.directed_by.startswith("sha256:"), "hashes non-grammar renderer output")
    _check(result.directed_by_encoding == "sha256(format_directed_by)", "reports mapping")


def test_registered_edge_definition() -> None:
    definition = AgentMessagingPlugin().get_edge_process_definitions()[
        "resolve_caller_provenance"
    ]
    _check(
        definition.name == "resolve_caller_provenance",
        "same-name EDGE definition is registered",
    )
    result_customizations = definition.result_processor_template_customizations
    _check(
        result_customizations is not None
        and result_customizations.result_type == "caller_provenance",
        "registered EDGE result type is caller_provenance",
    )


def test_store_result_honors_required_metadata() -> None:
    optional_context = ExecutionContext("optional-result-field")
    optional_context.store_result(
        "step",
        {"present": "value"},
        {
            "properties": {
                "present": {"type": "string", "required": True},
                "optional": {"type": "string", "required": False},
            }
        },
    )
    _check(
        optional_context.resolve_placeholder("<<PRESENT>>") == "value",
        "missing optional schema property does not raise",
    )

    required_context = ExecutionContext("required-result-field")
    try:
        required_context.store_result(
            "step",
            {"present": "value"},
            {
                "properties": {
                    "present": {"type": "string", "required": True},
                    "required": {"type": "string", "required": True},
                }
            },
        )
    except PlaceholderResolutionError as exc:
        _check(exc.placeholder == "<<REQUIRED>>", "missing required schema property raises")
    else:
        _check(False, "missing required schema property raises")


def test_roleless_provenance_result_stores_end_to_end() -> None:
    original_managed = provenance.read_managed_session
    original_claim = provenance.read_session_role_claim
    provenance.read_managed_session = lambda _service, instance: {  # type: ignore[assignment]
        "id": "mgs-real-row", "agent_instance_id": instance, "agent_session_id": "ases-one"
    }
    provenance.read_session_role_claim = lambda _service, _session: None  # type: ignore[assignment]
    plugin = AgentMessagingPlugin()
    plugin.orchestrator_ref = type(
        "StateServiceOrchestrator",
        (),
        {"get_service": lambda _self, _name: object()},
    )()
    try:
        action_result = plugin.resolve_caller_provenance(
            {},
            {
                "call_context": CallContext.for_operator(),
                "inference_vertex_session_id": "agi-one",
            },
        )
    finally:
        provenance.read_managed_session = original_managed  # type: ignore[assignment]
        provenance.read_session_role_claim = original_claim  # type: ignore[assignment]

    result = action_result["data"]
    if not isinstance(result, dict):
        _check(False, "roleless resolve_caller_provenance returns data payload")
        return
    schema = AgentMessagingPlugin.resolve_caller_provenance._platform_process_metadata.return_value_schema
    context = ExecutionContext("roleless-caller-provenance")
    context.store_result("resolve", result, schema.to_dict())
    _check(
        context.resolve_placeholder("<<MANAGED_SESSION_ID>>") == "mgs-real-row",
        "roleless resolve_caller_provenance result stores without placeholder error",
    )


def main() -> int:
    test_refuses_without_server_context()
    test_uses_server_identity_and_actual_row_id()
    test_normalizes_non_grammar_directed_by()
    test_registered_edge_definition()
    test_store_result_honors_required_metadata()
    test_roleless_provenance_result_stores_end_to_end()
    print(f"caller_provenance_smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
