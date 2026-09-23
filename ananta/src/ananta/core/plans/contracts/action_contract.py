"""Action contract validation — post-decode step contract enforcement.

Pure validation logic for checking emitted actions against declared
step keys.  The inference plugin wraps these with error handling and
logging.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from ananta.core.plans.types import BoundSubStep, ComposedReference, ParsedPlan

logger = logging.getLogger(__name__)


def normalize_step_contract_process_keys(
    actions: list[dict[str, Any]],
    declared_keys: list[str],
) -> None:
    """Repair unambiguous provider_type mismatches before contract checks."""
    from ananta.core.prompts.decode.action_schema import (
        _normalize_unambiguous_provider_type_mismatch,
    )

    for action in actions:
        emitted_key = action.get("process_key", "")
        if not isinstance(emitted_key, str) or not emitted_key:
            continue
        corrected = _normalize_unambiguous_provider_type_mismatch(
            emitted_key, declared_keys,
        )
        if corrected == emitted_key:
            continue
        action["process_key"] = corrected
        process = action.get("process")
        if isinstance(process, dict):
            provider_type, provider, function_name = corrected.split("::", 2)
            process["provider_type"] = provider_type
            process["provider"] = provider
            process["function_name"] = function_name
        logger.info(
            "STEP_CONTRACT: Repaired provider_type mismatch %s -> %s",
            emitted_key, corrected,
        )


def check_no_undeclared(
    emitted: set[str], declared: set[str],
) -> str | None:
    """Rule 1: every emitted key must be declared. Returns error msg or None."""
    undeclared = emitted - declared
    if undeclared:
        return (
            f"Step contract violation: emitted actions {sorted(undeclared)} "
            f"are not declared in the current step. "
            f"Declared: {sorted(declared)}. "
            f"Emit each declared process exactly once."
        )
    return None


def check_all_visible_present(
    emitted_keys: list[str],
    visible_counts: Counter[str],
) -> str | None:
    """Rule 2: every declared key must appear enough times. Returns error msg or None."""
    emitted_counts = Counter(emitted_keys)
    missing: dict[str, str] = {}
    for key, required_count in visible_counts.items():
        actual = emitted_counts.get(key, 0)
        if actual < required_count:
            missing[key] = f"expected {required_count}, got {actual}"
    if missing:
        return (
            f"Step contract violation: emitted actions are missing "
            f"required purpose actions. Missing: {missing}."
        )
    return None


def check_no_excess_duplicates(
    emitted_keys: list[str],
    declared_counts: Counter[str],
) -> str | None:
    """Rule 3: each key must not appear more times than declared. Returns error msg or None."""
    emitted_counts = Counter(emitted_keys)
    excess = {
        k: f"declared {declared_counts.get(k, 0)}, emitted {v}"
        for k, v in emitted_counts.items()
        if v > declared_counts.get(k, 0)
    }
    if excess:
        return f"Step contract violation: excess duplicate actions {excess}."
    return None


def reorder_actions(
    actions: list[dict[str, Any]],
    declared_order: list[str],
) -> None:
    """Reorder actions in-place to match the declared step order."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for a in actions:
        by_key.setdefault(a.get("process_key", ""), []).append(a)
    reordered: list[dict[str, Any]] = []
    for k in declared_order:
        bucket = by_key.get(k, [])
        if bucket:
            reordered.append(bucket.pop(0))
    actions[:] = reordered


def resolve_single_composed_source(
    ref: ComposedReference,
    step_num: int,
    wbs_parsed: ParsedPlan,
) -> str | None:
    """Resolve one source step for a ``Composed:`` reference."""
    source_step = wbs_parsed.step_by_number(step_num)
    if source_step is None:
        return None
    for sub in source_step.bound_sub_steps:
        if sub.arguments and ref.source_arg in sub.arguments:
            return str(sub.arguments[ref.source_arg]) + ref.suffix
    return None


def resolve_composed_references(
    process_key: str,
    submitted_args: dict[str, Any],
    references: tuple[ComposedReference, ...],
    wbs_parsed: ParsedPlan,
) -> None:
    """Resolve ``Composed:`` cross-step references and inject into args.

    Output slots get overwritten afterward by work product injection.
    Input slots get resolved from the register. Bound args must flow
    through so WBS-authored names are available for register matching.
    """
    for ref in references:
        values = [
            v for step_num in ref.source_steps
            if (v := resolve_single_composed_source(
                ref, step_num, wbs_parsed,
            )) is not None
        ]
        if not values:
            continue
        composed_value: str | list[str] = (
            values[0] if len(ref.source_steps) == 1 else values
        )
        existing = submitted_args.get(ref.target_arg)
        if existing != composed_value:
            logger.info(
                "COMPOSED_RESOLVED: %s.%s = %r (was %r)",
                process_key, ref.target_arg, composed_value, existing,
            )
        submitted_args[ref.target_arg] = composed_value


