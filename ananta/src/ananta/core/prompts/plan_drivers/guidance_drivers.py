"""Step instruction building, driver message assembly, and enrichment.

Pure functions for constructing step-derived instruction text,
WBS execution guardrails, and driver message content for observation
turns.  No plugin or context dependencies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from ananta.core.prompts.plan_drivers.wbs_bindings import (
    lift_bound_arguments_from_wbs,
    step_has_post_message_sub_step,
)

if TYPE_CHECKING:
    from ananta.core.plans.types import ParsedPlanStep
    from ananta.core.prompts.plan_state import PlanState

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

_SEARCH_PROCESS_KEY = "service_interface::knowledge_service::search"

# ── Protocol ─────────────────────────────────────────────────────────


class ProcessDataLookup(Protocol):
    """Looks up process data from the registry."""

    def get_process_data(self, process_key: str) -> dict[str, object] | None: ...

    def get_schema_visible_keys(
        self, step: ParsedPlanStep,
    ) -> set[str] | None: ...

    def get_required_keys(
        self, step: ParsedPlanStep,
    ) -> set[str]: ...

    def resolve_delivery_attachment(
        self, plan_state: PlanState, step: ParsedPlanStep,
    ) -> str | None: ...


class DiscoveryProcessDataLookup:
    """Platform-owned ``ProcessDataLookup`` using discovery + state services.

    Replaces the plugin's closure-based adapter.
    """

    def __init__(
        self,
        discovery_service: Any,
        state_service: Any,
    ) -> None:
        self._discovery = discovery_service
        self._state = state_service

    def get_process_data(self, process_key: str) -> dict[str, object] | None:
        result = self._discovery.get_process_by_key(process_key)
        return result if isinstance(result, dict) else None

    def get_schema_visible_keys(
        self, step: ParsedPlanStep,
    ) -> set[str] | None:
        from ananta.core.plans.work_product_policies import (
            get_all_owned_output_slots,
            get_audio_midi_policy,
        )
        from ananta.core.prompts.decode.action_schema import (
            extract_invocation_arg_properties,
        )

        plugin_keys = [
            k for k in step.process_keys
            if k.startswith("plugin::") and not k.endswith("::post_message")
        ]
        if not plugin_keys:
            return None

        # Use the same extraction the output schema uses so the prompt
        # text and the constrained-decoding grammar agree on which
        # argument keys exist.
        visible: set[str] = set()
        for key in plugin_keys:
            data = self._discovery.get_process_by_key(key)
            if isinstance(data, dict):
                visible |= set(extract_invocation_arg_properties(data).keys())

        if not visible:
            return None

        visible -= get_all_owned_output_slots(get_audio_midi_policy())
        return visible

    def get_required_keys(
        self, step: ParsedPlanStep,
    ) -> set[str]:
        """Return required argument keys for the step's plugin processes."""
        plugin_keys = [
            k for k in step.process_keys
            if k.startswith("plugin::") and not k.endswith("::post_message")
        ]
        required: set[str] = set()
        for key in plugin_keys:
            required |= self._lookup_required_properties(key)
        return required

    def resolve_delivery_attachment(
        self, plan_state: PlanState, step: ParsedPlanStep,
    ) -> str | None:
        from ananta.core.plans.windowing import ACTIVE_WBS_HEADER_RE
        from ananta.core.plans.work_product_runtime import resolve_latest_delivery_attachment
        from ananta.core.plans.work_product_store import WorkProductStoreAdapter
        from ananta.core.plans.work_products import WorkProductRegister
        from ananta.core.prompts.plan_drivers.wbs_bindings import resolve_wbs_step_number

        has_pm = any(
            k == "<POST_MESSAGE>" or k.endswith("::post_message")
            for k in step.process_keys
        )
        if not has_pm:
            return None
        wbs_num = resolve_wbs_step_number(step)
        if wbs_num is None:
            return None
        plan_text = plan_state.focused_plan_text
        if not plan_text:
            return None
        from ananta.core.plans.windowing import ACTIVE_WORK_PRODUCT_RUN_RE

        wbs_match = ACTIVE_WBS_HEADER_RE.search(plan_text)
        if not wbs_match:
            return None
        if self._state is None:
            return None
        run_match = ACTIVE_WORK_PRODUCT_RUN_RE.search(plan_text)
        run_id = run_match.group(1) if run_match else None
        store = WorkProductStoreAdapter(self._state, work_product_run_id=run_id)
        data = store.load_register(wbs_match.group(1))
        register = (
            WorkProductRegister.deserialize(data) if data
            else WorkProductRegister()
        )
        return resolve_latest_delivery_attachment(register, wbs_num)

    def _lookup_declared_properties(self, process_key: str) -> set[str]:
        """Look up all declared argument property names from registry."""
        args = self._navigate_args(process_key)
        if args is None:
            return set()
        props = args.get("properties")
        return set(props.keys()) if isinstance(props, dict) else set()

    def _lookup_required_properties(self, process_key: str) -> set[str]:
        """Look up required argument property names from registry."""
        args = self._navigate_args(process_key)
        if args is None:
            return set()
        req = args.get("required")
        return {str(r) for r in req} if isinstance(req, list) else set()

    def _navigate_args(self, process_key: str) -> dict[str, object] | None:
        """Navigate invocation schema to the arguments sub-schema."""
        data = self._discovery.get_process_by_key(process_key)
        if not isinstance(data, dict):
            return None
        schema = data.get("invocation_schema")
        if not isinstance(schema, dict):
            return None
        outer = schema.get("properties")
        if not isinstance(outer, dict):
            return None
        args = outer.get("arguments")
        return args if isinstance(args, dict) else None


