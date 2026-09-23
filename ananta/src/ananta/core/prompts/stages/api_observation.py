"""Observation handling for APIStage.

Functions extracted from APIStage for reading rendering config,
validating fields, rendering observation content, extracting
process keys, and building metadata trailers.
"""

from __future__ import annotations

import json
from typing import Any

from ananta.core.prompts.context import (
    ALLOWED_CONTEXT_LAYERS,
    ALLOWED_HISTORY_KINDS,
    ALLOWED_PROMPT_ROLES,
    ALLOWED_REASONING_SLOTS,
    ALLOWED_TRANSITION_BEHAVIORS,
    PromptContext,
)
from ananta.core.prompts.renderers import get_renderer


def get_observation_rendering(ctx: PromptContext) -> dict[str, Any]:
    """Read message_rendering from resolved action params (Section 15).

    Returns the rendering contract dict, or empty dict if not present.
    """
    prompt_part = ctx.resolved_action_params.get("prompt", {})
    if not isinstance(prompt_part, dict):
        return {}
    rendering = prompt_part.get("message_rendering", {})
    return rendering if isinstance(rendering, dict) else {}


def validate_rendering_fields(rendering: dict[str, Any]) -> None:
    """Validate message_rendering values against closed vocabularies.

    Fails fast on typos in process registry JSON before they propagate
    into block creation.
    """
    checks: tuple[tuple[str, frozenset[str]], ...] = (
        ("context_layer", ALLOWED_CONTEXT_LAYERS),
        ("reasoning_slot", ALLOWED_REASONING_SLOTS),
        ("prompt_role", ALLOWED_PROMPT_ROLES),
        ("history_kind", ALLOWED_HISTORY_KINDS),
        ("transition_behavior", ALLOWED_TRANSITION_BEHAVIORS),
    )
    for field_name, allowed in checks:
        value = rendering.get(field_name)
        if value is not None and value not in allowed:
            msg = (
                f"message_rendering.{field_name}='{value}' is not in "
                f"allowed values {sorted(allowed)}"
            )
            raise ValueError(msg)


def render_observation_content(
    ctx: PromptContext,
    rendering: dict[str, Any],
) -> str | None:
    """Produce formatted observation content via prompt-layer renderer.

    When ``rendering`` contains a ``renderer_key`` and ``ctx.raw_observation_dict``
    is available, the corresponding renderer function formats the raw result
    data into prompt-ready text.  Otherwise falls back to the template-rendered
    ``ctx.tool_observation``.
    """
    import logging

    renderer_key = rendering.get("renderer_key") if rendering else None
    if not renderer_key or not ctx.raw_observation_dict:
        return ctx.tool_observation

    renderer_fn = get_renderer(str(renderer_key))
    action_result = ctx.raw_observation_dict.get("action_result")
    raw_data = (
        action_result if isinstance(action_result, dict)
        else ctx.raw_observation_dict
    )
    rendered = renderer_fn(raw_data)

    if rendered is not None:
        logger = logging.getLogger(__name__)
        logger.info(
            "RENDERER: %s produced %d chars from raw_observation_dict",
            renderer_key, len(rendered),
        )
        return rendered

    # Renderer returned None (empty result set) -- fall back to template text
    return ctx.tool_observation


def observation_process_key(ctx: PromptContext) -> str:
    """Extract process_key from the raw observation dict."""
    if ctx.raw_observation_dict:
        pk = ctx.raw_observation_dict.get("process_key", "")
        if isinstance(pk, str):
            return pk
    return "unknown"


def build_live_observation_trailer(ctx: PromptContext) -> str:
    """Build JSON metadata trailer for the live observation message.

    Mirrors the trailer format produced by ContextStage for persisted
    OUTPUT events so that observation messages have consistent structure
    on both initial injection and history reload.
    """
    trailer: dict[str, str] = {}

    if ctx.raw_observation_dict:
        pk = ctx.raw_observation_dict.get("process_key", "")
        if isinstance(pk, str) and pk:
            trailer["namespace"] = pk

    from datetime import UTC, datetime
    trailer["posted_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # session_id omitted — AQP injects it post-inference. Certain random
    # session_id strings cause grammar-compiler explosions in LM Studio.

    if not trailer:
        return ""
    return json.dumps(trailer, separators=(",", ":"))


def build_live_input_trailer(ctx: PromptContext) -> str:
    """Build JSON metadata trailer for the live user input message.

    Mirrors the trailer format produced by ContextStage for persisted
    INPUT events: {namespace, source, posted_at, session_id}.
    """
    prompt_part = ctx.resolved_action_params.get("prompt", {})
    user_part = (
        prompt_part.get("user", {}) if isinstance(prompt_part, dict) else {}
    )
    flow_input = (
        user_part.get("flow_input", {}) if isinstance(user_part, dict) else {}
    )
    if not isinstance(flow_input, dict):
        return ""

    trailer: dict[str, str] = {}
    namespace = flow_input.get("source_namespace", "")
    if namespace:
        trailer["namespace"] = str(namespace)
    source = flow_input.get("source", "")
    if source:
        trailer["source"] = str(source)

    from datetime import UTC, datetime
    trailer["posted_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # session_id omitted — AQP injects it post-inference. Certain random
    # session_id strings cause grammar-compiler explosions in LM Studio.

    if not trailer:
        return ""
    return json.dumps(trailer, separators=(",", ":"))
