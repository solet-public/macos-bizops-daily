"""Prompt assembly profiles — named configurations for pipeline runs.

Profiles use stable stage keys, not raw stage class types. The pipeline
factory resolves keys to configured stage instances using the dependency
bundle. This prevents callers from forking stage construction.

Four profiles:
- ``INFERENCE_PROFILE``: Full pipeline for the main inference model.
- ``THINKING_PROFILE``: Reduced pipeline for the thinking/planning model.
- ``THINKING_ARTIFACT_PROFILE``: Artifact authoring via the thinking model.
- ``TEXT_COMPLETION_PROFILE``: Minimal pipeline for text completion.
"""

from __future__ import annotations

from dataclasses import dataclass

from ananta.services.inference_service.assembly_types import (
    LM_STUDIO_SPEC,
    SerializationSpec,
)


@dataclass(frozen=True)
class PromptAssemblyProfile:
    """Named configuration for a prompt assembly pipeline run.

    Profiles use stable stage keys, not raw stage class types. The pipeline
    factory resolves keys to configured stage instances using the dependency
    bundle. This prevents callers from forking stage construction.
    """

    name: str
    stage_keys: tuple[str, ...]
    serialization_spec: SerializationSpec
    include_conversation_history: bool
    include_focused_memories: bool
    include_semantic_recall: bool
    include_process_catalog: bool
    include_step_guidance: bool
    include_decode_contract: bool
    max_context_messages: int | None


# ── Standard profiles ────────────────────────────────────────────────

INFERENCE_PROFILE = PromptAssemblyProfile(
    name="inference",
    stage_keys=(
        "template", "format", "context", "plan_state",
        "catalog", "guidance", "decode_contract", "api",
    ),
    serialization_spec=LM_STUDIO_SPEC,
    include_conversation_history=True,
    include_focused_memories=True,
    include_semantic_recall=True,
    include_process_catalog=True,
    include_step_guidance=True,
    include_decode_contract=True,
    max_context_messages=None,
)

THINKING_PROFILE = PromptAssemblyProfile(
    name="thinking",
    stage_keys=("template", "format", "context", "api"),
    serialization_spec=LM_STUDIO_SPEC,
    include_conversation_history=False,
    include_focused_memories=True,
    include_semantic_recall=False,
    include_process_catalog=False,
    include_step_guidance=False,
    include_decode_contract=False,
    max_context_messages=None,
)

THINKING_ARTIFACT_PROFILE = PromptAssemblyProfile(
    name="thinking_artifact",
    stage_keys=("template", "context", "artifact_prompt", "api"),
    serialization_spec=LM_STUDIO_SPEC,
    include_conversation_history=False,
    include_focused_memories=True,
    include_semantic_recall=False,
    include_process_catalog=False,
    include_step_guidance=False,
    include_decode_contract=False,
    max_context_messages=None,
)

TEXT_COMPLETION_PROFILE = PromptAssemblyProfile(
    name="text_completion",
    stage_keys=("template", "format", "context", "api"),
    serialization_spec=LM_STUDIO_SPEC,
    include_conversation_history=False,
    include_focused_memories=False,
    include_semantic_recall=False,
    include_process_catalog=False,
    include_step_guidance=False,
    include_decode_contract=False,
    max_context_messages=None,
)

# Phase 2 — the retrieval/provenance-first briefing profile for
# ``context_service::assemble_agent_context``. Runs the SAME bundle-producing
# stages as INFERENCE_PROFILE (catalog + plan state + guidance + decode contract
# + conversation/memory context) but OMITS the ``api`` serialization stage: the
# briefing consumes ``ctx.message_blocks`` + ``ctx.output_schema`` as structured
# DATA (grouped into named bundles with provenance), never a serialized model
# message array. ``max_context_messages=None`` — a briefing service, not a prompt
# shrinker; window-fitting is opt-in via the verb's ``budget`` argument, not
# baked into the profile. ``serialization_spec`` is unused (no ``api`` stage) but
# the dataclass requires a value.
AGENT_CONTEXT_PROFILE = PromptAssemblyProfile(
    name="agent_context",
    stage_keys=(
        "template", "format", "context", "plan_state",
        "catalog", "guidance", "decode_contract",
    ),
    serialization_spec=LM_STUDIO_SPEC,
    include_conversation_history=True,
    include_focused_memories=True,
    include_semantic_recall=True,
    include_process_catalog=True,
    include_step_guidance=True,
    include_decode_contract=True,
    max_context_messages=None,
)