# ── Step identification ──────────────────────────────────────────────


def format_step_identification(
    step: ParsedPlanStep,
    active_step: ParsedPlanStep | None,
) -> str:
    """Format the step-identification header for the instruction.

    When the active step is an await-user step (no process keys), the
    driver targets the next executable step directly.  The "paused at"
    preamble is not shown — the canonical format is just "Your current
    step is:" pointing at the target.
    """
    is_await = (
        active_step is not None
        and active_step.number != step.number
        and not active_step.process_keys
    )
    if active_step is not None and active_step.number != step.number and not is_await:
        return (
            "The active plan is currently paused at:\n"
            f"{active_step.summary()}\n\n"
            "Resume with the next executable step:\n"
            f"{step.summary()}"
        )
    return f"Your current step is:\n{step.summary()}"


# ── Action list formatting ───────────────────────────────────────────


def format_step_action_list(step: ParsedPlanStep) -> str:
    """Format the model-visible action list for the step instruction.

    For planning-extension steps, all declared keys are visible.
    For progress-only steps, ``upsert_plan`` is hidden (auto-injected).
    """
    if step.has_planning_extension:
        visible = list(step.process_keys)
    else:
        visible = [
            k for k in step.process_keys
            if not k.endswith("::upsert_plan")
        ]
        if not visible:
            visible = list(step.process_keys)
    lines = [f"This step requires exactly {len(visible)} action(s):"]
    for i, key in enumerate(visible):
        lines.append(f"  {i + 1}. {key}")
    return "\n".join(lines)


# ── Step instruction building ────────────────────────────────────────


def build_focused_step_instruction(
    step: ParsedPlanStep,
    *,
    active_step: ParsedPlanStep | None = None,
    plan_state: PlanState | None = None,
    session_id: str = "",
    has_reinforcement: bool = False,
    process_lookup: ProcessDataLookup | None = None,
    observation_process_key: str | None = None,
) -> str:
    """Build plan-derived instruction for the current focused step.

    Content is derived from the step's text, annotations, and -- for
    steps that contain a search process -- the search process's
    registered guidance.  No flow-specific text is hardcoded.

    When ``has_reinforcement`` is True, the guidance article's
    ``### Driver Reinforcement`` subsection will be appended by
    the caller.  In that case, generic enrichment (delegation
    instructions, search guidance) is suppressed so the reinforcement
    is the sole authoritative instruction block.
    """
    parts: list[str] = []

    # Step identification
    parts.append(format_step_identification(step, active_step))

    # Sub-step guidance.  Keep GUIDANCE_ARTICLE/GUIDANCE_SECTION
    # visible in the driver so the model sees which article applies.
    # Only strip legacy PLAYBOOK metadata.
    sub_lines = [
        line
        for line in step.lines[1:]
        if not line.strip().startswith(("PLAYBOOK:", "PLAYBOOK_SECTION:"))
    ]
    if sub_lines:
        parts.append("\n".join(sub_lines))

    # Tail: execution format instruction
    if step.playbook_section_id:
        parts.append(
            "The playbook section loaded above contains the guidance "
            "for this step. Follow it to determine what to retrieve, "
            "what to decide, and how to communicate."
        )
    elif step.process_keys:
        parts.append(format_step_action_list(step))
    else:
        parts.append("Execute this step using the declared processes.")

    if not has_reinforcement:
        enrich_step_parts(
            step, parts, plan_state,
            session_id=session_id,
            process_lookup=process_lookup,
            observation_process_key=observation_process_key,
        )
    return "\n\n".join(parts)


