"""Step-schema narrowing for plan-driven decode contracts.

Public entry point ``build_step_schema`` replaces the plugin's
``_adjust_output_schema_from_ctx`` callback.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from ananta.core.plans import parse as parse_plan
from ananta.core.plans.types import COMPANION_SUFFIXES, ParsedPlan, ParsedPlanStep
from ananta.core.prompts.decode.action_schema import (
    _ALL_ARG_PROPERTIES,
    _FUNCTION_ARG_PROPERTIES,
    _FUNCTION_REQUIRED_ARGS,
    _MAX_TOTAL_ARG_PROPERTIES_IN_OUTPUT_SCHEMA,
    _action_schema,
    _narrow_arg_schema,
    _narrow_arg_schema_with_all_required,
    _parse_process_keys,
    _step_narrowed_schema,
)
from ananta.core.prompts.decode.action_schema import (
    extract_invocation_arg_properties as extract_invocation_arg_properties,  # re-export
)
from ananta.core.prompts.plan_state import PlanState

logger = logging.getLogger(__name__)

# (step_number, resolved_keys, has_extension, min_actions_override)
type StepData = tuple[int, list[str], bool, int | None]

_UPSERT_PLAN_SUFFIX = "::upsert_plan"
_AWAIT_USER_RE = re.compile(r"Await USER message", re.IGNORECASE)
_INVESTIGATE_STEP1_KEYS: list[str] = [
    "service_interface::thinking_service::upsert_plan",
    "service_interface::memory_service::recall",
]


class ProcessArgLookup(Protocol):
    """Narrow protocol for process argument property lookups."""

    def get_arg_properties(self, process_key: str) -> dict[str, dict[str, object]]: ...
    def get_required_properties(self, process_key: str) -> set[str]: ...
    def get_declared_properties(self, process_key: str) -> set[str]: ...


class PlanAdvancer(Protocol):
    """Narrow protocol for plan step advancement (session-scoped, JOS-02)."""

    def advance_current_plan_step(
        self, *, session_id: str,
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class StepSchemaResult:
    """Output of schema narrowing for a single plan step."""

    output_schema: dict[str, Any] | None
    current_step_process_keys: list[str]
    model_visible_process_keys: list[str]


def build_step_schema(
    plan_state: PlanState,
    process_arg_lookup: ProcessArgLookup,
    *,
    base_schema: dict[str, Any] | None = None,
    tool_observation: str | None = None,
    raw_observation_dict: dict[str, Any] | None = None,
    io_namespace: str | None = None,
    has_focused_plan: bool = False,
    is_delivery_confirmation: bool = False,
    plan_advancer: PlanAdvancer | None = None,
    session_id: str = "",
) -> StepSchemaResult:
    """Build the step-narrowed output schema for a plan-driven vertex.

    Only applies to action schemas (has ``"actions"`` in required).
    Returns the base schema unmodified when no narrowing applies.
    ``session_id`` keys any advancement the narrowing performs (JOS-02).
    """
    current_keys: list[str] = []
    visible_keys: list[str] = []

    if not base_schema:
        return StepSchemaResult(base_schema, current_keys, visible_keys)

    required = base_schema.get("required", [])
    if "actions" not in required:
        return StepSchemaResult(base_schema, current_keys, visible_keys)

    return _dispatch_schema_adjustment(
        plan_state=plan_state,
        process_arg_lookup=process_arg_lookup,
        base_schema=base_schema,
        tool_observation=tool_observation,
        raw_observation_dict=raw_observation_dict,
        io_namespace=io_namespace,
        has_focused_plan=has_focused_plan,
        is_delivery_confirmation=is_delivery_confirmation,
        plan_advancer=plan_advancer,
        session_id=session_id,
    )


def _dispatch_schema_adjustment(
    *,
    plan_state: PlanState,
    process_arg_lookup: ProcessArgLookup,
    base_schema: dict[str, Any],
    tool_observation: str | None,
    raw_observation_dict: dict[str, Any] | None,
    io_namespace: str | None,
    has_focused_plan: bool,
    is_delivery_confirmation: bool,
    plan_advancer: PlanAdvancer | None,
    session_id: str,
) -> StepSchemaResult:
    """Route to the correct schema builder based on vertex context."""
    if is_delivery_confirmation:
        if has_focused_plan:
            logger.info("OUTPUT_SCHEMA: Delivery conf with focused plan -- continuing")
        else:
            logger.info("OUTPUT_SCHEMA: Delivery confirmation -- enforcing empty actions")
            return StepSchemaResult(_build_delivery_confirmation_schema(), [], [])

    if not tool_observation:
        return _adjust_initial_turn_schema(
            plan_state, process_arg_lookup,
            io_namespace=io_namespace,
            plan_advancer=plan_advancer,
            session_id=session_id,
        )

    return _adjust_post_observation_schema(
        plan_state, process_arg_lookup,
        raw_observation_dict=raw_observation_dict,
        io_namespace=io_namespace,
    )


def _build_focused_step_schema(
    plan_state: PlanState,
    process_arg_lookup: ProcessArgLookup,
    step_data: StepData,
) -> StepSchemaResult:
    """Build the output schema for a focused plan step."""
    step_num, all_keys, is_extension, min_actions_override = step_data

    if is_extension:
        model_keys = all_keys
        label = "planning-extension"
    else:
        model_keys, label = _resolve_non_extension_keys(all_keys)

    min_items = (
        min_actions_override
        if min_actions_override is not None
        else len(model_keys)
    )
    arg_schema = _resolve_arg_schema(plan_state, process_arg_lookup, model_keys)

    if plan_state.focused_wbs_text:
        arg_schema = _merge_bound_keys_if_needed(plan_state, arg_schema)

    schema = _step_narrowed_schema(
        model_keys,
        min_items=min_items,
        arg_schema=arg_schema,
    )
    logger.info(
        "OUTPUT_SCHEMA: Step-narrowed %s schema (step %d, minItems=%d: %s)",
        label, step_num, min_items, ", ".join(model_keys),
    )
    return StepSchemaResult(schema, all_keys, model_keys)


def _resolve_non_extension_keys(
    all_keys: list[str],
) -> tuple[list[str], str]:
    """Select model-visible keys for non-extension steps."""
    model_keys = [
        k for k in all_keys
        if not k.endswith(_UPSERT_PLAN_SUFFIX)
    ]
    if not model_keys:
        return all_keys, "progress-only"
    non_companion = [
        k for k in model_keys
        if not any(k.endswith(s) for s in COMPANION_SUFFIXES)
    ]
    label = "progress-only" if non_companion else "checkpoint"
    return model_keys, label


def _merge_bound_keys_if_needed(
    plan_state: PlanState,
    arg_schema: dict[str, object] | None,
) -> dict[str, object] | None:
    """No-op: bound keys are injected post-inference and excluded from the schema.

    Formerly re-added stripped bound keys as ``{"type": "string"}``,
    which caused type mismatches (e.g. array-typed pitch_range forced
    to string) that triggered runaway model generation.
    """
    return arg_schema


def _resolve_arg_schema(
    plan_state: PlanState,
    process_arg_lookup: ProcessArgLookup,
    model_keys: list[str],
) -> dict[str, object] | None:
    """Resolve the argument schema for the step's output contract."""
    bound_arg_keys = _collect_bound_arg_keys(plan_state)
    arg_schema = _build_widened_arg_schema(
        process_arg_lookup, model_keys, bound_arg_keys=bound_arg_keys,
    )
    if arg_schema is not None:
        if plan_state.focused_wbs_text:
            from ananta.core.plans.work_product_runtime import (
                strip_owned_slots_from_arg_schema,
            )
            arg_schema = strip_owned_slots_from_arg_schema(arg_schema)
        return arg_schema

    if plan_state.is_completion_handoff:
        return _narrow_arg_schema_with_all_required(model_keys)

    _, _, function_names = _parse_process_keys(model_keys)
    has_unknown = any(
        fn not in _FUNCTION_ARG_PROPERTIES for fn in function_names
    )
    if has_unknown:
        return _narrow_from_registry(process_arg_lookup, model_keys)
    return None


