"""JSON codec and preview projection of the runtime plan (Step 5 design section 8).

``plan_to_json``/``plan_from_json`` are the exact inverse pair the executor journals the approved plan with
at approval time so a resume never re-probes to rediscover it; ``plan_preview_data`` is the closed
``update_preview`` payload.  Split out of ``update_runtime_plan`` for maintainability; no probing here.
"""

from __future__ import annotations

from typing import cast

from .models import DeclaredClosurePiece, JsonValue, LifecycleObservation, ManagedArtifactState, RuntimeOperationPlan, RuntimePlan
from .update_runtime_plan import STEP5_CAPABILITIES, STEP5_MANAGED_SUB_SURFACES, STEP5_NON_TOUCH_SURFACES, PlanContext

__all__ = ["plan_from_json", "plan_preview_data", "plan_to_json"]


def plan_to_json(plan: RuntimePlan) -> dict[str, JsonValue]:
    """The approved plan as closed JSON, journaled at approval so resume never re-probes to rediscover it."""
    lifecycle = plan.lifecycle
    return {
        "operation_id": plan.operation_id,
        "instance_id": plan.instance_id,
        "source_commit": plan.source_commit,
        "source_tree": plan.source_tree,
        "source_tag": plan.source_tag,
        "runtime_release_commit": plan.runtime_release_commit,
        "runtime_contract_digest": plan.runtime_contract_digest,
        "declared_closure": [[piece.distribution, piece.relative_path, piece.origin] for piece in plan.declared_closure],
        "operations": [
            {
                "operation_id": item.operation_id,
                "operation_ref": item.operation_ref,
                "runner": item.runner,
                "stage": item.stage,
                "mutation_class": item.mutation_class,
                "rollback_class": item.rollback_class,
                "retry_policy": item.retry_policy,
                "idempotency_key": item.idempotency_key,
                "applies": item.applies,
                "postcondition_now": item.postcondition_now,
                "planned_actions": list(item.planned_actions),
                "planned_targets": list(item.planned_targets),
                "public_inputs": item.public_inputs,
                "requires_confirmation": item.requires_confirmation,
                "backup_checkpoint_id": item.backup_checkpoint_id,
            }
            for item in plan.operations
        ],
        "managed_artifacts": [
            {
                "artifact_id": item.artifact_id,
                "kind": item.kind,
                "destination": item.destination,
                "state": item.state,
                "action": item.action,
                "stamped_digest": item.stamped_digest,
                "template_digest": item.template_digest,
                "expected_sha256": item.expected_sha256,
                "current_sha256": item.current_sha256,
                "conflict": item.conflict,
                "operation_id": item.operation_id,
                "adopt_diff": list(item.adopt_diff),
            }
            for item in plan.managed_artifacts
        ],
        "lifecycle": {
            "strategy": lifecycle.strategy,
            "launch_topology": lifecycle.launch_topology,
            "launchagent_label": lifecycle.launchagent_label,
            "plist_path": lifecycle.plist_path,
            "plist_expected_sha256": lifecycle.plist_expected_sha256,
            "current_release_id": lifecycle.current_release_id,
            "adapter_module_sha256": lifecycle.adapter_module_sha256,
            "adapter_module_replaced": lifecycle.adapter_module_replaced,
            "verification_modules": list(lifecycle.verification_modules),
            "readiness_budget_seconds": lifecycle.readiness_budget_seconds,
            "cutover_fingerprint": lifecycle.cutover_fingerprint,
            "zero_downtime_rollback": lifecycle.zero_downtime_rollback,
            "attestation": lifecycle.attestation,
            "cutover_probe_receipt": lifecycle.cutover_probe_receipt,
            "unproven_reason": lifecycle.unproven_reason,
            "pre_transition": lifecycle.pre_transition,
        },
        "forward_only_boundary": plan.forward_only_boundary,
        "ignore_sources_digest": plan.ignore_sources_digest,
        "target_process_executions": plan.target_process_executions,
        "blocked": [[subject, reason] for subject, reason in plan.blocked],
        "fingerprint": plan.fingerprint,
        "operator_selections": plan.operator_selections,
        "knowledge_removed_articles": [list(row) for row in plan.knowledge_removed_articles],
    }


