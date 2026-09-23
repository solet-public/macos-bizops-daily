"""Inference transaction helpers — platform-owned, provider-independent.

Pure and near-pure functions extracted from the inference plugin.
These handle action normalization, plan extension validation,
request/response logging, and result construction.
"""

from __future__ import annotations

import logging
import time as time_module
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ananta.core.contexts.normalization import normalize_flow_id, normalize_session_id
from ananta.core.domain.types import ActionResult
from ananta.core.plans import parse as parse_plan
from ananta.core.plans.contracts.action_normalization import (
    build_action_definition,
    extract_process_parts,
    resolve_placeholders_in_dict,
)
from ananta.core.plans.parser import normalize_content as normalize_plan_content
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER
from ananta.error_handling import AnantaError

if TYPE_CHECKING:
    from ananta.core.plans.types import ParsedPlan
    from ananta.core.prompts.context import PromptContext
    from ananta.interfaces.inference_service_interface import InferenceRequest
    from ananta.interfaces.state_service_protocol import StateServiceProtocol
    from ananta.services.inference_service.interfaces.provider import InferenceDefaults

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Placeholder context
# ---------------------------------------------------------------------------


def build_placeholder_context(
    state: dict[str, Any],
    model_info: dict[str, Any],
    resolved_user_input: str | None,
) -> dict[str, str]:
    """Build comprehensive placeholder context for action normalization."""
    now_utc = datetime.now(UTC)
    now_local = datetime.now()

    if time_module.daylight:
        tz_offset = time_module.altzone
        tz_name = time_module.tzname[1]
    else:
        tz_offset = time_module.timezone
        tz_name = time_module.tzname[0]

    tz_hours = -tz_offset // 3600
    tz_sign = "+" if tz_hours >= 0 else "-"
    tz_str = f"UTC{tz_sign}{abs(tz_hours)}"

    normalized_session_id = normalize_session_id(state.get("session_id"))
    normalized_flow_id = normalize_flow_id(state.get("flow_id"))

    placeholder_context: dict[str, str] = {
        "TIMESTAMP": now_utc.isoformat(),
        "DATE": now_local.strftime("%Y-%m-%d"),
        "TIME": now_local.strftime(f"%H:%M:%S {tz_name}"),
        "TIMEZONE": tz_name,
        "TIMEZONE_OFFSET": tz_str,
    }

    if normalized_session_id:
        placeholder_context["SESSION_ID"] = normalized_session_id
    if normalized_flow_id:
        placeholder_context["FLOW_ID"] = normalized_flow_id
    if resolved_user_input:
        placeholder_context["USER_INPUT"] = resolved_user_input

    model_name = model_info.get("name")
    if model_name:
        placeholder_context["MODEL_NAME"] = str(model_name)

    return placeholder_context


# ---------------------------------------------------------------------------
# Action normalization
# ---------------------------------------------------------------------------


