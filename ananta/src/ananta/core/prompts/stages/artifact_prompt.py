"""ArtifactPromptStage - Converts structured artifact prompt payloads into MessageBlocks.

The artifact_prompt stage validates an ``artifact_prompt`` payload from
``raw_action_params`` and writes deterministic MessageBlock objects to
``ctx.message_blocks``.  It also filters and compacts supplemental focused
memories using the same helpers as the main inference pipeline.

Block ordering within the stage follows the thinking artifact message
block order specified in the architecture:

1. ``ossified_context.static_frame``: artifact system frame.
2. ``living_context.working_state``: required parent dependencies.
3. ``living_context.working_state``: prior thinking outputs.
4. ``living_context.working_state``: supplemental focused memories.
5. ``living_context.working_evidence``: artifact guidance and support articles.
6. ``living_context.synthetic_driver``: directive, output contract, reinforcement.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ananta.core.prompts.context import (
    ACTIVE_PLAN_MARKER,
    MessageBlock,
    PromptContext,
    SourceReference,
)
from ananta.core.prompts.stages.api_focus import (
    compact_focused_memories,
    format_focused_parts,
)

logger = logging.getLogger(__name__)

ARTIFACT_PROMPT_KEY = "artifact_prompt"


def _block_id() -> str:
    return f"art-{uuid.uuid4().hex[:12]}"


def _require_field(payload: dict[str, Any], field: str) -> Any:
    """Extract a required field, raising ValueError if missing."""
    value = payload.get(field)
    if value is None:
        raise ValueError(
            f"artifact_prompt payload missing required field: {field!r}"
        )
    return value


def _build_system_frame_block(system_frame: str) -> MessageBlock:
    """Build the system frame block (ossified_context.static_frame)."""
    return MessageBlock(
        block_id=_block_id(),
        context_layer="ossified_context",
        reasoning_slot="static_frame",
        ephemeral=False,
        history_kind="none",
        source_kind="system_template",
        subtype="artifact_system_frame",
        source_reference=SourceReference(kind="artifact_prompt", ref="system_frame"),
        transition_behavior="stable",
        content=system_frame,
        sequence=0,
    )


def _build_dependency_blocks(
    dependencies: list[dict[str, Any]],
) -> list[MessageBlock]:
    """Build blocks for required parent dependencies (living_context.working_state)."""
    blocks: list[MessageBlock] = []
    for i, dep in enumerate(dependencies):
        label = dep.get("label", dep.get("kind", "dependency"))
        content = dep.get("content", "")
        if not content:
            source = dep.get("source", "unknown")
            raise ValueError(
                f"Dependency {label!r} (source={source!r}) has empty content"
            )
        blocks.append(MessageBlock(
            block_id=_block_id(),
            context_layer="living_context",
            reasoning_slot="working_state",
            ephemeral=True,
            history_kind="none",
            source_kind="prompt_asset",
            subtype="artifact_dependency",
            source_reference=SourceReference(
                kind="artifact_dependency",
                ref=dep.get("source", label),
            ),
            transition_behavior="filtered",
            content=content,
            sequence=100 + i,
        ))
    return blocks


def _build_prior_output_blocks(
    prior_outputs: list[dict[str, Any]],
) -> list[MessageBlock]:
    """Build blocks for prior thinking outputs (living_context.working_state)."""
    blocks: list[MessageBlock] = []
    for i, output in enumerate(prior_outputs):
        label = output.get("label", f"prior_output_{i}")
        content = output.get("content", "")
        if not content:
            artifact_id = output.get("artifact_id", "unknown")
            raise ValueError(
                f"Prior output {label!r} (artifact_id={artifact_id!r}) has empty content"
            )
        order = output.get("order", i)
        blocks.append(MessageBlock(
            block_id=_block_id(),
            context_layer="living_context",
            reasoning_slot="working_state",
            ephemeral=True,
            history_kind="none",
            source_kind="model_output",
            subtype="artifact_prior_output",
            source_reference=SourceReference(
                kind="artifact_prior_output",
                ref=output.get("artifact_id", label),
            ),
            transition_behavior="filtered",
            content=content,
            sequence=200 + order,
        ))
    return blocks


def _build_focus_blocks(
    focused_memories: list[dict[str, Any]],
    loaded_dependency_refs: set[str],
    blocked_labels: frozenset[str],
    history_memory_ids: set[str],
) -> list[MessageBlock]:
    """Build supplemental focus blocks, filtered and compacted.

    Exclusion rules:
    - Exclude memories containing the ACTIVE_PLAN marker.
    - Exclude memories whose memory_id matches a loaded dependency.
    - Exclude memories whose label matches a blocked label.
    - Use compact_focused_memories and format_focused_parts for compaction.
    """
    filtered = [
        mem for mem in focused_memories
        if not _is_focus_memory_excluded(mem, loaded_dependency_refs, blocked_labels)
    ]
    if not filtered:
        return []
    compacted = compact_focused_memories(filtered)
    parts = format_focused_parts(compacted, history_memory_ids)
    return [
        MessageBlock(
            block_id=_block_id(),
            context_layer="living_context",
            reasoning_slot="working_state",
            ephemeral=True,
            history_kind="none",
            source_kind="focus_state",
            subtype="artifact_supplemental_focus",
            source_reference=SourceReference(
                kind="focus_buffer",
                ref=f"supplemental_{i}",
            ),
            transition_behavior="filtered",
            content=part,
            sequence=300 + i,
        )
        for i, part in enumerate(parts)
    ]


def _is_focus_memory_excluded(
    mem: dict[str, Any],
    loaded_dependency_refs: set[str],
    blocked_labels: frozenset[str],
) -> bool:
    """True if a memory should be skipped from supplemental focus."""
    content = mem.get("content", "")
    if isinstance(content, str) and ACTIVE_PLAN_MARKER in content:
        return True
    mem_id = str(mem.get("memory_id", ""))
    if mem_id and mem_id in loaded_dependency_refs:
        return True
    mem_tags = mem.get("tags", [])
    if isinstance(mem_tags, list) and any(tag in blocked_labels for tag in mem_tags):
        return True
    return False


def _build_guidance_blocks(
    guidance: list[dict[str, Any]],
    support_articles: list[dict[str, Any]],
) -> list[MessageBlock]:
    """Build blocks for guidance and support articles (living_context.working_evidence)."""
    blocks: list[MessageBlock] = []
    seq = 0
    for item in guidance:
        content = item.get("content", "")
        if not content:
            continue
        blocks.append(MessageBlock(
            block_id=_block_id(),
            context_layer="living_context",
            reasoning_slot="working_evidence",
            ephemeral=True,
            history_kind="none",
            source_kind="prompt_asset",
            subtype="artifact_guidance",
            source_reference=SourceReference(
                kind="guidance_article",
                ref=item.get("filename", f"guidance_{seq}"),
            ),
            transition_behavior="filtered",
            content=content,
            sequence=seq,
        ))
        seq += 1

    for item in support_articles:
        content = item.get("content", "")
        if not content:
            continue
        blocks.append(MessageBlock(
            block_id=_block_id(),
            context_layer="living_context",
            reasoning_slot="working_evidence",
            ephemeral=True,
            history_kind="none",
            source_kind="prompt_asset",
            subtype="artifact_support_article",
            source_reference=SourceReference(
                kind="support_article",
                ref=item.get("filename", f"support_{seq}"),
            ),
            transition_behavior="filtered",
            content=content,
            sequence=seq,
        ))
        seq += 1
    return blocks


def _build_driver_blocks(payload: dict[str, Any]) -> list[MessageBlock]:
    """Build directive and output contract blocks (living_context.synthetic_driver)."""
    blocks: list[MessageBlock] = []
    seq = 0

    directive = payload.get("directive", "")
    if directive:
        blocks.append(MessageBlock(
            block_id=_block_id(),
            context_layer="living_context",
            reasoning_slot="synthetic_driver",
            ephemeral=True,
            history_kind="none",
            source_kind="runtime_instruction",
            subtype="artifact_directive",
            source_reference=SourceReference(
                kind="artifact_prompt",
                ref="directive",
            ),
            transition_behavior="filtered",
            content=directive,
            sequence=seq,
        ))
        seq += 1

    output_contract = payload.get("output_contract", "")
    if output_contract:
        blocks.append(MessageBlock(
            block_id=_block_id(),
            context_layer="living_context",
            reasoning_slot="synthetic_driver",
            ephemeral=True,
            history_kind="none",
            source_kind="runtime_instruction",
            subtype="artifact_output_contract",
            source_reference=SourceReference(
                kind="artifact_prompt",
                ref="output_contract",
            ),
            transition_behavior="filtered",
            content=output_contract,
            sequence=seq,
        ))
        seq += 1

    reinforcement = payload.get("reinforcement", "")
    if reinforcement:
        blocks.append(MessageBlock(
            block_id=_block_id(),
            context_layer="living_context",
            reasoning_slot="synthetic_driver",
            ephemeral=True,
            history_kind="none",
            source_kind="runtime_instruction",
            subtype="artifact_reinforcement",
            source_reference=SourceReference(
                kind="artifact_prompt",
                ref="reinforcement",
            ),
            transition_behavior="filtered",
            content=reinforcement,
            sequence=seq,
        ))

    return blocks


class ArtifactPromptStage:
    """Converts structured artifact prompt payloads into MessageBlocks.

    Validates the payload, builds deterministic blocks for dependencies
    and prior outputs, filters/compacts supplemental focused memories,
    and adds guidance/driver blocks.
    """

    name = "artifact_prompt"

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Convert artifact_prompt payload into MessageBlocks on ctx."""
        payload = ctx.raw_action_params.get(ARTIFACT_PROMPT_KEY)
        if payload is None:
            ctx.add_decision(
                self.name,
                "No artifact_prompt payload in raw_action_params — skipped",
            )
            return ctx

        if not isinstance(payload, dict):
            raise ValueError(
                f"artifact_prompt must be a dict, got {type(payload).__name__}"
            )

        artifact_type = _require_field(payload, "artifact_type")
        artifact_id = _require_field(payload, "artifact_id")
        system_frame = _require_field(payload, "system_frame")

        dependencies = payload.get("dependencies", [])
        prior_outputs = payload.get("prior_outputs", [])
        guidance = payload.get("guidance", [])
        support_articles = payload.get("support_articles", [])
        blocked_focus_labels = frozenset(payload.get("blocked_focus_labels", ()))

        # Collect refs for focus dedup
        loaded_refs: set[str] = set()
        for dep in dependencies:
            source = dep.get("source", "")
            if source:
                loaded_refs.add(source)
            mem_id = dep.get("memory_id", "")
            if mem_id:
                loaded_refs.add(mem_id)
        for out in prior_outputs:
            mem_id = out.get("memory_id", "")
            if mem_id:
                loaded_refs.add(mem_id)

        # Build blocks in architectural order
        blocks: list[MessageBlock] = []

        # 1. System frame
        blocks.append(_build_system_frame_block(system_frame))

        # 2. Required dependencies
        dep_blocks = _build_dependency_blocks(dependencies)
        blocks.extend(dep_blocks)

        # 3. Prior outputs
        prior_blocks = _build_prior_output_blocks(prior_outputs)
        blocks.extend(prior_blocks)

        # 4. Supplemental focused memories
        focus_blocks = _build_focus_blocks(
            ctx.focused_memories,
            loaded_refs,
            blocked_focus_labels,
            ctx.history_memory_ids,
        )
        blocks.extend(focus_blocks)

        # 5. Guidance and support articles
        evidence_blocks = _build_guidance_blocks(guidance, support_articles)
        blocks.extend(evidence_blocks)

        # 6. Directive, output contract, reinforcement
        driver_blocks = _build_driver_blocks(payload)
        blocks.extend(driver_blocks)

        ctx.message_blocks.extend(blocks)

        ctx.add_decision(
            self.name,
            f"Artifact prompt: type={artifact_type}, id={artifact_id}, "
            f"deps={len(dep_blocks)}, prior={len(prior_blocks)}, "
            f"focus={len(focus_blocks)}, evidence={len(evidence_blocks)}, "
            f"driver={len(driver_blocks)}, total_blocks={len(blocks)}",
        )

        return ctx