def plan_from_json(raw: dict[str, JsonValue]) -> RuntimePlan:
    """Rebuild the approved plan from its journaled form (the inverse of :func:`plan_to_json`).

    A plan journaled before r65 carries no ``current_sha256`` or ``adopt_diff`` on its artifact rows: it approved no adoption and bound no
    digest, so both decode as absent and the hydration stage refuses any adoption that plan never showed (``hydrate_one``).
    """
    lifecycle = cast(dict[str, JsonValue], raw["lifecycle"])
    return RuntimePlan(
        cast(str, raw["operation_id"]),
        cast(str, raw["instance_id"]),
        cast(str, raw["source_commit"]),
        cast(str, raw["source_tree"]),
        cast(str | None, raw["source_tag"]),
        cast(str | None, raw["runtime_release_commit"]),
        cast(str | None, raw["runtime_contract_digest"]),
        tuple(DeclaredClosurePiece(cast(str, row[0]), cast(str, row[1]), cast(str, row[2])) for row in cast(list[list[JsonValue]], raw["declared_closure"])),
        tuple(
            RuntimeOperationPlan(
                cast(str, item["operation_id"]),
                cast(str, item["operation_ref"]),
                cast(str, item["runner"]),
                cast(str, item["stage"]),
                cast(str, item["mutation_class"]),
                cast(str, item["rollback_class"]),
                cast(str, item["retry_policy"]),
                cast(str, item["idempotency_key"]),
                cast(bool, item["applies"]),
                cast(str, item["postcondition_now"]),
                tuple(cast(list[str], item["planned_actions"])),
                tuple(cast(list[str], item["planned_targets"])),
                cast(dict[str, JsonValue], item["public_inputs"]),
                cast(bool, item["requires_confirmation"]),
                cast(str | None, item["backup_checkpoint_id"]),
            )
            for item in cast(list[dict[str, JsonValue]], raw["operations"])
        ),
        tuple(
            ManagedArtifactState(
                cast(str, item["artifact_id"]),
                cast(str, item["kind"]),
                cast(str, item["destination"]),
                cast(str, item["state"]),
                cast(str, item["action"]),
                cast(str | None, item["stamped_digest"]),
                cast(str, item["template_digest"]),
                cast(str | None, item["expected_sha256"]),
                cast(str | None, item.get("current_sha256")),
                cast(str | None, item["conflict"]),
                cast(str, item["operation_id"]),
                tuple(cast(list[str], item.get("adopt_diff", []))),
            )
            for item in cast(list[dict[str, JsonValue]], raw["managed_artifacts"])
        ),
        LifecycleObservation(
            cast(str, lifecycle["strategy"]),
            cast(str | None, lifecycle["launch_topology"]),
            cast(str, lifecycle["launchagent_label"]),
            cast(str, lifecycle["plist_path"]),
            cast(str | None, lifecycle["plist_expected_sha256"]),
            cast(str | None, lifecycle["current_release_id"]),
            cast(str | None, lifecycle["adapter_module_sha256"]),
            cast(bool, lifecycle["adapter_module_replaced"]),
            tuple(cast(list[str], lifecycle["verification_modules"])),
            cast(int, lifecycle["readiness_budget_seconds"]),
            cast(str | None, lifecycle["cutover_fingerprint"]),
            cast(bool, lifecycle["zero_downtime_rollback"]),
            cast(dict[str, JsonValue] | None, lifecycle["attestation"]),
            cast(dict[str, JsonValue] | None, lifecycle["cutover_probe_receipt"]),
            cast(str | None, lifecycle["unproven_reason"]),
            cast(dict[str, JsonValue] | None, lifecycle.get("pre_transition")),
        ),
        cast(str | None, raw["forward_only_boundary"]),
        cast(str | None, raw["ignore_sources_digest"]),
        cast(int, raw["target_process_executions"]),
        tuple((cast(str, row[0]), cast(str, row[1])) for row in cast(list[list[JsonValue]], raw["blocked"])),
        cast(str | None, raw["fingerprint"]),
        cast(dict[str, JsonValue], raw["operator_selections"]),
        tuple((cast(str, row[0]), cast(str, row[1]), cast(str, row[2])) for row in cast(list[list[JsonValue]], raw.get("knowledge_removed_articles", []))),
    )