def _narrow_from_registry(
    process_arg_lookup: ProcessArgLookup,
    model_keys: list[str],
) -> dict[str, object]:
    """Build a narrowed arg schema using dynamic registry lookups."""
    _, _, function_names = _parse_process_keys(model_keys)

    def _registry_props(fn: str) -> dict[str, dict[str, object]]:
        for key in model_keys:
            if key.endswith(f"::{fn}"):
                return process_arg_lookup.get_arg_properties(key)
        return {}

    def _registry_required(fn: str) -> set[str]:
        for key in model_keys:
            if key.endswith(f"::{fn}"):
                return process_arg_lookup.get_required_properties(key)
        return set()

    return _narrow_arg_schema(
        function_names,
        registry_lookup=_registry_props,
        registry_required_lookup=_registry_required,
    )


def _build_widened_arg_schema(
    process_arg_lookup: ProcessArgLookup,
    schema_keys: list[str],
    *,
    bound_arg_keys: set[str] | None = None,
) -> dict[str, object] | None:
    """Build an argument schema for steps with plugin processes.

    Returns ``None`` when no plugin processes are present.
    """
    plugin_keys = [
        k for k in schema_keys
        if k.startswith("plugin::") and not k.endswith("::post_message")
    ]
    if not plugin_keys:
        return None

    merged: dict[str, object] = {}
    _merge_plugin_arg_props(process_arg_lookup, merged, plugin_keys)
    _merge_builtin_arg_props(merged, schema_keys)

    required = _collect_widened_required_args(
        process_arg_lookup, schema_keys, plugin_keys, set(merged),
    )

    if bound_arg_keys:
        merged = _strip_bound_properties(
            merged, bound_arg_keys,
        )

    if len(merged) > _MAX_TOTAL_ARG_PROPERTIES_IN_OUTPUT_SCHEMA:
        merged = dict(
            list(merged.items())[:_MAX_TOTAL_ARG_PROPERTIES_IN_OUTPUT_SCHEMA]
        )

    schema: dict[str, object] = {
        "type": "object",
        "properties": merged,
        "additionalProperties": False,
    }
    required = [r for r in required if r in merged]
    if required:
        schema["required"] = required
    return schema


