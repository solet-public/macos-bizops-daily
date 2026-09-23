"""Platform-owned inference transaction orchestration.

Owns the full flow: plan advancement → prompt assembly → model
invocation → action parsing → contract validation → event
persistence.  All calls go through typed platform functions or
the ``InferenceProvider`` protocol.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ananta.core.domain.types import ActionResult
from ananta.error_handling import FrameworkError

if TYPE_CHECKING:
    from ananta.core.plans.work_products import WorkProductRegister
    from ananta.core.prompts.context import PromptContext
    from ananta.interfaces.state_service_protocol import StateServiceProtocol
    from ananta.services.context_management.config import ContextManagementConfig
    from ananta.services.context_management.service import ContextManagementService
    from ananta.services.inference_service.interfaces.provider import (
        InferenceProvider,
    )

logger = logging.getLogger(__name__)


def execute(
    plugin: InferenceProvider,
    plugin_name: str,
    pipeline_factory: Any,
    orchestrator: object,
    state_service: StateServiceProtocol | None,
    flows_with_input_stored: set[str],
    params: dict[str, Any],
    state: dict[str, Any],
) -> ActionResult:
    """Run the full inference transaction.

    Called by ``InferenceService._execute_transaction``.
    """
    from ananta.core.plans.advancement import (
        has_focused_plan as _has_focused_plan,
    )
    from ananta.core.plans.advancement import maybe_advance_plan
    from ananta.services.inference_service.transaction import (
        create_inference_error_response,
        extract_action_parameters,
    )

    action_name, action_params = extract_action_parameters(params)

    # Plan advancement (platform functions + orchestrator services).
    # Focus is session-scoped (JOS-02): the VERTEX dispatch stamps the
    # action's OWN session into ``state`` (server-built by action_processor);
    # a session-less vertex is treated as plan-less (V-5 skip+log ruling).
    acting_session = str(state.get("session_id") or "")
    has_observation = "observation" in action_params.get("prompt", {})
    memory_svc = _get_service(orchestrator, "MEMORY_SERVICE")
    plan_svc = _get_service_optional(orchestrator, "PLAN_LIFECYCLE_SERVICE")
    maybe_advance_plan(
        action_name=action_name,
        is_continuation=has_observation,
        memory_provider=memory_svc,
        thinking_service=plan_svc,
        session_id=acting_session,
    )

    if action_name == "process_results" and not _has_focused_plan(
        memory_svc, session_id=acting_session,
    ):
        logger.info(
            "FLOW_COMPLETE: No focused plan for session %s — skipping inference",
            acting_session or "<none>",
        )
        return ActionResult(
            action_status="completed",
            data={"status": "flow_complete"},
            actions=[],
        )

    # Context config + context_id resolution
    from ananta.interfaces.context_management_contract import ContextManagementContract
    from ananta.services.context_management.types import ContextMode

    if not isinstance(plugin, ContextManagementContract):
        raise FrameworkError("Plugin does not implement ContextManagementContract")
    context_config = plugin.get_context_management_config()
    is_platform = context_config.context_mode == ContextMode.PLATFORM

    context_id: str | None = None
    if is_platform:
        context_id = _resolve_context_id(
            action_params, state, context_config, plugin_name, orchestrator,
        )

    # Resolve IO process key from flow service — single deterministic path.
    io_process_key = _resolve_io_process_key(orchestrator, state)
    state["io_process_key"] = io_process_key

    try:
        return _run_pipeline(
            plugin, plugin_name, pipeline_factory, orchestrator,
            state_service, flows_with_input_stored,
            action_params, action_name, state,
            context_id, is_platform, params,
        )
    except Exception as e:
        return create_inference_error_response(
            e, action_name, state, io_process_key,
        )


def _run_pipeline(
    plugin: InferenceProvider,
    plugin_name: str,
    pipeline_factory: Any,
    orchestrator: object,
    state_service: StateServiceProtocol | None,
    flows_with_input_stored: set[str],
    action_params: dict[str, Any],
    action_name: str,
    state: dict[str, Any],
    context_id: str | None,
    is_platform: bool,
    raw_params: dict[str, Any],
) -> ActionResult:
    """Prepare → infer → parse → validate → return."""
    from ananta.core.plans.contracts.action_normalization import (
        inject_job_context,
        inject_observation_into_create_extended_plan,
    )
    from ananta.core.prompts.decode.action_extraction import (
        has_explicit_actions_key,
        parse_llm_response_for_actions,
        validate_actions_found,
    )
    from ananta.services.inference_service.transaction import (
        build_placeholder_context,
        create_success_response,
        extract_job_id_from_context,
        log_request_to_state,
        log_response_to_state,
        normalize_action_definitions,
        prepare_inference_request,
        validate_model_config,
        validate_planning_extension_content,
    )

    # Prepare request (typed platform function)
    (
        request, model_info, resolved_user_input,
        _, __, prompt_ctx,
    ) = prepare_inference_request(
        action_params, action_name, state,
        plugin.get_inference_defaults(),
        pipeline_factory,
        context_id=context_id,
    )

    validate_model_config(model_info, plugin.get_configured_model_name())

    log_request_to_state(
        state, action_name, model_info,
        request_messages=request.messages,
        request_temperature=request.temperature,
        request_max_tokens=request.max_tokens,
        resolved_user_input=resolved_user_input,
        state_service=state_service,
    )

    # Context budget guard — trim history if prompt would overflow model
    from ananta.interfaces.context_management_contract import ContextManagementContract

    if isinstance(plugin, ContextManagementContract):
        guard_context_budget(request, plugin.get_context_management_config())

    # Inference (typed InferenceProvider method)
    completion_text, result_data = _invoke_and_extract(plugin, request)

    # Event persistence (typed platform module)
    if is_platform and context_id:
        _store_events(
            orchestrator, plugin, plugin_name,
            flows_with_input_stored,
            context_id, state, prompt_ctx, completion_text,
        )

    log_response_to_state(
        state, action_name, model_info,
        completion_text, result_data,
        state_service=state_service,
    )

    # Parse and normalize actions (platform functions)
    actions = parse_llm_response_for_actions(completion_text)
    if not actions and not has_explicit_actions_key(completion_text):
        error_msg = validate_actions_found(
            actions, completion_text, raw_response=completion_text,
        )
        if error_msg:
            raise FrameworkError(error_msg)

    inject_observation_into_create_extended_plan(
        actions, getattr(prompt_ctx, "tool_observation", None),
    )
    actions = normalize_action_definitions(
        actions, state.get("session_id"), state.get("flow_id"),
        context=build_placeholder_context(state, model_info, resolved_user_input),
        context_id=context_id,
    )

    # Step contract + work product injection (platform)
    _validate_step_contract(prompt_ctx, actions, state_service, pipeline_factory)

    validate_planning_extension_content(prompt_ctx, actions)

    job_ctx = extract_job_id_from_context(state, action_params, raw_params=raw_params)
    inject_job_context(actions, job_ctx)

    return create_success_response(
        completion_text, actions,
        {"timestamp": result_data.get("timestamp")},
    )


def guard_context_budget(
    request: Any,
    context_config: ContextManagementConfig,
) -> None:
    """Trim prompt messages that would overflow the model context window.

    Estimates input token count from message char lengths, reserves space
    for output tokens (``request.max_tokens``), and compares against the
    configured ``model_context_tokens``.  When the prompt is over budget,
    removes the oldest conversation-history messages (non-system,
    non-final-user) until it fits, logging a WARNING for each drop.

    If the prompt still exceeds the budget after all trimmable messages
    are removed, logs an ERROR but does not prevent the call — the model
    may still produce partial output.
    """
    messages: list[dict[str, str]] = request.messages
    chars_per_token = context_config.chars_per_token
    model_budget = context_config.model_context_tokens
    output_budget = getattr(request, "max_tokens", 0) or 0

    input_budget = model_budget - output_budget
    if input_budget <= 0:
        logger.error(
            "CONTEXT_BUDGET: output budget (%d) >= model context (%d) — "
            "cannot reserve input space",
            output_budget, model_budget,
        )
        return

    def _estimate_tokens() -> int:
        total_chars = sum(len(m.get("content", "")) for m in messages)
        return total_chars // max(chars_per_token, 1)

    estimated = _estimate_tokens()
    if estimated <= input_budget:
        return

    # Identify trimmable range: everything between system messages
    # and the final user message.
    first_non_system = 0
    for i, m in enumerate(messages):
        if m.get("role") != "system":
            first_non_system = i
            break

    # The last message is the current-turn instruction — keep it.
    trim_end = len(messages) - 1
    trim_start = first_non_system

    dropped = 0
    while trim_start < trim_end and _estimate_tokens() > input_budget:
        removed = messages.pop(trim_start)
        trim_end -= 1
        dropped += 1
        logger.warning(
            "CONTEXT_BUDGET_TRIM: dropped %s message (%d chars) — "
            "input est %d tokens vs budget %d",
            removed.get("role", "?"),
            len(removed.get("content", "")),
            _estimate_tokens(), input_budget,
        )

    if dropped:
        logger.warning(
            "CONTEXT_BUDGET: trimmed %d messages to fit model context "
            "(%d token budget, est %d tokens remaining)",
            dropped, input_budget, _estimate_tokens(),
        )

    if _estimate_tokens() > input_budget:
        logger.error(
            "CONTEXT_BUDGET: prompt still exceeds budget after trimming "
            "all history (%d tokens est vs %d budget) — proceeding anyway",
            _estimate_tokens(), input_budget,
        )


def _invoke_and_extract(
    plugin: InferenceProvider,
    request: Any,
) -> tuple[str, dict[str, Any]]:
    """Call generate_completion and extract completion text."""
    result = plugin.generate_completion(request)
    error_value = result.get("error")
    if error_value:
        raise FrameworkError(error_value.get("message", "Inference failed"))
    result_data: dict[str, Any] = result.get("data", {})
    raw = result_data.get("result", {})
    completion = raw if isinstance(raw, dict) else {}
    return str(completion.get("completion", "")), result_data


def _validate_step_contract(
    prompt_ctx: PromptContext,
    actions: list[dict[str, Any]],
    state_service: StateServiceProtocol | None,
    pipeline_factory: Any,
) -> None:
    """Validate step contract, enforce bound args, inject work products."""
    from ananta.core.plans.contracts.action_contract import validate_step_contract
    from ananta.core.plans.work_product_runtime import (
        enforce_bound_argument_values,
        inject_work_product_values,
        resolve_current_bound_sub_steps,
    )

    visible = prompt_ctx.model_visible_process_keys or prompt_ctx.current_step_process_keys
    if not visible:
        return

    validate_step_contract(actions, visible)

    plan_state = prompt_ctx.plan_state
    if plan_state is None:
        return

    # Enforce WBS bound argument values (with schema validation)
    bound_subs, _ = resolve_current_bound_sub_steps(plan_state)
    has_composed = any(bs.composed_references for bs in bound_subs)
    register = _load_register(plan_state, state_service) if has_composed else None
    arg_lookup = getattr(
        getattr(pipeline_factory, "_deps", None),
        "process_arg_lookup", None,
    )
    enforce_bound_argument_values(plan_state, actions, register, arg_lookup)

    # Inject deterministic work product filenames
    if state_service is not None and arg_lookup is not None:
        inject_work_product_values(plan_state, actions, state_service, arg_lookup)


def _load_register(
    plan_state: Any,
    state_service: StateServiceProtocol | None,
) -> WorkProductRegister | None:
    """Load work product register for composed reference resolution."""
    from ananta.core.plans.windowing import ACTIVE_WBS_HEADER_RE, ACTIVE_WORK_PRODUCT_RUN_RE
    from ananta.core.plans.work_product_store import WorkProductStoreAdapter
    from ananta.core.plans.work_products import WorkProductRegister

    if state_service is None:
        return None
    plan_text = plan_state.focused_plan_text or ""
    wbs_match = ACTIVE_WBS_HEADER_RE.search(plan_text)
    if not wbs_match:
        return None
    run_match = ACTIVE_WORK_PRODUCT_RUN_RE.search(plan_text)
    run_id = run_match.group(1) if run_match else None
    store = WorkProductStoreAdapter(state_service, work_product_run_id=run_id)
    data = store.load_register(wbs_match.group(1))
    return WorkProductRegister.deserialize(data) if data else WorkProductRegister()


def _resolve_context_id(
    action_params: dict[str, Any],
    state: dict[str, Any],
    context_config: Any,
    plugin_name: str,
    orchestrator: object,
) -> str | None:
    """Resolve context_id via typed platform module."""
    from ananta.services.inference_service.context_id import resolve_context_id

    ctx_mgmt = _get_context_management_service(orchestrator)
    return resolve_context_id(
        action_params, state,
        context_config.context_id_source.value,
        provider_name=plugin_name,
        registry=ctx_mgmt.registry,
        address_key=context_config.context_id_address_key,
    )


def _store_events(
    orchestrator: object,
    plugin: InferenceProvider,
    plugin_name: str,
    flows_with_input_stored: set[str],
    context_id: str,
    state: dict[str, Any],
    prompt_ctx: PromptContext,
    completion_text: str,
) -> None:
    """Store post-inference events via typed platform module."""
    from ananta.interfaces.context_management_contract import ContextManagementContract
    from ananta.services.inference_service.event_persistence import (
        store_post_inference_events,
    )

    ctx_mgmt = _get_context_management_service(orchestrator)
    if not isinstance(plugin, ContextManagementContract):
        raise FrameworkError("Plugin does not implement ContextManagementContract")
    config = plugin.get_context_management_config()
    resolved_params: dict[str, Any] = prompt_ctx.resolved_action_params
    store_post_inference_events(
        context_id, state, resolved_params,
        completion_text, prompt_ctx, flows_with_input_stored,
        event_writer=ctx_mgmt.events,
        content_storage=ctx_mgmt.content_storage,
        provider_name=plugin_name,
        sessions=ctx_mgmt.sessions,
        compaction=ctx_mgmt,
        compaction_config=config,
    )


# ── IO process key resolution (flow-service, single path) ──


def _resolve_io_process_key(
    orchestrator: object,
    state: dict[str, Any],
) -> str:
    """Resolve the IO plugin's post_message process key from the flow.

    Queries the flow service for the flow's ``source_namespace`` and
    constructs ``plugin::<namespace>::post_message``.  This is the single
    deterministic resolution path — no fallback, no cache check.

    Raises:
        FrameworkError: If the flow service is unavailable, the flow has
            no source_namespace, or any other resolution failure.
    """
    from ananta.constants import CONTEXT_KEY_FLOW_ID

    flow_id = state.get(CONTEXT_KEY_FLOW_ID)
    if not flow_id:
        raise FrameworkError(
            "Cannot resolve IO process key: no flow_id in state"
        )

    get_svc = getattr(orchestrator, "get_service", None)
    if not callable(get_svc):
        raise FrameworkError("orchestrator missing get_service")
    flow_service = get_svc("flow_service")
    if flow_service is None:
        raise FrameworkError("flow_service not available on orchestrator")
    result = flow_service.get_flow_input(str(flow_id))
    inner = result["data"]["result"]
    source_namespace: str = inner.get("source_namespace", "")
    if not source_namespace:
        # P1-A 2026-06-16: explicit message when a system-owned periodic cron
        # reaches IO-process-key resolution. These crons MUST be
        # terminal/headless — reaching this path indicates the cron's
        # action declared ``result_processor_kind="inference"`` or the action
        # definition carries ``result_processor_customizations`` so the
        # action_factory wired a result processor that drove the result
        # through ``_resolve_io_process_key`` for IO routing. The fix is
        # to drop both declarations so ``action_queue_poller`` short-circuits
        # at the EDGE_SINK_SKIP branch (terminal action, no dispatch). See
        # ``knowledge_bases/ananta_platform/21_scheduling_service/01_template_flow_record_lifecycle.md``
        # for the canonical pattern.
        if inner.get("kind") == "system_owned_periodic_cron":
            raise FrameworkError(
                f"flow {flow_id} declares kind=system_owned_periodic_cron "
                "but reached _resolve_io_process_key; system-owned crons "
                "should be terminal/headless and not enter generic "
                "result-processing inference. See "
                "`21_scheduling_service/01_template_flow_record_lifecycle.md` "
                "for the canonical pattern. Either route through a "
                "terminal/headless action shape OR declare a real "
                "source_namespace."
            )
        raise FrameworkError(
            f"Empty source_namespace in flow trigger_data for flow {flow_id}"
        )
    return f"plugin::{source_namespace}::post_message"


# ── Service resolution (typed, from orchestrator) ──


def _get_service(orchestrator: object, name: str) -> Any:
    """Resolve a required service by ServiceName enum member name."""
    from ananta.core.orchestration.service_bindings import ServiceName

    get_svc = getattr(orchestrator, "get_service", None)
    if not callable(get_svc):
        raise FrameworkError("orchestrator missing get_service")
    svc = get_svc(getattr(ServiceName, name))
    if svc is None:
        raise FrameworkError(f"{name} not available on orchestrator")
    return svc


def _get_service_optional(orchestrator: object, name: str) -> Any:
    """Resolve an optional service — returns None if unavailable."""
    from ananta.core.orchestration.service_bindings import ServiceName

    get_svc = getattr(orchestrator, "get_service", None)
    if not callable(get_svc):
        return None
    return get_svc(getattr(ServiceName, name))


def _get_context_management_service(
    orchestrator: object,
) -> ContextManagementService:
    """Resolve context management service with type narrowing."""
    from ananta.services.context_management.service import ContextManagementService

    get_svc = getattr(orchestrator, "get_service", None)
    if not callable(get_svc):
        raise FrameworkError("orchestrator missing get_service")
    svc = get_svc("context_management_service")
    if not isinstance(svc, ContextManagementService):
        raise FrameworkError("Context management service not available")
    return svc