# ── Step enrichment ──────────────────────────────────────────────────


def enrich_step_parts(
    step: ParsedPlanStep,
    parts: list[str],
    plan_state: PlanState | None,
    *,
    session_id: str = "",
    process_lookup: ProcessDataLookup | None = None,
    observation_process_key: str | None = None,
) -> None:
    """Add search guidance, delegation, and bound arguments.

    Skipped entirely when a Driver Reinforcement block is present,
    since the reinforcement subsection is the sole authoritative
    instruction for the step.
    """
    if not step.guidance_article and process_lookup is not None:
        search_guidance = get_search_pre_action_guidance(
            step, process_lookup,
            observation_process_key=observation_process_key,
        )
        if search_guidance:
            parts.append(search_guidance)

    layer_policy_block = _build_layer_policy_bound_block(step)
    if layer_policy_block:
        parts.append(layer_policy_block)

    if plan_state is not None and process_lookup is not None:
        bound_block = _lift_bound_arguments(
            plan_state, step,
            session_id=session_id,
            process_lookup=process_lookup,
        )
        if bound_block:
            parts.append(bound_block)
        guardrails = build_wbs_execution_guardrails(plan_state, step)
        if guardrails:
            parts.append(guardrails)


# ── Layer-policy binding ────────────────────────────────────────────


def _build_layer_policy_bound_block(step: ParsedPlanStep) -> str:
    """Render the step's ``LAYER_POLICY:`` annotation as a "Bound
    arguments" block when the step's continuation actions include
    ``knowledge_service::search``.

    Returns an empty string when the step has no layer policy or when
    the step's continuation actions don't include search.
    """
    policy = step.layer_policy
    if policy is None or policy.is_empty:
        return ""
    if _SEARCH_PROCESS_KEY not in step.process_keys:
        return ""

    lines = ["Bound arguments — use these exact values:"]
    if policy.knowledge_layers is not None:
        layer_list = ", ".join(str(layer) for layer in policy.knowledge_layers)
        lines.append(f"  knowledge_layers: [{layer_list}]")
    if policy.min_knowledge_layer is not None:
        lines.append(f"  min_knowledge_layer: {policy.min_knowledge_layer}")
    if policy.max_knowledge_layer is not None:
        lines.append(f"  max_knowledge_layer: {policy.max_knowledge_layer}")
    if policy.include_unlayered is not None:
        flag = "true" if policy.include_unlayered else "false"
        lines.append(f"  include_unlayered: {flag}")
    return "\n".join(lines)


# ── WBS binding integration ─────────────────────────────────────────


def _filter_visible_by_bound(
    visible_keys: set[str],
    wbs_text: str,
    step: ParsedPlanStep,
    process_lookup: ProcessDataLookup,
) -> set[str]:
    """Remove bound optional keys from visible_keys to keep schema aligned."""
    from ananta.core.plans.parser import parse as parse_plan
    from ananta.core.prompts.plan_drivers.wbs_bindings import resolve_wbs_step_number

    wbs_step_number = resolve_wbs_step_number(step)
    if wbs_step_number is None:
        return visible_keys
    wbs_step = parse_plan(wbs_text).step_by_number(wbs_step_number)
    if wbs_step is None:
        return visible_keys
    required = process_lookup.get_required_keys(step)
    bound_keys: set[str] = set()
    for sub in wbs_step.bound_sub_steps:
        if sub.arguments:
            bound_keys |= set(sub.arguments.keys())
    if not bound_keys:
        return visible_keys
    return {k for k in visible_keys if k in required or k not in bound_keys}


