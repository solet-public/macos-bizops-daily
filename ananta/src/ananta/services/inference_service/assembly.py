"""Prompt assembly service — runs the pipeline and returns structured results.

This module provides the ``assemble_prompt`` function that both the
inference plugin and external callers (like the thinking plugin) use
to produce prompt messages through the shared pipeline.

Two modes:
1. **Pipeline mode** (default): constructs a pipeline via
   ``PromptPipelineFactory``, executes it, serializes blocks.
2. **Pre-built mode**: when ``request.pre_built_messages`` is set,
   applies only the serialization spec (role merge, system consolidation)
   without running the pipeline.  Used by callers that build
   domain-specific messages directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ananta.core.prompts.api_stage.serialization import (
    order_blocks,
    serialize_blocks_with_spec,
)
from ananta.services.inference_service.assembly_types import (
    PromptAssemblyRequest,
    PromptAssemblyResult,
)

# Unicode smart-quote characters that break LM Studio's constrained
# decoding grammar compiler.  Replaced with ASCII equivalents before
# messages are sent to the inference provider.
_SMART_QUOTE_TABLE = str.maketrans({
    "\u2018": "'",   # left single curly quote
    "\u2019": "'",   # right single curly quote
    "\u201c": '"',   # left double curly quote
    "\u201d": '"',   # right double curly quote
})

if TYPE_CHECKING:
    from ananta.core.prompts.pipeline_factory import PromptPipelineFactory
    from ananta.core.prompts.profiles import PromptAssemblyProfile

logger = logging.getLogger(__name__)


def assemble_prompt(
    request: PromptAssemblyRequest,
    profile: PromptAssemblyProfile,
    factory: PromptPipelineFactory,
) -> PromptAssemblyResult:
    """Run the prompt pipeline and return a structured assembly result.

    When ``request.pre_built_messages`` is set, the pipeline is bypassed
    and only serialization rules are applied.

    Args:
        request: Input contract with identity, parameters, and overrides.
        profile: Named configuration controlling stage selection and
            serialization behavior.
        factory: Configured factory that resolves stage keys to instances.

    Returns:
        ``PromptAssemblyResult`` with serialized messages, semantic blocks,
        output schema, and assembly manifest.
    """
    spec = request.serialization_spec or profile.serialization_spec

    if request.pre_built_messages is not None:
        return _assemble_from_pre_built(request, profile, spec)

    return _assemble_via_pipeline(request, profile, factory, spec)


def _assemble_from_pre_built(
    request: PromptAssemblyRequest,
    profile: PromptAssemblyProfile,
    spec: object,
) -> PromptAssemblyResult:
    """Assemble from pre-built messages — apply serialization only.

    Applies the spec's same-role merge and system consolidation rules
    to the pre-built messages.
    """
    from ananta.core.prompts.api_stage.serialization import (
        consolidate_system_messages,
        merge_adjacent_same_role,
    )
    from ananta.services.inference_service.assembly_types import SerializationSpec

    raw_pairs = [
        (m.get("role", "user"), m.get("content", ""))
        for m in request.pre_built_messages or ()
    ]

    # Pre-built thinking prompts have deliberately structured message
    # boundaries (separate assistant messages for manifest, sketch,
    # guidance, specification, etc.).  Do not merge them — the artifact
    # authoring code already built the correct message array.
    if isinstance(spec, SerializationSpec) and not spec.supports_multiple_system_messages:
        merged = merge_adjacent_same_role(raw_pairs)
        merged = consolidate_system_messages(merged)
    else:
        merged = raw_pairs

    messages = tuple({"role": r, "content": c} for r, c in merged)

    logger.info(
        "ASSEMBLY: profile=%s (pre-built), messages=%d → %d after merge",
        profile.name, len(request.pre_built_messages or ()), len(messages),
    )

    return PromptAssemblyResult(
        messages=messages,
        output_schema=None,
        semantic_blocks=(),
        profile_name=profile.name,
        assembly_manifest={"mode": "pre_built", "raw_count": len(request.pre_built_messages or ())},
    )


def _assemble_via_pipeline(
    request: PromptAssemblyRequest,
    profile: PromptAssemblyProfile,
    factory: PromptPipelineFactory,
    spec: object,
) -> PromptAssemblyResult:
    """Assemble via the full pipeline — execute stages and serialize blocks."""
    from ananta.services.inference_service.assembly_types import SerializationSpec

    pipeline = factory.create_pipeline(profile)

    ctx, manifest = pipeline.execute(
        flow_id=request.flow_id,
        action_name=request.action_name,
        session_id=request.session_id,
        action_params=request.raw_action_params,
        context_id=request.context_id,
        io_namespace=request.io_namespace,
        include_conversation_history=profile.include_conversation_history,
        include_focused_memories=profile.include_focused_memories,
        include_semantic_recall=profile.include_semantic_recall,
        max_context_messages=profile.max_context_messages,
    )

    ordered = order_blocks(ctx.message_blocks)

    if isinstance(spec, SerializationSpec):
        messages = serialize_blocks_with_spec(ctx.message_blocks, spec)
    else:
        messages = [{"role": b.prompt_role, "content": b.content} for b in ordered]

    # Sanitize smart quotes that break LM Studio's constrained decoding.
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str) and any(c in content for c in "\u2018\u2019\u201c\u201d"):
            msg["content"] = content.translate(_SMART_QUOTE_TABLE)

    result = PromptAssemblyResult(
        messages=tuple(messages),
        output_schema=ctx.output_schema,
        semantic_blocks=tuple(ordered),
        profile_name=profile.name,
        assembly_manifest=manifest.to_dict() if hasattr(manifest, "to_dict") else {},
        prompt_context=ctx,
    )

    logger.info(
        "ASSEMBLY: profile=%s, blocks=%d, messages=%d",
        profile.name, len(ordered), len(messages),
    )
    return result