def _merge_plugin_arg_props(
    process_arg_lookup: ProcessArgLookup,
    merged: dict[str, object],
    plugin_keys: list[str],
) -> None:
    """Add plugin process argument properties from the registry."""
    for key in plugin_keys:
        for name, prop_def in process_arg_lookup.get_arg_properties(key).items():
            if name not in merged:
                merged[name] = prop_def


def _merge_builtin_arg_props(
    merged: dict[str, object],
    schema_keys: list[str],
) -> None:
    """Add builtin service process properties from the static map."""
    for key in schema_keys:
        if key.startswith("plugin::"):
            continue
        fn = key.rsplit("::", 1)[-1] if "::" in key else key
        props = _FUNCTION_ARG_PROPERTIES.get(fn)
        if not props:
            continue
        for name in props:
            if name not in merged and name in _ALL_ARG_PROPERTIES:
                merged[name] = _ALL_ARG_PROPERTIES[name]


def _collect_widened_required_args(
    process_arg_lookup: ProcessArgLookup,
    schema_keys: list[str],
    plugin_keys: list[str],
    available_properties: set[str],
) -> list[str]:
    """Collect required argument names (intersection across all processes)."""
    per_process: list[set[str]] = []

    for key in plugin_keys:
        per_process.append(process_arg_lookup.get_required_properties(key))

    for key in schema_keys:
        if key.startswith("plugin::"):
            continue
        fn = key.rsplit("::", 1)[-1] if "::" in key else key
        req = _FUNCTION_REQUIRED_ARGS.get(fn)
        per_process.append(set(req) if req else set())

    if not per_process:
        return []

    result = per_process[0]
    for s in per_process[1:]:
        result = result & s

    return sorted(result & available_properties)


def _strip_bound_properties(
    merged: dict[str, object],
    bound_arg_keys: set[str],
) -> dict[str, object]:
    """Remove WBS-bound properties from the schema.

    When the platform injects bound arguments automatically, they should
    not appear in the model's schema — otherwise the model emits its own
    values which may conflict with the injected ones.
    """
    return {
        name: prop
        for name, prop in merged.items()
        if name not in bound_arg_keys
    }