def _lift_bound_arguments(
    plan_state: PlanState,
    step: ParsedPlanStep,
    *,
    session_id: str = "",
    process_lookup: ProcessDataLookup,
) -> str:
    """Lift bound arguments from the focused WBS for the current step.

    Filters the displayed bound arguments to only those the output
    schema grammar can accept — i.e. invocation schema properties minus
    owned output slots minus bound optional keys.
    """
    wbs_text = plan_state.focused_wbs_text
    if not wbs_text:
        return ""
    delivery_attachment = process_lookup.resolve_delivery_attachment(plan_state, step)
    visible_keys = process_lookup.get_schema_visible_keys(step)
    if visible_keys is not None:
        visible_keys = _filter_visible_by_bound(visible_keys, wbs_text, step, process_lookup)
    text, _attachment, _sess = lift_bound_arguments_from_wbs(
        wbs_text,
        step,
        session_id=session_id,
        delivery_attachment=delivery_attachment,
        schema_visible_keys=visible_keys,
    )
    return text


# ── WBS execution guardrails ────────────────────────────────────────


def build_wbs_execution_guardrails(
    plan_state: PlanState,
    step: ParsedPlanStep,
) -> str:
    """Build negative guardrails for WBS execution steps.

    Progress-only WBS execution steps should not emit actions beyond
    what the step declares.  The guardrails prevent the model from
    adding post_message, patch operations, or full document rewrites.

    Delivery ``<POST_MESSAGE>`` steps are exempt from the
    ``post_message`` guardrail.

    For the completion handoff step (last WBS step), also adds the
    continuation guardrail to preserve the Phase N+1 planning tail.
    """
    step_text = step.full_text()
    if "WBS Step" not in step_text:
        return ""

    is_delivery = step_has_post_message_sub_step(step)

    lines: list[str] = []
    if not is_delivery:
        lines.append("Do not emit `post_message` on this turn.")
    lines.extend([
        "Do not emit `patch_work_breakdown_structure` on this turn.",
        "Do not emit `patch_work_manifest` on this turn.",
        "Do not emit `register_authored_work_breakdown_structure` on this turn.",
        "Do not return full document rewrites on this turn.",
    ])

    # Check for continuation tail -- if Phase N+1 steps exist
    # after the current step, add the continuation guardrail.
    plan_text = plan_state.focused_plan_text
    if plan_text and "Create the Phase" in plan_text:
        lines.append("")
        lines.append(
            "Leave the already-authored next-phase planning steps "
            "in place."
        )

    return "\n".join(lines)


# ── Search guidance ──────────────────────────────────────────────────


def _step_involves_search(
    step: ParsedPlanStep,
    observation_process_key: str | None,
) -> bool:
    """Return True if the step or its observation involves a search action."""
    if _SEARCH_PROCESS_KEY in step.process_keys:
        return True
    return observation_process_key is not None and "search" in observation_process_key


def _build_search_guidance_parts(customizations: dict[str, object]) -> list[str]:
    """Extract guidance text parts from search result_processor_customizations."""
    parts: list[str] = []
    output_guidance = customizations.get("output_action_guidance", "")
    if isinstance(output_guidance, str) and output_guidance.strip():
        parts.append(output_guidance.strip())
    presentation = customizations.get("presentation_guidance", "")
    if isinstance(presentation, str) and presentation.strip():
        parts.append(f"Response format: {presentation.strip()}")
    return parts


