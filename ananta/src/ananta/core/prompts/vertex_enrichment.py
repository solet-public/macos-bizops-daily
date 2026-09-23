"""Vertex enrichment — trailer reminders and action shape descriptions.

Enriches the user prompt for non-initial inference turns with:
1. Trailer reminder (platform appends metadata to assistant messages)
2. Valid action shapes (provider_type/provider/function_name guidance)

Extracted from the inference plugin — all logic is pure functions
over ``PromptContext`` data with no plugin instance state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.core.prompts.context import PromptContext

# ── Action shape registry ──
# Maps function_name → (provider_type, provider, required_args, optional_args)
ACTION_SHAPES: dict[str, tuple[str, str, str, str]] = {
    "post_message": ("plugin", "<io>", "session_id and message", "attachments"),
    "search": ("service_interface", "knowledge_service", "query", ""),
    "create_extended_plan": ("service_interface", "thinking_service", "goal", ""),
    "upsert_plan": ("service_interface", "thinking_service", "content", ""),
    "recall": ("service_interface", "memory_service", "query", ""),
    "query_process_registry": ("service_interface", "discovery_service", "query", ""),
}

_TRAILER_REMINDER = (
    "Persisted assistant messages in conversation history include a "
    "JSON metadata trailer appended by the platform. When you call "
    "post_message, `arguments.message` must be pure user-visible "
    "content only; do not include any metadata trailer or embedded JSON."
)

_BASE_INSTRUCTION_MARKER = "An empty actions array ends the flow silently."


def enrich_vertex(ctx: PromptContext) -> None:
    """Enrich ctx.user_prompt for non-initial turns.

    Adds trailer reminder and action shape descriptions.
    Mutates ``ctx.user_prompt`` in place.
    """
    if not _should_enrich(ctx):
        return

    content = ctx.user_prompt
    if not content:
        return

    io_namespace = ctx.io_namespace
    content = _inject_trailer_reminder(ctx, io_namespace, content)
    shapes = _build_action_shapes(ctx, io_namespace)
    if shapes:
        content = content + "\n\n" + shapes

    ctx.user_prompt = content


def _should_enrich(ctx: PromptContext) -> bool:
    """Check if the current vertex should be enriched."""
    if not ctx.output_schema:
        return False
    if "actions" not in ctx.output_schema.get("required", []):
        return False
    if not ctx.tool_observation:
        return False
    if ctx.has_focused_plan:
        return False
    return not _is_delivery_confirmation(ctx)


def _is_delivery_confirmation(ctx: PromptContext) -> bool:
    """Check if the vertex is a delivery confirmation (post_message result)."""
    if not ctx.tool_observation:
        return False
    obs = ctx.tool_observation.lower()
    return "post_message" in obs and ("message posted" in obs or "message delivered" in obs)


def _is_process_error_vertex(ctx: PromptContext) -> bool:
    """Check if the vertex is a process_error (tool failure)."""
    if not ctx.raw_observation_dict:
        return False
    action_result = ctx.raw_observation_dict.get("action_result")
    if not isinstance(action_result, dict):
        return False
    status = action_result.get("action_status", "")
    return status in ("error", "failed")


def _extract_function_name_enum(
    output_schema: dict[str, Any] | None,
) -> list[str]:
    """Extract function_name enum values from an output schema."""
    pprops = _extract_schema_process_props(output_schema)
    fn = pprops.get("function_name")
    return fn.get("enum", []) if isinstance(fn, dict) else []


def _extract_schema_process_props(
    output_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Navigate to process properties inside an action schema."""
    if not output_schema:
        return {}
    actions = output_schema.get("properties", {}).get("actions", {})
    items = actions.get("items", {}) if isinstance(actions, dict) else {}
    props = items.get("properties", {}) if isinstance(items, dict) else {}
    process = props.get("process", {}) if isinstance(props, dict) else {}
    return process.get("properties", {}) if isinstance(process, dict) else {}


def _schema_allows_post_message(
    output_schema: dict[str, Any] | None,
    io_namespace: str | None,
) -> bool:
    """Check whether the schema allows IO post_message."""
    if not io_namespace:
        return False
    pprops = _extract_schema_process_props(output_schema)
    pt_enum = pprops.get("provider_type", {}).get("enum", [])
    fn_enum = pprops.get("function_name", {}).get("enum", [])
    pv_enum = pprops.get("provider", {}).get("enum", [])
    if "plugin" not in pt_enum or "post_message" not in fn_enum:
        return False
    return not pv_enum or io_namespace in pv_enum


def _inject_trailer_reminder(
    ctx: PromptContext,
    io_namespace: str | None,
    content: str,
) -> str:
    """Insert trailer reminder after base instructions if applicable."""
    if _is_process_error_vertex(ctx):
        return content
    if not _schema_allows_post_message(ctx.output_schema, io_namespace):
        return content
    if _BASE_INSTRUCTION_MARKER not in content:
        return content + "\n\n" + _TRAILER_REMINDER
    idx = content.index(_BASE_INSTRUCTION_MARKER) + len(_BASE_INSTRUCTION_MARKER)
    return content[:idx] + "\n\n" + _TRAILER_REMINDER + content[idx:]


def _resolve_display_name(
    fn_name: str,
    ptype: str,
    provider: str,
    *,
    is_post_observation: bool,
) -> str:
    """Resolve provider-aware display name for an action shape."""
    if ptype == "plugin" or (is_post_observation and provider != "knowledge_service"):
        return fn_name
    return f"{provider}::{fn_name}"


def _build_action_shapes(
    ctx: PromptContext,
    io_namespace: str | None,
) -> str:
    """Build 'Valid action shapes at this vertex:' from output schema enums."""
    if not ctx.output_schema:
        return ""
    fn_enum = _extract_function_name_enum(ctx.output_schema)
    if not fn_enum:
        return ""

    ns = io_namespace or "io_plugin"
    is_post_obs = ctx.tool_observation is not None
    lines = ["Valid action shapes at this vertex:"]

    for fn_name in fn_enum:
        shape = ACTION_SHAPES.get(fn_name)
        if not shape:
            continue
        ptype, provider, req_args, opt_args = shape
        if provider == "<io>":
            provider = ns
        display = _resolve_display_name(
            fn_name, ptype, provider, is_post_observation=is_post_obs,
        )
        if fn_name == "create_extended_plan" and is_post_obs:
            req_args = "goal"
            opt_args = ""
        arg_text = f"{req_args} ({opt_args} optional)" if opt_args else req_args
        lines.append(
            f"- {display}: provider_type={ptype}, provider={provider}, "
            f"function_name={fn_name}; arguments include {arg_text}."
        )

    return "\n".join(lines)