def normalize_action_definitions(
    raw_actions: list[dict[str, Any]],
    session_id: str | None,
    flow_id: str | None,
    context: dict[str, Any] | None = None,
    context_id: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize raw action steps from LLM into proper action definitions.

    FAIL-FAST: Raises immediately on any invalid action.
    """
    normalized: list[dict[str, Any]] = []

    for i, action in enumerate(raw_actions):
        parts = extract_process_parts(action)
        if not parts:
            action_id = (
                action.get("process", {}).get("function_name")
                or action.get("process_key", "unknown")
            )
            raise RuntimeError(
                f"Action {i} has invalid/missing process object: {action_id}"
            )

        action_def = build_action_definition(
            action, parts, session_id, flow_id, context_id,
        )

        if session_id and "session_id" in action_def.get("arguments", {}):
            action_def["arguments"]["session_id"] = session_id

        if context:
            action_def["arguments"] = resolve_placeholders_in_dict(
                action_def["arguments"], context,
            )

        normalized.append(action_def)

    return normalized


# ---------------------------------------------------------------------------
# Planning extension helpers
# ---------------------------------------------------------------------------


def _get_focused_plan_text(ctx: PromptContext) -> str | None:
    """Extract plan text from focused memories on the prompt context."""
    for mem in ctx.focused_memories:
        content = mem.get("content", "")
        if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
            return content
    return None


def find_upsert_action(
    actions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the upsert_plan action in the action list."""
    for action in actions:
        pk = action.get("process_key", "")
        if isinstance(pk, str) and pk.endswith("::upsert_plan"):
            return action
    return None


def extract_planning_extension(
    ctx: PromptContext,
    actions: list[dict[str, Any]],
) -> tuple[ParsedPlan, ParsedPlan, str, int, dict[str, Any]] | None:
    """Extract and parse planning-extension data, or ``None`` if not applicable."""
    plan_text = _get_focused_plan_text(ctx)
    if not plan_text:
        return None

    existing = parse_plan(plan_text)
    current = existing.current_step
    if current is None or not current.has_planning_extension:
        return None

    upsert_action = find_upsert_action(actions)
    if upsert_action is None:
        return None

    raw_content = upsert_action.get("arguments", {}).get("content")
    if not raw_content:
        return None

    try:
        normalized = normalize_plan_content(raw_content)
        submitted = parse_plan(normalized)
    except ValueError:
        return None  # Malformed -- let downstream handle it

    boundary = current.number + 1
    return existing, submitted, normalized, boundary, upsert_action


def merge_prefix_with_model_tail(
    existing: ParsedPlan,
    submitted: ParsedPlan,
    current_step: int,
    upsert_action: dict[str, Any],
) -> None:
    """Merge the platform's immutable prefix with the model's tail.

    - Platform controls steps 1 through ``current_step - 1``
    - Model controls ``current_step`` and above (may update
      the current step's sub-steps to reflect joseki selection)

    Always strips prefix steps from the model's submission and
    prepends the platform's authoritative prefix.
    """
    prefix_lines: list[str] = []
    for step in existing.steps:
        if step.number < current_step:
            prefix_lines.extend(step.lines)
            prefix_lines.append("")

    tail_lines: list[str] = []
    for step in submitted.steps:
        if step.number >= current_step:
            if tail_lines:
                tail_lines.append("")
            tail_lines.extend(step.lines)

    merged_content = "\n".join(prefix_lines + tail_lines)
    upsert_action["arguments"]["content"] = merged_content

    logger.info(
        "PLANNING_EXTENSION: Merged prefix (plan steps 1-%d) with "
        "model tail (plan steps %d+)",
        current_step - 1, current_step,
    )


def validate_planning_extension_content(
    ctx: PromptContext,
    actions: list[dict[str, Any]],
) -> None:
    """Auto-merge planning-extension upsert_plan content.

    When the current step is a planning-extension step, the model
    emits an explicit ``upsert_plan`` with authored content.  The
    model controls the current step and everything after it (the
    tail).  The platform controls everything before the current
    step (the immutable prefix).

    This function always strips prefix steps from the model's
    submission and prepends the platform's authoritative prefix.
    """
    extension = extract_planning_extension(ctx, actions)
    if extension is None:
        return

    existing, submitted, _, current_number, upsert_action = extension

    merge_prefix_with_model_tail(
        existing, submitted, current_number, upsert_action,
    )


# ---------------------------------------------------------------------------
# Job ID extraction
# ---------------------------------------------------------------------------


def extract_job_id_from_context(
    state: dict[str, Any],
    action_params: dict[str, Any],
    raw_params: dict[str, Any] | None = None,
) -> str | None:
    """Return job_id from state, action params, or raw params."""
    candidate = state.get("job_id") or action_params.get("job_id")

    if not candidate and raw_params:
        raw_state = raw_params.get("state", {})
        if isinstance(raw_state, dict):
            candidate = raw_state.get("job_id")
        if not candidate:
            raw_nested = raw_params.get("params", {})
            if isinstance(raw_nested, dict):
                candidate = raw_nested.get("job_id")

    return str(candidate) if isinstance(candidate, str) and candidate else None


# ---------------------------------------------------------------------------
# Action parameter extraction
# ---------------------------------------------------------------------------


def extract_action_parameters(
    params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Extract action name and parameters from request.

    Expects flat structure from ActionProcessor:
    ``{"model": {...}, "prompt": {...}}``.

    FAIL-FAST: No fallback formats, no nested ``"params"`` wrapper support.

    Returns:
        Tuple of ``(action_name, action_params)``.

    Raises:
        RuntimeError: If model or prompt missing from params.
    """
    action_name = params.get("action_name", "inference_request")

    if "model" not in params and "prompt" not in params:
        raise RuntimeError(
            "Missing 'model' or 'prompt' in action parameters. "
            f"Expected flat structure: keys={list(params.keys())}"
        )

    return str(action_name), params


# ---------------------------------------------------------------------------
# User prompt validation
# ---------------------------------------------------------------------------


def require_user_prompt(ctx: PromptContext) -> str:
    """Extract user prompt text from pipeline context.

    Raises:
        RuntimeError: If ``ctx.user_prompt`` is falsy.
    """
    user_prompt: str | None = ctx.user_prompt
    if not user_prompt:
        raise RuntimeError("ctx.user_prompt is required for context tracking")
    return user_prompt


# ---------------------------------------------------------------------------
# State logging
# ---------------------------------------------------------------------------


def log_request_to_state(
    state: dict[str, Any],
    action_name: str,
    model_info: dict[str, Any],
    request_messages: list[dict[str, str]],
    request_temperature: float,
    request_max_tokens: int,
    resolved_user_input: str | None,
    state_service: StateServiceProtocol | None,
) -> None:
    """Log inference request to state for debugging and auditing."""
    if not state_service:
        return

    try:
        request_log: dict[str, object] = {
            "action_type": "inference_request",
            "timestamp": state.get("timestamp"),
            "session_id": state.get("session_id"),
            "flow_id": state.get("flow_id"),
            "action_name": action_name,
            "model": model_info.get("name"),
            "temperature": request_temperature,
            "max_tokens": request_max_tokens,
            "prompt": request_messages,
            "user_input": resolved_user_input,
        }
        state_service.write_state(namespace="inference_requests", data=request_log)
        logger.debug("Logged inference request to state")
    except Exception:
        logger.exception("Failed to log inference request to state")


def log_response_to_state(
    state: dict[str, Any],
    action_name: str,
    model_info: dict[str, Any],
    completion_text: str,
    result_data: dict[str, Any],
    state_service: StateServiceProtocol | None,
) -> None:
    """Log inference response to state for debugging and auditing."""
    if not state_service:
        return

    try:
        response_log: dict[str, object] = {
            "action_type": "inference_response",
            "timestamp": state.get("timestamp"),
            "session_id": state.get("session_id"),
            "flow_id": state.get("flow_id"),
            "action_name": action_name,
            "model": model_info.get("name"),
            "completion_text": completion_text,
            "full_result": result_data,
        }
        state_service.write_state(namespace="inference_responses", data=response_log)
        logger.debug("Logged inference response to state")
    except Exception:
        logger.exception("Failed to log inference response to state")


# ---------------------------------------------------------------------------
# Response construction
# ---------------------------------------------------------------------------


def create_success_response(
    content_data: Any,
    actions_to_execute: list[dict[str, Any]],
    result: dict[str, Any],
) -> ActionResult:
    """Create successful inference response."""
    timestamp = result.get("timestamp", datetime.now(UTC).isoformat())
    timestamp_str = str(timestamp) if timestamp else datetime.now(UTC).isoformat()
    return ActionResult(
        action_status=ActionStatus.COMPLETED.value,
        data={"result": content_data},
        actions=actions_to_execute,
        error=None,
        timestamp=timestamp_str,
    )


def create_inference_error_response(
    error: Exception,
    action_name: str,
    state: dict[str, Any],
    io_process_key: str | None,
) -> ActionResult:
    """Create error response with injected post_message action.

    Args:
        error: The exception that caused the failure.
        action_name: Name of the action that failed.
        state: Runtime state dict (for session_id, flow_id).
        io_process_key: Resolved IO plugin process key for post_message,
            or ``None`` if unavailable.
    """
    response_dict: dict[str, object]
    if isinstance(error, AnantaError):
        response_dict = error.to_response(action_name)
        message = f"{'Inference' if not _is_system_error(error) else 'System'} error: {error.message}"
        error_code: str = error.error_code
    else:
        logger.error("Error executing action %s: %s", action_name, error, exc_info=True)
        error_code = type(error).__name__
        response_dict = {
            "action_status": ActionStatus.ERROR.value,
            "error": {
                "code": error_code,
                "message": str(error),
                "action": action_name,
            },
            "timestamp": datetime.now(UTC).isoformat(),
        }
        message = f"Unexpected error: {error!s}"

    error_detail = response_dict.get("error")

    actions_list: list[dict[str, object]] = []
    if io_process_key:
        actions_list.append(
            {
                "name": "post_message",
                "process_key": io_process_key,
                "arguments": {"message": message},
                "notes": f"Error message: {error_code}",
                "session_id": state.get("session_id"),
                "flow_id": state.get("flow_id"),
            }
        )

    timestamp_value = response_dict.get("timestamp")
    timestamp_str = str(timestamp_value) if timestamp_value else datetime.now(UTC).isoformat()

    raw_data = response_dict.get("data", {})
    data: dict[str, object] = raw_data if isinstance(raw_data, dict) else {}
    return ActionResult(
        action_status=ActionStatus.ERROR.value,
        data=data,
        actions=actions_list,
        error=error_detail if isinstance(error_detail, dict) else None,  # type: ignore[typeddict-item]
        timestamp=timestamp_str,
    )


def _is_system_error(error: AnantaError) -> bool:
    """Distinguish system errors from plugin errors for message wording."""
    from ananta.error_handling import PluginError

    return not isinstance(error, PluginError)


def resolve_inference_params(
    defaults: InferenceDefaults,
    output_schema: dict[str, object] | None,
    model_info: dict[str, Any],
    api_payload: dict[str, Any],
) -> tuple[float, int]:
    """Resolve temperature and max_tokens for an inference request.

    Resolution order per parameter:
    1. Explicit value in ``model_info`` (action template override)
    2. Value in ``api_payload`` (pipeline-set)
    3. Provider default from ``defaults``

    Action vertices (schema requires ``"actions"``) get low temperature
    and capped max_tokens unless ``model_info`` explicitly overrides.
    """
    temperature: float = model_info.get(
        "temperature",
        api_payload.get("temperature", defaults.temperature),
    )
    max_tokens: int = model_info.get(
        "max_tokens",
        api_payload.get("max_tokens", defaults.max_tokens),
    )

    schema_required = (output_schema or {}).get("required", [])
    if not isinstance(schema_required, list) or "actions" not in schema_required:
        return temperature, max_tokens

    if "temperature" not in model_info:
        temperature = defaults.action_vertex_temperature
    if "max_tokens" not in model_info:
        max_tokens = defaults.action_vertex_max_tokens

    return temperature, max_tokens


def validate_model_config(
    model_info: dict[str, Any],
    configured_model: str,
) -> None:
    """Validate model name and inject configured model if missing.

    - If ``model.name`` is omitted: inject ``configured_model``
    - If ``model.name`` is ``"default"`` or ``"None"``: raise (ambiguous)
    - If ``model.name`` differs from ``configured_model``: raise (mismatch)
    """
    requested = model_info.get("name")
    if requested in ("default", "None"):
        raise ValueError(
            f"model.name '{requested}' is ambiguous — "
            "omit for configured model or specify exact name"
        )
    if requested is not None and str(requested) != configured_model:
        raise ValueError(
            f"Model mismatch: '{requested}' vs provider '{configured_model}'"
        )
    if requested is None:
        model_info["name"] = configured_model


def _validate_action_params(action_params: dict[str, Any]) -> dict[str, Any]:
    """Validate and return model_info from action parameters. Fail-fast."""
    model_info = action_params.get("model")
    if model_info is None:
        raise ValueError("Missing 'model' in action parameters")
    if not isinstance(model_info, dict):
        raise ValueError(f"model must be dict, got {type(model_info).__name__}")

    prompt_info = action_params.get("prompt")
    if not prompt_info:
        raise ValueError("Missing or empty 'prompt' in action parameters")
    if isinstance(prompt_info, str) and not prompt_info.strip():
        raise ValueError("Empty string prompt provided")
    if isinstance(prompt_info, dict):
        if "user" not in prompt_info and "system" not in prompt_info:
            raise ValueError(
                f"Prompt dict must contain 'user' or 'system' key, "
                f"got: {list(prompt_info.keys())}"
            )
    elif not isinstance(prompt_info, str):
        raise ValueError(f"Unsupported prompt type: {type(prompt_info).__name__}")
    return model_info


def prepare_inference_request(
    action_params: dict[str, Any],
    action_name: str,
    state: dict[str, Any],
    defaults: InferenceDefaults,
    pipeline_factory: Any,
    *,
    context_id: str | None = None,
) -> tuple[
    InferenceRequest,
    dict[str, Any],
    str | None,
    str | None,
    dict[str, Any],
    PromptContext,
]:
    """Build an InferenceRequest via the prompt assembly pipeline.

    Platform-owned orchestration: validates parameters, resolves IO
    namespace from state, runs assembly pipeline, resolves inference
    params, and constructs the typed request.

    Returns ``(request, model_info, resolved_user_input, context_id,
    resolved_action_params, prompt_ctx)``.
    """
    from ananta.core.prompts.profiles import INFERENCE_PROFILE
    from ananta.interfaces.inference_service_interface import InferenceRequest
    from ananta.services.inference_service.assembly import (
        assemble_prompt as _assemble,
    )
    from ananta.services.inference_service.assembly_types import PromptAssemblyRequest

    model_info = _validate_action_params(action_params)

    normalized_flow_id = normalize_flow_id(state.get("flow_id"))
    normalized_session_id = normalize_session_id(state.get("session_id"))

    io_namespace = _resolve_io_namespace(state)

    assembly_request = PromptAssemblyRequest(
        profile_name="inference",
        flow_id=normalized_flow_id or "",
        action_name=action_name,
        session_id=normalized_session_id or "",
        raw_action_params=action_params,
        context_id=context_id,
        io_namespace=io_namespace,
    )
    assembly_result = _assemble(assembly_request, INFERENCE_PROFILE, pipeline_factory)
    if assembly_result.prompt_context is None:
        raise ValueError("Assembly returned no prompt context")
    ctx = assembly_result.prompt_context

    temperature, max_tokens = resolve_inference_params(
        defaults, ctx.output_schema, model_info, ctx.api_payload,
    )
    resolved_user_input = require_user_prompt(ctx)

    request = InferenceRequest(
        prompt=list(assembly_result.messages),
        temperature=temperature,
        max_tokens=max_tokens,
        context_metadata={
            "session_id": state.get("session_id"),
            "flow_id": state.get("flow_id"),
            "action_name": action_name,
            "_pipeline_context_injected": True,
        },
        response_schema=assembly_result.output_schema,
        use_structured_output=False,
    )
    return request, model_info, resolved_user_input, context_id, ctx.resolved_action_params, ctx


def _resolve_io_namespace(state: dict[str, Any]) -> str:
    """Extract IO plugin namespace from state's io_process_key.

    The io_process_key is set by ``inference_transaction.execute`` via
    flow-service lookup before this function is called.

    Raises:
        RuntimeError: If io_process_key is missing or malformed.
    """
    io_key = state.get("io_process_key")
    if not io_key:
        raise RuntimeError(
            "io_process_key missing in state — "
            "inference_transaction must resolve it before calling prepare_inference_request"
        )
    if not isinstance(io_key, str):
        raise TypeError(
            f"io_process_key must be str, got {type(io_key).__name__}"
        )
    parts = io_key.split("::")
    if len(parts) < 3:
        raise ValueError(
            f"io_process_key format invalid (expected plugin::namespace::function): {io_key}"
        )
    return parts[1]