def get_search_pre_action_guidance(
    step: ParsedPlanStep,
    process_lookup: ProcessDataLookup,
    *,
    observation_process_key: str | None = None,
) -> str | None:
    """Extract pre-action guidance for search-observation resume steps.

    Injects the search process's ``output_action_guidance`` and
    ``presentation_guidance`` when the observation came from a search
    action OR the current step declares a search action.  This ensures
    post-search steps (like a checkpoint post_message after a search)
    still receive the guidance that constrains the model's response.
    """
    if not _step_involves_search(step, observation_process_key):
        return None
    process_data = process_lookup.get_process_data(_SEARCH_PROCESS_KEY)
    if not process_data:
        return None
    customizations = process_data.get("result_processor_customizations")
    if not isinstance(customizations, dict):
        return None
    parts = _build_search_guidance_parts(customizations)
    return "\n\n".join(parts) if parts else None


# ── Delegated artifact detection ─────────────────────────────────────


def step_has_delegated_artifact(
    step: ParsedPlanStep,
    process_lookup: ProcessDataLookup,
) -> bool:
    """Check if any process in this step has a delegated-artifact contract."""
    for key in step.process_keys:
        process_data = process_lookup.get_process_data(key)
        if not isinstance(process_data, dict):
            continue
        contract = process_data.get("prompt_contract")
        if isinstance(contract, dict) and contract.get("kind") == "delegated_artifact_creation":
            return True
    return False


# ── Driver message assembly ──────────────────────────────────────────


def build_driver_messages(
    instruction: str,
    step: ParsedPlanStep,
    plan_state: PlanState,
    *,
    tool_observation: str | None = None,
    raw_observation_dict: dict[str, Any] | None = None,
    reinforcement: str = "",
    process_lookup: ProcessDataLookup,
) -> tuple[list[dict[str, str]], str | None]:
    """Build the final USER message content for observation-driven turns.

    Returns ``(extra_messages, user_prompt_replacement)``.
    ``user_prompt_replacement`` is ``None`` when no change is needed,
    otherwise it replaces the existing user prompt.
    """
    use_delegation = (
        step_has_delegated_artifact(step, process_lookup)
        and not step.has_planning_extension
    )

    if use_delegation:
        content = _build_delegation_driver(
            plan_state, instruction, reinforcement,
            raw_observation_dict=raw_observation_dict,
        )
    else:
        content = append_to_observation(
            tool_observation or "", instruction, reinforcement,
        )

    return [], content


def _build_delegation_driver(
    plan_state: PlanState,
    instruction: str,
    reinforcement: str,
    *,
    raw_observation_dict: dict[str, Any] | None = None,
) -> str:
    """Build USER driver content for single-action delegated artifact steps."""
    parts: list[str] = []
    intake_handoff = render_resolved_intake_handoff(plan_state)
    if intake_handoff:
        parts.append(intake_handoff)
    artifact_handoff = render_artifact_handoff(raw_observation_dict)
    if artifact_handoff:
        parts.append(artifact_handoff)
    parts.append(instruction)
    if reinforcement:
        parts.append(reinforcement)
    return "\n\n".join(parts)


def append_to_observation(
    existing: str,
    instruction: str,
    reinforcement: str,
) -> str:
    """Build USER driver from observation, instruction, and reinforcement.

    When reinforcement is present, the observation is suppressed
    entirely.  The reinforcement subsection is the sole authoritative
    instruction.
    """
    if reinforcement:
        return f"{instruction}\n\n{reinforcement}"
    if existing:
        return f"{existing}\n\n{instruction}"
    return instruction


# ── Artifact handoff rendering ───────────────────────────────────────


def render_artifact_handoff(
    raw_observation_dict: dict[str, Any] | None,
) -> str:
    """Re-render search findings as a condensed artifact-handoff summary."""
    from ananta.core.prompts.renderers import render_artifact_handoff as _render

    if not raw_observation_dict:
        return ""
    action_result = raw_observation_dict.get("action_result")
    if not isinstance(action_result, dict):
        return ""
    rendered = _render(action_result)
    return rendered or ""


def render_resolved_intake_handoff(plan_state: PlanState) -> str:
    """Render the focused Resolved Intake State as a structured handoff.

    When a durable Resolved Intake State artifact is in the focus buffer,
    render it as a handoff block for delegated-artifact creation steps.
    """
    intake_text = plan_state.focused_resolved_intake_text
    if not intake_text:
        return ""
    return (
        "Resolved intake state (authoritative for blocker fields and "
        "deliberate defaults):\n\n"
        + intake_text.strip()
    )