def _collect_bound_arg_keys(plan_state: PlanState) -> set[str]:
    """Collect argument keys already committed in the WBS."""
    wbs_step = _resolve_wbs_step_for_bound_keys(plan_state)
    if wbs_step is None:
        return set()
    keys: set[str] = set()
    for bs in wbs_step.bound_sub_steps:
        if bs.arguments is not None:
            keys.update(bs.arguments)
    return keys


def _get_current_step_data(
    plan_state: PlanState,
    io_namespace: str | None,
) -> StepData | None:
    """Return ``(step_number, resolved_keys, has_extension, min_actions)``."""
    plan_text = plan_state.focused_plan_text
    if not plan_text:
        logger.info("STEP_DATA: No focused plan text found")
        return None

    parsed = parse_plan(plan_text)
    current = _resolve_current(parsed)
    if current is None:
        logger.info("STEP_DATA: No incomplete step found in plan")
        return None

    raw_keys = list(current.process_keys)
    logger.info(
        "STEP_DATA: Active plan step %d found, %d inline keys: %s",
        current.number, len(raw_keys), raw_keys,
    )

    if not raw_keys:
        return None

    resolved = _resolve_step_process_keys(raw_keys, io_namespace)
    if not resolved:
        logger.info("STEP_DATA: All inline keys dropped after resolution")
        return None

    return (current.number, resolved, current.has_planning_extension, current.min_actions)


def _resolve_current(parsed: ParsedPlan) -> ParsedPlanStep | None:
    """Resolve the active or first executable step from a parsed plan."""
    current = parsed.current_step
    if current is None:
        first_num = parsed.first_executable_step_number
        if first_num is not None:
            current = parsed.step_by_number(first_num)
    return current


def _is_current_step_await_user(plan_state: PlanState) -> bool:
    """Check whether the current ``[>]`` step is an await-user step."""
    plan_text = plan_state.focused_plan_text
    if not plan_text:
        return False
    parsed = parse_plan(plan_text)
    current = parsed.current_step
    if current is None or current.process_keys:
        return False
    return bool(_AWAIT_USER_RE.search(current.full_text()))


def _find_next_executable_step(
    parsed: ParsedPlan,
    current_number: int,
    io_namespace: str | None,
) -> StepData | None:
    """Find the next pending step with resolvable process keys."""
    for step in parsed.steps:
        if step.number <= current_number:
            continue
        if step.is_completed or step.is_skipped:
            continue
        raw_keys = list(step.process_keys)
        if not raw_keys:
            return None
        resolved = _resolve_step_process_keys(raw_keys, io_namespace)
        if not resolved:
            return None
        return (step.number, resolved, step.has_planning_extension, step.min_actions)
    return None


def _get_wbs_checkpoint_data(
    plan_state: PlanState,
    io_namespace: str | None,
) -> StepData | None:
    """Detect WBS pause-and-resume; return next step data or ``None``."""
    plan_text = plan_state.focused_plan_text
    if not plan_text:
        return None
    parsed = parse_plan(plan_text)
    current = parsed.current_step
    if current is None or current.process_keys:
        return None

    if _AWAIT_USER_RE.search(current.full_text()):
        logger.info("WBS_CHECKPOINT: Step %d is await-user -- pausing", current.number)
        return None

    result = _find_next_executable_step(parsed, current.number, io_namespace)
    if result:
        logger.info(
            "WBS_CHECKPOINT: Step %d no keys -> next %d (%d keys)",
            current.number, result[0], len(result[1]),
        )
    return result


def _resolve_step_process_keys(
    raw_keys: list[str],
    io_namespace: str | None,
) -> list[str]:
    """Resolve IO placeholders and normalize IO process keys.

    Raises:
        RuntimeError: If a pseudo-key (``<POST_MESSAGE>``, ``<origin_io>``)
            requires io_namespace but io_namespace is None.
    """
    resolved: list[str] = []
    for key in raw_keys:
        stripped = key.strip()
        if stripped == "<POST_MESSAGE>":
            if not io_namespace:
                raise RuntimeError(
                    f"Cannot resolve pseudo-key {stripped!r}: io_namespace is None"
                )
            resolved.append(f"plugin::{io_namespace}::post_message")
            continue
        if "<origin_io>" in stripped:
            if not io_namespace:
                raise RuntimeError(
                    f"Cannot resolve pseudo-key <origin_io> in {stripped!r}: "
                    f"io_namespace is None"
                )
            stripped = stripped.replace("<origin_io>", io_namespace)
        is_post_message = (
            stripped.endswith("::post_message")
            and stripped.startswith("plugin::")
        )
        if is_post_message and io_namespace:
            stripped = f"plugin::{io_namespace}::post_message"
        if "<" in stripped:
            raise RuntimeError(
                f"Unresolved placeholder in process key: {stripped!r}"
            )
        resolved.append(stripped)
    return resolved