def _blocked_rows(plan: RuntimePlan) -> list[JsonValue]:
    """Each blocked subject with its reason code and, for a probed operation, the adapter's own repair text (``None`` when it gave none)."""
    repairs = {item.operation_id: item.blocked_repair for item in plan.operations}
    return [{"subject": subject, "reason": reason, "repair": repairs.get(subject)} for subject, reason in plan.blocked]


def plan_preview_data(plan: RuntimePlan, context: PlanContext, journal_status: str) -> dict[str, JsonValue]:
    """The closed ``update_preview`` result data for a runtime preview (section 8.2)."""
    record = context.record
    lifecycle = plan.lifecycle
    return {
        "instance": {"instance_id": record.instance_id, "name": record.name, "canonical_target": record.target.canonical_path},
        "operation_id": plan.operation_id,
        "journal_status": journal_status,
        "source": {"commit": plan.source_commit, "tree": plan.source_tree, "tag": plan.source_tag},
        "runtime_baseline": {
            "runtime_release": plan.runtime_release_commit,
            "runtime_contract_digest": plan.runtime_contract_digest,
            "attestation": lifecycle.attestation,
        },
        "declared_closure": [{"distribution": piece.distribution, "relative_path": piece.relative_path, "origin": piece.origin} for piece in plan.declared_closure],
        "dependency_actions": [list(item.planned_actions) for item in plan.operations if item.stage == "dependencies"],
        "migrations_pre": _operation_rows(plan, "migrations_pre"),
        "migrations_post": _operation_rows(plan, "runtime_reconcile"),
        "managed_artifacts": [
            {
                "artifact_id": item.artifact_id,
                "destination": item.destination,
                "state": item.state,
                "action": item.action,
                "conflict": item.conflict,
                "expected_sha256": item.expected_sha256,
                "current_sha256": item.current_sha256,
                "adopt_diff": list(item.adopt_diff),
            }
            for item in plan.managed_artifacts
        ],
        "lifecycle": {
            "strategy": lifecycle.strategy,
            "launch_topology": lifecycle.launch_topology,
            "plist_expected_sha256": lifecycle.plist_expected_sha256,
            "cutover_probe_receipt": lifecycle.cutover_probe_receipt,
            "zero_downtime_rollback": lifecycle.zero_downtime_rollback,
            "unproven_reason": lifecycle.unproven_reason,
            "pre_transition": lifecycle.pre_transition,
        },
        "rollback_limit": {
            "classes": {item.operation_id: item.rollback_class for item in plan.operations},
            "forward_only_boundary": plan.forward_only_boundary,
            "router_previous_is_code_only": True,
        },
        "blocked": _blocked_rows(plan),
        "knowledge_removed_articles": [{"knowledge_base": kb, "path": path, "title": title} for kb, path, title in plan.knowledge_removed_articles],
        # Step 6 D4: disclosed Manager actions after the runtime stages; read-only or
        # Manager-state-only, so they are not part of the approval preimage.
        "manager_actions_after_runtime": ["manager.final_doctor", "manager.promote"],
        "preservation": {
            "target_byte_writes": 0,
            "manager_state_writes": 0,
            "target_process_executions": plan.target_process_executions,
            "non_touch_surfaces": list(STEP5_NON_TOUCH_SURFACES),
            "managed_sub_surfaces": list(STEP5_MANAGED_SUB_SURFACES),
            "capabilities": list(STEP5_CAPABILITIES),
        },
        "runtime_approval_fingerprint": plan.fingerprint,
    }


def _operation_rows(plan: RuntimePlan, stage: str) -> list[JsonValue]:
    return [
        {"operation_id": item.operation_id, "operation_ref": item.operation_ref, "applies": item.applies, "postcondition_now": item.postcondition_now, "planned_actions": list(item.planned_actions)}
        for item in plan.operations
        if item.stage == stage
    ]