def _override_bound_arguments(
    process_key: str,
    submitted_args: dict[str, Any],
    bound_arguments: dict[str, Any],
) -> None:
    """Override submitted argument values with WBS-declared bound values.

    Output slots get overwritten afterward by work product injection
    (deterministic stems). Input slots get resolved from the register.
    Bound args must flow through so WBS-authored names are available
    for display_name capture and register matching.
    """
    for key, bound_value in bound_arguments.items():
        submitted_value = submitted_args.get(key)
        if submitted_value != bound_value:
            logger.warning(
                "BOUND_ARG_ENFORCED: %s.%s — bound=%r overrides submitted=%r",
                process_key, key, bound_value, submitted_value,
            )
            submitted_args[key] = bound_value


def _apply_bound_step_to_action(
    action: dict[str, Any],
    bound: BoundSubStep,
    wbs_parsed: ParsedPlan | None,
) -> None:
    """Apply a single BoundSubStep's constraints to an action."""
    submitted_args = action.get("arguments", {})
    if not isinstance(submitted_args, dict):
        return
    process_key = action.get("process_key", "")
    if bound.arguments is not None:
        _override_bound_arguments(process_key, submitted_args, bound.arguments)
    if bound.composed_references and wbs_parsed is not None:
        resolve_composed_references(
            process_key, submitted_args, bound.composed_references, wbs_parsed,
        )


def enforce_bound_argument_values(
    actions: list[dict[str, Any]],
    bound_with_args: list[BoundSubStep],
    wbs_parsed: ParsedPlan | None,
    *,
    delivery_attachment: str | None = None,
    delivery_session_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Enforce WBS bound argument values on submitted actions.

    Returns updated (delivery_attachment, delivery_session_id) in case
    delivery enforcement was triggered.
    """
    buckets: dict[str, list[BoundSubStep]] = {}
    for bs in bound_with_args:
        buckets.setdefault(bs.process_key, []).append(bs)

    for action in actions:
        process_key = action.get("process_key", "")
        bucket = buckets.get(process_key)
        if not bucket:
            if "post_message" in process_key and delivery_attachment:
                _enforce_delivery_on_action(
                    action, delivery_attachment, delivery_session_id,
                )
            continue
        _apply_bound_step_to_action(action, bucket.pop(0), wbs_parsed)

    return delivery_attachment, delivery_session_id


def validate_step_contract(
    actions: list[dict[str, Any]],
    visible_keys: list[str],
) -> None:
    """Validate emitted actions against declared step keys.

    Orchestrates all four contract rules:
    1. No undeclared actions
    2. All visible keys present
    3. No excess duplicates
    4. Correct ordering (reorders if needed)

    Raises:
        RuntimeError: On any contract violation.
    """
    normalize_step_contract_process_keys(actions, visible_keys)

    visible_set = set(visible_keys)
    visible_counts = Counter(visible_keys)
    emitted_keys = [a.get("process_key", "") for a in actions]
    emitted_set = set(emitted_keys)

    error = check_no_undeclared(emitted_set, visible_set)
    if error:
        raise RuntimeError(error)
    error = check_all_visible_present(emitted_keys, visible_counts)
    if error:
        raise RuntimeError(error)
    error = check_no_excess_duplicates(emitted_keys, visible_counts)
    if error:
        raise RuntimeError(error)

    if emitted_keys != list(visible_keys):
        logger.warning(
            "STEP_CONTRACT: Wrong action order — emitted %s, expected %s (reordering)",
            emitted_keys, visible_keys,
        )
        reorder_actions(actions, visible_keys)

    logger.info(
        "STEP_CONTRACT: Satisfied — emitted %s, expected %s",
        emitted_keys, list(visible_keys),
    )


def _enforce_delivery_on_action(
    action: dict[str, Any],
    final_filename: str,
    session_id: str | None,
) -> None:
    """Enforce delivery attachment on a post_message action."""
    submitted_args = action.get("arguments", {})
    if not isinstance(submitted_args, dict):
        return
    expected = [final_filename]
    submitted_attachments = submitted_args.get("attachments", [])
    if submitted_attachments != expected:
        logger.warning(
            "DELIVERY_ATTACHMENT_ENFORCED: overriding attachments %r → %r",
            submitted_attachments, expected,
        )
        submitted_args["attachments"] = expected
    if session_id and submitted_args.get("session_id") != session_id:
        logger.warning(
            "DELIVERY_SESSION_ENFORCED: overriding session_id %r → %r",
            submitted_args.get("session_id"), session_id,
        )
        submitted_args["session_id"] = session_id