def _resolve_wbs_step_for_bound_keys(
    plan_state: PlanState,
) -> ParsedPlanStep | None:
    """Resolve the WBS step for bound-argument key merging."""
    if plan_state.wbs_step_number is None or not plan_state.focused_wbs_text:
        return None
    wbs_parsed = parse_plan(plan_state.focused_wbs_text)
    return wbs_parsed.step_by_number(plan_state.wbs_step_number)



def _adjust_initial_turn_schema(
    plan_state: PlanState,
    process_arg_lookup: ProcessArgLookup,
    *,
    io_namespace: str | None,
    plan_advancer: PlanAdvancer | None,
    session_id: str,
) -> StepSchemaResult:
    """Build output schema for initial turn (no prior tool observation)."""
    step_data = _get_current_step_data(plan_state, io_namespace)
    if step_data:
        return _build_focused_step_schema(plan_state, process_arg_lookup, step_data)

    checkpoint_data = _get_wbs_checkpoint_data(plan_state, io_namespace)
    if checkpoint_data:
        return _build_focused_step_schema(
            plan_state, process_arg_lookup, checkpoint_data,
        )

    # User completing an await-user step: advance the plan past
    # the await and use the next executable step's schema.
    result = _try_advance_past_await(
        plan_state, process_arg_lookup,
        io_namespace=io_namespace,
        plan_advancer=plan_advancer,
        session_id=session_id,
    )
    if result is not None:
        return result

    return _build_plan_selection_result(io_namespace)


def _try_advance_past_await(
    plan_state: PlanState,
    process_arg_lookup: ProcessArgLookup,
    *,
    io_namespace: str | None,
    plan_advancer: PlanAdvancer | None,
    session_id: str,
) -> StepSchemaResult | None:
    """Advance past an await-user step and return next step's schema."""
    if not _is_current_step_await_user(plan_state):
        return None
    plan_text = plan_state.focused_plan_text
    if not plan_text:
        return None

    parsed = parse_plan(plan_text)
    current = parsed.current_step
    if current is None:
        return None

    next_data = _find_next_executable_step(
        parsed, current.number, io_namespace,
    )
    if not next_data:
        return None

    if plan_advancer is None:
        raise RuntimeError(
            f"Cannot advance past await step {current.number} — "
            f"PlanAdvancer not provided"
        )

    # Let advancement failures propagate — building the next step's
    # schema while plan state remains on the await step creates
    # schema/state misalignment.
    plan_advancer.advance_current_plan_step(session_id=session_id)

    logger.info(
        "INITIAL_TURN: User completed await step %d -- using next step %d",
        current.number, next_data[0],
    )
    return _build_focused_step_schema(plan_state, process_arg_lookup, next_data)


def _adjust_post_observation_schema(
    plan_state: PlanState,
    process_arg_lookup: ProcessArgLookup,
    *,
    raw_observation_dict: dict[str, Any] | None,
    io_namespace: str | None,
) -> StepSchemaResult:
    """Apply output schema after tool observation."""
    process_key = _extract_observation_process_key(raw_observation_dict)

    step_data = _get_current_step_data(plan_state, io_namespace)
    if not step_data:
        step_data = _get_wbs_checkpoint_data(plan_state, io_namespace)

    current_keys = step_data[1] if step_data else []

    if _is_process_error_vertex(raw_observation_dict) and process_key:
        return _build_error_recovery_result(
            plan_state, process_arg_lookup,
            process_key, step_data, current_keys,
        )

    if step_data:
        result = _build_focused_step_schema(
            plan_state, process_arg_lookup, step_data,
        )
        return StepSchemaResult(
            result.output_schema, current_keys, result.model_visible_process_keys,
        )

    if _is_current_step_await_user(plan_state):
        logger.info("POST_OBS_SCHEMA: Await-user step -- empty-actions schema")
        return StepSchemaResult(_build_delivery_confirmation_schema(), current_keys, [])

    logger.info(
        "POST_OBS_SCHEMA: No step data -- plan selection (key=%s)", process_key,
    )
    return _build_plan_selection_result(io_namespace, current_keys=current_keys)


