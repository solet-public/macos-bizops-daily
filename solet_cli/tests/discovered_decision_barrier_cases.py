"""Terminal and stale-control cases for the decision-barrier fixture."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import solet_manager.create_execution as create_execution_module
from discovered_decision_barrier_scenarios import (
    _GOLDEN_APPLY_TRACE,
    _GOLDEN_PLANNED_ORDER,
    _PERSISTENT_KEYS,
    JsonObject,
    PersistentArtifactAdapter,
    _advance_to_models,
    _artifact_census,
    _canonical_request,
    _check,
    _closure_matrix,
    _decision_error_ids,
    _expected_planned_actions,
    _has_prohibited_effect,
    _initialize_fixture_state,
    _persistent_slice,
    _refused_arm,
    _set_invoke_adapter,
    _stable_result,
)
from discovered_decision_projection_scenarios import MODEL_BASE_URL, MODEL_SELECTIONS
from discovered_decision_support import _prepare
from solet_manager.config import CreateConfig
from solet_manager.create import CreateManager
from solet_manager.errors import StateConflictError
from solet_manager.models import CommandResult


def _terminal_steps(
    manager: CreateManager,
    config: CreateConfig,
    selections: dict[str, str],
) -> tuple[list[list[str]], list[JsonObject], CommandResult]:
    frontiers: list[list[str]] = []
    planned_actions: list[JsonObject] = []
    for _attempt in range(8):
        preview = manager.preview(config, decision_selections=selections)
        frontier = preview.data.get("frontier")
        frontiers.append(list(frontier) if isinstance(frontier, list) else [])
        raw_actions = preview.data.get("planned_actions")
        if isinstance(raw_actions, list):
            planned_actions.extend(
                cast(list[JsonObject], json.loads(json.dumps(raw_actions)))
            )
        result = manager.create(
            config,
            approved_fingerprint=str(preview.data["approval_fingerprint"]),
            decision_selections=selections,
        )
        if result.status == "verified":
            return frontiers, planned_actions, result
    raise AssertionError("terminal fixture did not reach verified")


def _all_terminal_artifacts_activated(after: JsonObject) -> bool:
    artifacts = cast(JsonObject, after["artifacts"])
    every_artifact_exists = all(
        cast(JsonObject, artifacts[key]).get("exists") is True
        for key in _PERSISTENT_KEYS
    )
    return all(
        (
            every_artifact_exists,
            after["shell_managed_block_count"] == 2,
            len(cast(list[object], after["registry_instances"])) == 1,
        )
    )


def _terminal_arm(root: Path, selections: dict[str, str]) -> JsonObject:
    fixture_root = root / "barrier-terminal-fixture"
    _initialize_fixture_state(fixture_root)
    paths, config, manager, _transaction = _prepare(root, name="barrier-terminal")
    trace: list[JsonObject] = []
    adapter = PersistentArtifactAdapter(fixture_root, event_trace=trace)
    _set_invoke_adapter(adapter)
    before = _artifact_census(fixture_root, paths, config.name)
    frontiers, planned_actions, result = _terminal_steps(manager, config, selections)
    completion_requests = [request for request in adapter.requests if request.probe_purpose == "completion" and request.operation_ref.startswith("setup::lm_studio.")]
    _check(len(completion_requests) == 8 and all(request.public_inputs.get("lm_studio_base_url") == MODEL_BASE_URL for request in completion_requests), "all eight terminal LM Studio probes receive the retained reviewed URL through the real manager runner")
    after = _artifact_census(fixture_root, paths, config.name)
    apply_requests = [request for request in adapter.requests if request.phase == "apply"]
    canonical_trace = [_canonical_request(request) for request in apply_requests]
    expected_actions = _expected_planned_actions(_GOLDEN_PLANNED_ORDER)
    return {
        "fixture": "barrier-terminal",
        "frontiers": frontiers,
        "result": _stable_result(result),
        "apply_trace": canonical_trace,
        "golden_apply_trace": list(_GOLDEN_APPLY_TRACE),
        "planned_actions": planned_actions,
        "golden_planned_actions": expected_actions,
        "apply_trace_exact": canonical_trace == list(_GOLDEN_APPLY_TRACE),
        "planned_actions_exact": planned_actions == expected_actions,
        "before": before,
        "after": after,
        "all_artifacts_activated": _all_terminal_artifacts_activated(after),
        "protected_unchanged": before["protected"] == after["protected"],
        "trace": trace,
    }


def _no_model_advance(
    _manager: CreateManager,
    _config: CreateConfig,
    _selections: dict[str, str],
) -> None:
    return None


def _changed_stale_inputs(
    root: Path,
    config: CreateConfig,
    selections: dict[str, str],
    adapter: PersistentArtifactAdapter,
    variant: str,
) -> tuple[CreateConfig, dict[str, str]]:
    def no_change() -> None:
        return None

    changed_selections = {**selections, "embedding_model": "embedding_model.alternate"}
    variants: dict[str, tuple[CreateConfig, dict[str, str], Callable[[], None]]] = {
        "closure": (config, selections, lambda: setattr(adapter, "empty_decision", "embedding_model")),
        "qualification": (
            config,
            selections,
            lambda: setattr(adapter, "fail_qualification", "embedding_model"),
        ),
        "provider": (config, selections, lambda: setattr(adapter, "metadata_revision", "m2")),
        "candidate_set": (config, selections, lambda: setattr(adapter, "label_suffix", "v2")),
        "answer": (config, changed_selections, no_change),
        "target": (replace(config, target=(root / "changed-target").resolve()), selections, no_change),
        "action_state": (config, selections, lambda: setattr(adapter, "action_revision", "changed")),
    }
    try:
        create_config, create_selections, apply_change = variants[variant]
    except KeyError as exc:
        raise AssertionError(f"unknown stale variant {variant}") from exc
    apply_change()
    return create_config, create_selections


def _stale_failure(
    manager: CreateManager,
    config: CreateConfig,
    fingerprint: str,
    selections: dict[str, str],
) -> str:
    try:
        result = manager.create(
            config,
            approved_fingerprint=fingerprint,
            decision_selections=selections,
        )
    except StateConflictError as exc:
        return f"state_conflict:{exc}"
    return result.error_kind or result.status


def _run_stale_case(
    root: Path,
    selections: dict[str, str],
    variant: str,
    *,
    name_prefix: str,
    advance: Callable[[CreateManager, CreateConfig, dict[str, str]], None],
) -> tuple[str, bool]:
    fixture_root = root / f"{name_prefix}-{variant}-fixture"
    _initialize_fixture_state(fixture_root)
    paths, config, manager, _transaction = _prepare(root, name=f"{name_prefix}-{variant}")
    trace: list[JsonObject] = []
    adapter = PersistentArtifactAdapter(fixture_root, event_trace=trace)
    _set_invoke_adapter(adapter)
    advance(manager, config, selections)
    preview = manager.preview(config, decision_selections=selections)
    before = _artifact_census(fixture_root, paths, config.name)
    trace_start = len(trace)
    create_config, create_selections = _changed_stale_inputs(
        root,
        config,
        selections,
        adapter,
        variant,
    )
    failure = _stale_failure(
        manager,
        create_config,
        str(preview.data["approval_fingerprint"]),
        create_selections,
    )
    after = _artifact_census(fixture_root, paths, config.name)
    unchanged = _persistent_slice(before) == _persistent_slice(after)
    return failure, not _has_prohibited_effect(trace[trace_start:]) and unchanged


def _stale_case(root: Path, selections: dict[str, str], variant: str) -> JsonObject:
    failure, zero_effect = _run_stale_case(
        root,
        selections,
        variant,
        name_prefix="stale",
        advance=_no_model_advance,
    )
    return {"failure": failure, "zero_effect": zero_effect}


def _model_stale_case(root: Path, selections: dict[str, str], variant: str) -> JsonObject:
    failure, zero_effect = _run_stale_case(
        root,
        selections,
        variant,
        name_prefix="model-stale",
        advance=_advance_to_models,
    )
    return {"failure": failure, "zero_effect_after_models": zero_effect}


def _stale_controls(root: Path, selections: dict[str, str]) -> JsonObject:
    static_variants = {"target", "action_state"}
    variants = (
        "closure",
        "qualification",
        "provider",
        "candidate_set",
        "answer",
        "target",
        "action_state",
    )
    return {
        variant: (
            _stale_case(root, selections, variant)
            if variant in static_variants
            else _model_stale_case(root, selections, variant)
        )
        for variant in variants
    }


def _post_lock_control(root: Path, selections: dict[str, str]) -> JsonObject:
    fixture_root = root / "post-lock-fixture"
    _initialize_fixture_state(fixture_root)
    paths, config, manager, _transaction = _prepare(root, name="post-lock")
    trace: list[JsonObject] = []
    adapter = PersistentArtifactAdapter(fixture_root, event_trace=trace)
    _set_invoke_adapter(adapter)
    _advance_to_models(manager, config, selections)
    preview = manager.preview(config, decision_selections=selections)
    before = _artifact_census(fixture_root, paths, config.name)
    trace_start = len(trace)
    original_lock = create_execution_module.instance_lock

    @contextmanager
    def controlled_lock(path: Path, *, create: bool = False) -> Iterator[None]:
        del path, create
        trace.append({"event": "instance_lock_acquired"})
        adapter.empty_decision = "embedding_model"
        yield
        trace.append({"event": "instance_lock_released"})

    try:
        create_execution_module.instance_lock = controlled_lock
        result = manager.create(
            config,
            approved_fingerprint=str(preview.data["approval_fingerprint"]),
            decision_selections=selections,
        )
    finally:
        create_execution_module.instance_lock = original_lock
    after = _artifact_census(fixture_root, paths, config.name)
    lock_index = next(
        index
        for index, item in enumerate(trace)
        if item.get("event") == "instance_lock_acquired"
    )
    closure_index = next(
        (
            index
            for index, item in enumerate(trace)
            if item.get("event") == "decision_closure_nonterminal"
        ),
        -1,
    )
    return {
        "status": result.status,
        "error_kind": result.error_kind,
        "decision_ids": sorted(_decision_error_ids(result)),
        "lock_before_assertion": closure_index >= 0 and lock_index < closure_index,
        "execute_locked_create_not_entered": not any(
            item.get("event") == "apply_dispatch" for item in trace[trace_start:]
        ),
        "zero_effect_after_models": (
            not _has_prohibited_effect(trace[trace_start:])
            and _persistent_slice(before) == _persistent_slice(after)
        ),
        "trace": trace,
    }


def _case_is_refusal(case: JsonObject, decision_id: str) -> bool:
    return all(
        (
            case["frontier"] == ["models"],
            case["status"] == "awaiting_user",
            case["decision_ids"] == [decision_id],
            case["zero_effect_after_models"] is True,
        )
    )


def _assert_refusal_cases(
    matrix: JsonObject,
    keys: tuple[str, ...],
    decision_id: str,
    label: str,
) -> None:
    for key in keys:
        case = cast(JsonObject, matrix[key])
        _check(_case_is_refusal(case, decision_id), f"{label} {key} fails closed at models")


def _assert_matrix(matrix: JsonObject) -> None:
    _assert_refusal_cases(
        matrix,
        (
            "platform_expectation_missing",
            "empty_embedding_candidates",
            "failed_embedding_qualification",
            "selected_outside_permitted_set",
        ),
        "embedding_model",
        "model barrier",
    )
    _assert_refusal_cases(
        matrix,
        ("conditional_inference_empty",),
        "inference_model",
        "conditional model barrier",
    )
    unqualified_inference = cast(JsonObject, matrix["conditional_inference_unqualified"])
    _check(
        all(
            (
                unqualified_inference["status"] == "preview_ready",
                unqualified_inference["decision_ids"] == [],
                unqualified_inference["inference_probed"] is True,
                unqualified_inference["zero_effect_after_models"] is True,
            )
        ),
        "served inference stays eligible when qualification is unavailable",
    )
    inverse = cast(JsonObject, matrix["inverse_inference_none"])
    _check(
        all(
            (
                inverse["status"] == "preview_ready",
                inverse["inference_probed"] is False,
                inverse["zero_effect_after_models"] is True,
            )
        ),
        "inactive inference_model does not cause a model-stage refusal",
    )


def _assert_refused_arm(refused: JsonObject) -> None:
    _check(
        all(
            (
                refused["preview_frontier"] == ["models"],
                refused["zero_prohibited_effects_after_models"] is True,
            )
        ),
        "model barrier refuses at models before a later persistent effect",
    )
    _check(
        all(
            (
                refused["same_refusal"] is True,
                refused["decision_named"] is True,
                refused["journal_byte_identical"] is True,
                refused["model_activation_not_started"] is True,
            )
        ),
        "model-stage refusal is stable, actionable, truthful, and resumable",
    )
    _check(
        refused["compensation_or_cleanup"] == [],
        "refusal performs zero compensation, cleanup, repair, or adoption",
    )


def _assert_terminal_arm(terminal: JsonObject) -> None:
    _check(
        all(
            (
                terminal["apply_trace_exact"] is True,
                terminal["planned_actions_exact"] is True,
            )
        ),
        "terminal arm preserves exact golden activation IDs, arguments, and actions",
    )
    _check(
        all(
            (
                terminal["all_artifacts_activated"] is True,
                terminal["protected_unchanged"] is True,
            )
        ),
        "terminal arm activates the full artifact set without protected-state drift",
    )


def _assert_stale_controls(stale: JsonObject) -> None:
    all_cases_clean = all(
        cast(JsonObject, case).get(
            "zero_effect_after_models",
            cast(JsonObject, case).get("zero_effect"),
        )
        is True
        for case in stale.values()
    )
    _check(
        all_cases_clean,
        "all stale model and static variants refuse with zero prohibited effect",
    )


def _assert_post_lock(post_lock: JsonObject) -> None:
    _check(
        all(
            (
                post_lock["status"] == "awaiting_user",
                post_lock["error_kind"] == "decisions_required",
                post_lock["decision_ids"] == ["embedding_model"],
                post_lock["lock_before_assertion"] is True,
                post_lock["execute_locked_create_not_entered"] is True,
                post_lock["zero_effect_after_models"] is True,
            )
        ),
        "locked preview reasserts the model barrier immediately before dispatch",
    )


def run_barrier_scenario(root: Path) -> JsonObject:
    """Run the exact frozen two-arm fixture and every adversarial control."""

    selections = dict(MODEL_SELECTIONS)
    refused = _refused_arm(root, selections)
    terminal = _terminal_arm(root, selections)
    matrix = _closure_matrix(root, selections)
    stale = _stale_controls(root, selections)
    post_lock = _post_lock_control(root, selections)
    report: JsonObject = {
        "fixture_schema": "preactivation-barrier-v3",
        "nonterminal": refused,
        "terminal": terminal,
        "closure_matrix": matrix,
        "stale_controls": stale,
        "post_lock_control": post_lock,
    }
    print("BARRIER_EVIDENCE_JSON=" + json.dumps(report, sort_keys=True))
    _assert_refused_arm(refused)
    _assert_terminal_arm(terminal)
    _assert_matrix(matrix)
    _assert_stale_controls(stale)
    _assert_post_lock(post_lock)
    return report
