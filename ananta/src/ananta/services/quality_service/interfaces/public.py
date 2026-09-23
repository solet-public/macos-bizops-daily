"""Quality Service Public API.

``@service_interface_process``-decorated surface for the three verbs of
:class:`QualityServiceInterface`. The bound provider
(``platform_dev_surface_plugin``) inherits the plain contract ABC and is
reachable through ``service_interface::quality_service::*`` keys. The matching
KB JSONs live at ``ananta/knowledge_base/processes/quality_service/*.json``
(dual-write per the D1 mandate).

Every verb is EDGE (returns structured data) and carries BOTH processor-
customization blocks on the decorator (structural half) — the companion JSON
supplies the prose half. ``requires_call_context=True`` logs the server-built
principal per gate run / repo read. ``run_gate`` / ``run_test`` are
``is_long_running`` — the full aggregate gate and smoke suite run for minutes.

Gate/smoke output is the platform's OWN toolchain output (ruff / radon / smoke
verdicts), not user data — every field is rated public (0.0) for exposure
scoring. The security boundary is the server-side NAME allowlist + repo-root
confinement, not per-field redaction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process

if TYPE_CHECKING:
    from ananta.core.services.call_context import CallContext

PROVIDER = "quality_service"

_GATE_PARAM = ParameterMetadata(
    description=(
        "Allowlisted gate name from the server-side registry (see list_gates). "
        "Directly-runnable: 'code_quality' (the whole-tree aggregate), "
        "'whole_tree_integration', 'service_interface_ast', 'sql_access', "
        "'wint2_driver_import', 'wint2_vault_key_declaration'. Unknown or "
        "coherence-only names (god_class/radon_cc/radon_mi — run via "
        "'code_quality') are rejected with a typed error."
    ),
    required=True,
    type=ParameterType.STRING,
)
_SMOKE_PARAM = ParameterMetadata(
    description=(
        "Optional repo-relative path of ONE smoke to run; it must be present in "
        "the gate register (quality_gates/gate_smokes.txt) or the verb rejects "
        "it. Omit to run the full gate-eligible smoke suite."
    ),
    required=False,
    type=ParameterType.STRING,
)

# Every returned field is the platform's own toolchain output — public (0.0).


def _list_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Enumerated gate registry + smoke register.",
        properties={
            "gates": ParameterMetadata(
                type=ParameterType.LIST,
                description="Per-gate rows: name, kind, description, timeout_seconds, directly_runnable, run_via.",
            ),
            "smokes": ParameterMetadata(
                type=ParameterType.LIST,
                description="Repo-relative smoke paths from the tracked register.",
            ),
            "smoke_count": ParameterMetadata(
                type=ParameterType.INTEGER, description="Number of registered smokes."
            ),
        },
    )


def _run_return_schema(target_field: str, target_desc: str) -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Execution verdict with bounded output.",
        properties={
            target_field: ParameterMetadata(
                type=ParameterType.STRING, description=target_desc
            ),
            "passed": ParameterMetadata(
                type=ParameterType.BOOLEAN, description="True iff exit_code == 0."
            ),
            "skipped": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "True iff exit_code == 77 (the reserved SKIP convention: a "
                    "disclosed, non-blocking dependency gap, distinct from a "
                    "genuine failure). Always False for a static gate (run_gate) "
                    "or the whole suite (run_test with no smoke), which never "
                    "themselves exit 77 -- meaningful for run_test against a "
                    "single named smoke."
                ),
            ),
            "exit_code": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Process exit code (77 = disclosed skip, 124 = timeout).",
            ),
            "timed_out": ParameterMetadata(
                type=ParameterType.BOOLEAN, description="True iff the hard timeout fired."
            ),
            "summary": ParameterMetadata(
                type=ParameterType.STRING, description="Last meaningful line of output."
            ),
            "output": ParameterMetadata(
                type=ParameterType.STRING, description="Captured stdout+stderr (tail, bounded)."
            ),
            "truncated": ParameterMetadata(
                type=ParameterType.BOOLEAN, description="True iff output was size-capped."
            ),
            "output_chars_total": ParameterMetadata(
                type=ParameterType.INTEGER, description="True total output length before capping."
            ),
        },
    )


def _hash_verify_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Per-file SHA-256 verification and editable-install shadowing warnings.",
        properties={
            "unit_id": ParameterMetadata(type=ParameterType.STRING, description="Declared work-unit id."),
            "root_path": ParameterMetadata(type=ParameterType.STRING, description="Absolute root inspected."),
            "files": ParameterMetadata(type=ParameterType.LIST, description="[{path, expected_sha256, actual_sha256|null, status}]."),
            "all_exact": ParameterMetadata(type=ParameterType.BOOLEAN, description="True iff every declared path matched."),
            "editable_install_warnings": ParameterMetadata(type=ParameterType.LIST, description="Pointer rows that resolve outside the inspected root."),
            "editable_install_shadowing_detected": ParameterMetadata(type=ParameterType.BOOLEAN, description="True iff a venv/pointer can resolve code outside the root."),
        },
    )


def _merge_predict_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="No-write append-only register merge prediction with exact candidate content.",
        properties={
            "base_ref": ParameterMetadata(type=ParameterType.STRING, description="Base revision read."),
            "current_master": ParameterMetadata(type=ParameterType.STRING, description="Current master revision read."),
            "lane_root_path": ParameterMetadata(type=ParameterType.STRING, description="Lane worktree inspected."),
            "register_path": ParameterMetadata(type=ParameterType.STRING, description="Tracked register path."),
            "master_drifted_since_base": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether current master differs from base."),
            "lane_change_is_append_only": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether lane content preserves the base as a prefix."),
            "lane_hunk": ParameterMetadata(type=ParameterType.STRING, description="Lane-only appended text, or empty when not append-only."),
            "lane_registrations": ParameterMetadata(type=ParameterType.LIST, description="Non-comment registrations in the lane hunk."),
            "duplicate_registrations_on_master": ParameterMetadata(type=ParameterType.LIST, description="Lane registrations already present on current master."),
            "missing_registrations_on_master": ParameterMetadata(type=ParameterType.LIST, description="Lane registrations absent from current master."),
            "merged_candidate_content": ParameterMetadata(type=ParameterType.STRING, description="git merge-file -p output; contains conflict markers when verdict is conflict."),
            "merge_verdict": ParameterMetadata(type=ParameterType.STRING, description="clean or conflict."),
            "git_merge_file_exit_code": ParameterMetadata(type=ParameterType.INTEGER, description="0 clean, 1 conflict."),
        },
    )


def _scope_gap_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Per-file static-gate scope classification.",
        properties={
            "paths": ParameterMetadata(type=ParameterType.LIST, description="Normalized paths inspected."),
            "in_scope": ParameterMetadata(type=ParameterType.LIST, description="Paths covered by per-file static gates."),
            "out_of_scope": ParameterMetadata(type=ParameterType.LIST, description="Paths requiring a manual static-analysis supplement."),
            "manual_static_analysis_needed": ParameterMetadata(type=ParameterType.BOOLEAN, description="True iff out_of_scope is non-empty."),
            "scope_source": ParameterMetadata(type=ParameterType.STRING, description="The exact quality-gate predicate consulted."),
        },
    )


class QualityServicePublicAPI(ABC):
    """AI-discoverable quality-gate + smoke execution surface.

    Access via: ``service_interface::quality_service::{verb}``
    """

    @service_interface_process(
        name="list_gates",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={},
        return_value_schema=_list_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="quality_gate_registry",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        requires_call_context=True,
    )
    @abstractmethod
    def list_gates(
        self, *, call_context: CallContext | None = None
    ) -> dict[str, Any]:
        """Read-only enumeration of the server-side gate registry + smoke register."""

    @service_interface_process(
        name="run_gate",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"gate": _GATE_PARAM},
        return_value_schema=_run_return_schema("gate", "The gate that was run."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="quality_gate_run_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def run_gate(
        self, gate: str, *, call_context: CallContext | None = None
    ) -> dict[str, Any]:
        """Run ONE gate by allowlisted name; report pass/fail + bounded output."""

    @service_interface_process(
        name="run_test",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"smoke": _SMOKE_PARAM},
        return_value_schema=_run_return_schema("target", "'suite' or the single smoke path run."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="quality_test_run_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def run_test(
        self, smoke: str | None = None, *, call_context: CallContext | None = None
    ) -> dict[str, Any]:
        """Run the gate-eligible smoke suite, or one registered smoke by path."""

    @service_interface_process(
        name="verify_hash_manifest",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "unit_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="Work-unit id for the report."),
            "root_path": ParameterMetadata(type=ParameterType.STRING, required=True, description="Existing absolute worktree/candidate-tree root."),
            "manifest": ParameterMetadata(type=ParameterType.DICT, required=True, description="Declared {repo_relative_path: lowercase_sha256} manifest."),
        },
        return_value_schema=_hash_verify_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(result_type="gc_hash_verify_result"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def verify_hash_manifest(
        self, unit_id: str, root_path: str, manifest: dict[str, str], *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Rehash a declared manifest under one root; never changes Git or files."""

    @service_interface_process(
        name="predict_gate_smokes_merge",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "base_ref": ParameterMetadata(type=ParameterType.STRING, required=True, description="Common base Git revision."),
            "lane_root_path": ParameterMetadata(type=ParameterType.STRING, required=True, description="Existing absolute lane worktree root."),
            "current_master": ParameterMetadata(type=ParameterType.STRING, required=True, description="Current master revision in that worktree."),
            "register_path": ParameterMetadata(type=ParameterType.STRING, required=False, default="quality_gates/gate_smokes.txt", description="Repo-relative append-only tracked-debt register."),
        },
        return_value_schema=_merge_predict_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(result_type="gc_gate_smokes_merge_prediction"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def predict_gate_smokes_merge(
        self, base_ref: str, lane_root_path: str, current_master: str,
        register_path: str = "quality_gates/gate_smokes.txt", *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Predict a no-write merge of one append-only tracked-debt register."""

    @service_interface_process(
        name="detect_scope_regex_gaps",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={
            "paths": ParameterMetadata(type=ParameterType.LIST, required=True, description="Repo-relative changed paths to classify."),
        },
        return_value_schema=_scope_gap_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(result_type="gc_scope_regex_gap_result"),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def detect_scope_regex_gaps(
        self, paths: list[str], *, call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Report paths not covered by the exact per-file static-gate predicate."""