def _build_plan_selection_result(
    io_namespace: str | None,
    *,
    current_keys: list[str] | None = None,
) -> StepSchemaResult:
    """Build schema result for plan selection (Investigate Step 1).

    Raises:
        RuntimeError: If io_namespace is None (required for IO key).
    """
    if not io_namespace:
        raise RuntimeError(
            "Cannot build plan selection schema: io_namespace is None"
        )
    keys = [
        *_INVESTIGATE_STEP1_KEYS,
        f"plugin::{io_namespace}::post_message",
    ]
    schema = _step_narrowed_schema(keys, min_items=3)
    logger.info(
        "OUTPUT_SCHEMA: Plan selection -- narrowed schema (minItems=3: %s)",
        ", ".join(keys),
    )
    return StepSchemaResult(schema, current_keys or [], [])


_UPSERT_PLAN_KEY = "service_interface::thinking_service::upsert_plan"


def _build_error_recovery_result(
    plan_state: PlanState,
    process_arg_lookup: ProcessArgLookup,
    process_key: str,
    step_data: StepData | None,
    current_keys: list[str],
) -> StepSchemaResult:
    """Build schema result for error recovery.

    The recovery directive (``RECOVERY STRATEGY: Insert recovery
    steps into the active plan``) tells the model to call
    ``upsert_plan`` to add corrective steps before retrying. Without
    this allowance the model is trapped — it can only re-emit the
    process that just failed, and the same failure repeats until the
    flow is killed by the retry-cap guard.

    So the recovery schema includes the focused step's declared
    process(es) AND ``upsert_plan``. The model can either retry the
    action with corrected arguments or insert recovery steps. The
    minimum-actions floor drops to 1 (one of the available choices).
    """
    del process_arg_lookup  # custom arg schema isn't used here
    if step_data:
        _step_num, all_keys, _is_extension, _min_override = step_data
        recovery_keys = list(all_keys)
        if _UPSERT_PLAN_KEY not in recovery_keys:
            recovery_keys.append(_UPSERT_PLAN_KEY)
        schema = _step_narrowed_schema(recovery_keys, min_items=1)
        logger.info(
            "OUTPUT_SCHEMA: Error recovery -- step keys + upsert_plan "
            "(failed: %s; recovery_keys: %s)",
            process_key, ", ".join(recovery_keys),
        )
        _ = plan_state  # arg_schema binding deliberately omitted in recovery
        return StepSchemaResult(schema, current_keys, recovery_keys)

    schema = _action_schema(min_items=1)
    logger.info(
        "OUTPUT_SCHEMA: Error recovery -- canonical schema minItems=1 (failed: %s)",
        process_key,
    )
    return StepSchemaResult(schema, current_keys, [])


def _build_delivery_confirmation_schema() -> dict[str, object]:
    """Build empty-actions schema for delivery confirmation."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["actions"],
        "properties": {
            "actions": {
                "type": "array",
                "minItems": 0,
                "maxItems": 0,
            },
        },
        "additionalProperties": False,
    }


def _extract_observation_process_key(
    raw_observation_dict: dict[str, Any] | None,
) -> str | None:
    """Extract the completed process key from the raw observation dict."""
    if not raw_observation_dict:
        return None
    key = raw_observation_dict.get("process_key")
    return key if isinstance(key, str) else None


def _is_process_error_vertex(
    raw_observation_dict: dict[str, Any] | None,
) -> bool:
    """Check if the current vertex is a process_error (tool failure)."""
    if not raw_observation_dict:
        return False
    action_result = raw_observation_dict.get("action_result")
    if not isinstance(action_result, dict):
        return False
    status = action_result.get("action_status", "")
    return status in ("error", "failed")


