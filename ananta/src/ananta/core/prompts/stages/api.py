"""APIStage - Builds final LLM API payload.

Assembles the message array and API payload from accumulated context.
Note: temperature and max_tokens are NOT set here - they come from
the inference plugin config (default_inference_plugin.json).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ananta.core.prompts.api_stage.serialization import resolve_role
from ananta.core.prompts.context import (
    ACTIVE_PLAN_MARKER,
    ContextLayer,
    HistoryKind,
    MessageBlock,
    PromptContext,
    SourceKind,
    SourceReference,
)
from ananta.core.prompts.stages.api_files import (
    extract_file_from_observation,
    extract_generated_files,
    extract_generated_files_from_history,
    format_generated_files_summary,
)
from ananta.core.prompts.stages.api_focus import (
    compact_focused_memories,
    format_focused_parts,
)
from ananta.core.prompts.stages.api_frontier import identify_dialogue_frontier
from ananta.core.prompts.stages.api_observation import (
    build_live_input_trailer,
    build_live_observation_trailer,
    get_observation_rendering,
    observation_process_key,
    render_observation_content,
    validate_rendering_fields,
)

logger = logging.getLogger(__name__)

# Section headers for context in user messages (avoid magic strings)
APPENDIX_RELEVANT_CONTEXT_TITLE = "Relevant Context"

# Lightweight JSON format instruction appended to the last user message
# when constrained decoding (response_format) is disabled.  The model
# sees the action schema from the process catalog and this instruction
# tells it to emit JSON in the standard envelope shape.
_JSON_FORMAT_INSTRUCTION = (
    "\n\nRespond with a single JSON object. Do not include any text outside the JSON.\n"
    "Schema: {\"step_summary\": \"<one sentence>\", \"actions\": ["
    "{\"process\": {\"provider_type\": \"<first part>\", \"provider\": \"<second part>\", "
    "\"function_name\": \"<third part>\"}, \"reason\": \"<one sentence>\", \"arguments\": {...}}]}\n"
    "Split the declared process key on `::` to fill provider_type, provider, and function_name. "
    "For example, `plugin::audio_processing_plugin::generate_drone` becomes "
    "provider_type=\"plugin\", provider=\"audio_processing_plugin\", function_name=\"generate_drone\"."
)


def _inject_json_format_instruction_into_blocks(blocks: list[MessageBlock]) -> None:
    """Append JSON format instruction to the last user-role block.

    Scans from the end to find the last user-role block and appends
    the format instruction to its content.  The instruction survives
    the assembly re-serialization because it lives in the block, not
    in the serialized messages list.
    """
    for block in reversed(blocks):
        if resolve_role(block) == "user":
            block.content = block.content + _JSON_FORMAT_INSTRUCTION
            logger.info(
                "JSON_FORMAT: Injected into block %s (%d chars)",
                block.block_id, len(block.content),
            )
            return
    logger.warning("JSON_FORMAT: No user-role block found for format injection")


def _inject_json_format_instruction_into_messages(
    messages: list[dict[str, str]],
) -> None:
    """Append JSON format instruction to the last user message (delegated mode)."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            msg["content"] = msg["content"] + _JSON_FORMAT_INSTRUCTION
            return

class APIStage:
    """Builds final API payload from accumulated context.

    Assembles:
    - System message from formatted prompt
    - Memory context (identity + relevant memories)
    - Conversation history
    - User message
    - Response format with JSON schema (if output_schema present)

    Note: Does NOT set temperature/max_tokens - those come from inference plugin config.
    """

    name = "api"

    def __init__(self) -> None:
        """Initialize API stage.

        No parameters - inference settings come from plugin config.
        """

    def execute(self, ctx: PromptContext) -> PromptContext:
        """Build final messages and API payload.

        Both modes use cache-friendly ordering:
        - Static prefix: system prompt, identity memories, conversation history
        - Dynamic suffix: user prompt with appendix (relevant memories + environment)

        Platform mode appendix: relevant memories + environment
        Delegated mode appendix: relevant memories + environment

        Args:
            ctx: PromptContext with all prior stages completed

        Returns:
            Same context with messages and api_payload set
        """
        if ctx.context_id:
            messages = self._build_messages_platform_mode(ctx)
        else:
            messages = self._build_messages_delegated_mode(ctx)

        ctx.messages = messages

        # Build API payload - only messages and response format
        # temperature/max_tokens come from inference plugin config
        ctx.api_payload = {
            "messages": messages,
        }

        # Unconstrained decoding: do NOT inject response_format.
        # The format instruction is injected into the last user-role
        # MessageBlock by _inject_json_format_instruction_into_blocks
        # (called in _build_messages_platform_mode).  The model produces
        # valid JSON guided by the prompt text; grammar enforcement is
        # skipped to avoid LM Studio grammar explosion on long strings.
        if ctx.output_schema:
            ctx.add_decision(self.name, "Unconstrained mode (no response_format)")

        ctx.add_decision(self.name, f"Total messages: {len(messages)}")

        return ctx

    def _build_messages_platform_mode(self, ctx: PromptContext) -> list[dict[str, str]]:
        """Build messages using block-based assembly (Section 7).

        Pipeline: classify → frontier → order → serialize.
        Ordering rule:
          1. ossified_context.static_frame
          2. ossified_context.settled_history  (chronological)
          3. living_context.working_evidence   (if present)
          4. living_context.working_state
          5. living_context.dialogue_frontier OR synthetic_driver (current-step driver)  (last)
        """
        blocks = self._classify_blocks(ctx)
        blocks = identify_dialogue_frontier(blocks, ctx, self.name)
        has_schema = ctx.output_schema is not None
        logger.info(
            "PLATFORM_MODE: %d blocks, output_schema=%s",
            len(blocks), has_schema,
        )
        if has_schema:
            _inject_json_format_instruction_into_blocks(blocks)
        ordered = self._order_blocks(blocks)
        ctx.message_blocks = ordered
        ctx.add_decision(self.name, f"Block assembly: {len(ordered)} blocks")
        return self._serialize_blocks(ordered)

    # -- Block classification (Section 11) --

    def _classify_blocks(self, ctx: PromptContext) -> list[MessageBlock]:
        """Classify all sources into MessageBlock objects.

        Creates blocks from: system prompt, identity, conversation history,
        generated files, tool observation, focused memories, current user
        input, and user instruction.
        """
        blocks: list[MessageBlock] = []
        seq = 0

        # Static frame: system prompt + identity
        static_blocks = self._classify_static_frame_blocks(ctx, seq)
        blocks.extend(static_blocks)
        seq += len(static_blocks)

        # Settled history: conversation history entries
        history_blocks = self._classify_history_blocks(ctx)
        blocks.extend(history_blocks)
        if history_blocks:
            seq = len(history_blocks)

        # Working evidence: generated files summary
        gen_block = self._classify_generated_files_block(ctx, seq)
        if gen_block:
            blocks.append(gen_block)
            seq += 1

        # Working evidence: hydrated playbook section for current plan step
        playbook_block = self._classify_playbook_section_block(ctx, seq)
        if playbook_block:
            blocks.append(playbook_block)
            seq += 1

        # Observation + instruction blocks (depend on message_rendering)
        rendering = get_observation_rendering(ctx)
        obs_blocks = self._classify_observation_blocks(ctx, rendering, seq)
        blocks.extend(obs_blocks)
        seq += len(obs_blocks)

        # Working evidence: discovered process schema (from CatalogStage)
        disc_block = self._classify_discovered_schema_block(ctx, seq)
        if disc_block:
            blocks.append(disc_block)
            seq += 1

        # Working state: focused memories (artifacts + plan as separate messages)
        focus_blocks = self._classify_focus_blocks(ctx, seq)
        blocks.extend(focus_blocks)
        seq += len(focus_blocks)

        # Working evidence/driver: step guidance messages (from GuidanceStage)
        # Placed AFTER focused memories so the guidance article appears
        # immediately before the user driver — matching the canonical
        # message order.
        guidance_blocks = self._classify_step_guidance_blocks(ctx, seq)
        blocks.extend(guidance_blocks)
        seq += len(guidance_blocks)

        # Dialogue frontier: current user input
        input_block = self._classify_user_input_block(ctx, seq)
        if input_block:
            blocks.append(input_block)

        return blocks

    def _classify_static_frame_blocks(
        self, ctx: PromptContext, seq: int,
    ) -> list[MessageBlock]:
        """Create static frame blocks for system prompt and identity."""
        blocks: list[MessageBlock] = []
        if ctx.system_prompt:
            blocks.append(MessageBlock(
                block_id=f"sys-{seq}",
                context_layer="ossified_context",
                reasoning_slot="static_frame",
                ephemeral=False,
                history_kind="none",
                source_kind="system_template",
                subtype="process_catalog",
                source_reference=SourceReference(kind="template", ref="system_prompt"),
                transition_behavior="stable",
                content=ctx.system_prompt,
                sequence=seq,
            ))
            ctx.add_decision(self.name, f"System message: {len(ctx.system_prompt)} chars")
            seq += 1
        if ctx.identity_memories:
            content = self._format_identity(ctx.identity_memories)
            blocks.append(MessageBlock(
                block_id=f"sys-{seq}",
                context_layer="ossified_context",
                reasoning_slot="static_frame",
                ephemeral=False,
                history_kind="none",
                source_kind="system_template",
                subtype="identity",
                source_reference=SourceReference(kind="template", ref="identity_memories"),
                transition_behavior="stable",
                content=content,
                sequence=seq,
            ))
            ctx.add_decision(self.name, f"Identity context: {len(ctx.identity_memories)} items")
        return blocks

    def _classify_history_blocks(self, ctx: PromptContext) -> list[MessageBlock]:
        """Create settled history blocks from conversation history."""
        blocks: list[MessageBlock] = []
        for i, msg in enumerate(ctx.conversation_history):
            raw_role = msg.get("role", "assistant")
            hk: HistoryKind
            if raw_role == "user":
                hk = "input_event"
            elif raw_role == "assistant":
                hk = "output_event"
            else:
                hk = "none"
            blocks.append(MessageBlock(
                block_id=f"hist-{i}",
                context_layer="ossified_context",
                reasoning_slot="settled_history",
                ephemeral=False,
                history_kind=hk,
                source_kind="persisted_event",
                subtype="conversation",
                source_reference=SourceReference(kind="event_sequence", ref=str(i)),
                transition_behavior="stable",
                content=msg.get("content", ""),
                sequence=i,
                prompt_role=raw_role,
            ))
        if blocks:
            ctx.add_decision(
                self.name, f"Conversation history: {len(blocks)} messages"
            )
        return blocks

    # Content markers for planning artifacts that should be excluded from
    # the prompt during WBS execution.  The WBS itself stays in focused
    # memories for schema building / bound-argument lifting, but is not
    # rendered into the model's context.
    _WBS_EXEC_ARTIFACT_MARKERS = (
        "# Work Manifest",
        "MANIFEST ID:",
        "# Composition Design Document",
        "DESIGN ID:",
        "DOCUMENT ID:",
        "# Composition Sketch",
        "SKETCH ID:",
        "# Work Breakdown Structure",
        "WBS ID:",
        "# Resolved Intake State",
        "INTAKE ID:",
    )

    def _classify_focus_blocks(
        self, ctx: PromptContext, seq: int,
    ) -> list[MessageBlock]:
        """Create separate working-state blocks for each focused memory.

        Each focused item (artifact, plan) becomes its own assistant
        message so the model sees clean document boundaries.

        During WBS execution, only the active plan is rendered.
        Planning artifacts (manifest, sketch, WBS, intake) are
        excluded — they bloat the context and the step contract
        already provides the bound arguments.
        """
        if not ctx.focused_memories:
            return []
        memories = self._filter_focus_for_wbs_execution(ctx)
        memories = compact_focused_memories(memories)
        parts = format_focused_parts(
            memories,
            history_memory_ids=ctx.history_memory_ids,
        )
        if not parts:
            return []
        ctx.add_decision(self.name, f"Active focus: {len(parts)} items")
        blocks: list[MessageBlock] = []
        for i, content in enumerate(parts):
            blocks.append(MessageBlock(
                block_id=f"focus-{seq + i}",
                context_layer="living_context",
                reasoning_slot="working_state",
                ephemeral=True,
                history_kind="none",
                source_kind="focus_state",
                subtype="active_plan",
                source_reference=SourceReference(
                    kind="focus_buffer", ref="focused_memories",
                ),
                transition_behavior="replaced",
                content=content,
                sequence=seq + i,
            ))
        return blocks

    def _filter_focus_for_wbs_execution(
        self, ctx: PromptContext,
    ) -> list[dict[str, Any]]:
        """Filter focused memories for WBS execution context.

        During WBS execution, only the active plan is needed in the
        prompt.  Planning artifacts (manifest, sketch, WBS document,
        intake state) are excluded to keep the context lean.  The full
        focused_memories list is preserved on ``ctx`` for platform-
        internal processing (schema building, bound-argument lifting).
        """
        if not ctx.is_wbs_active_plan:
            return ctx.focused_memories

        filtered: list[dict[str, Any]] = []
        dropped = 0
        for mem in ctx.focused_memories:
            content = mem.get("content", "")
            if not isinstance(content, str):
                filtered.append(mem)
                continue
            if ACTIVE_PLAN_MARKER in content:
                filtered.append(mem)
                continue
            if any(marker in content[:100] for marker in self._WBS_EXEC_ARTIFACT_MARKERS):
                dropped += 1
                continue
            filtered.append(mem)
        if dropped:
            logger.info(
                "APISTAGE_FOCUS_FILTER: Excluded %d artifact(s) from "
                "focus rendering (WBS execution context)",
                dropped,
            )
        return filtered

    def _classify_user_input_block(
        self, ctx: PromptContext, seq: int,
    ) -> MessageBlock | None:
        """Create dialogue frontier block for current user input.

        Handles deduplication: if input is already in conversation history,
        marks current turn but returns None.

        Appends a JSON metadata trailer (namespace, source, posted_at,
        session_id) when IO metadata is available in flow_input, matching
        the trailer format used on persisted INPUT events.
        """
        user_input = self._extract_user_input_text(ctx)
        if not user_input:
            return None
        if self._input_already_in_history(user_input, ctx):
            ctx.has_current_user_turn = True
            ctx.add_decision(self.name, "User input already in history, marking current turn")
            return None
        ctx.has_current_user_turn = True

        # Append metadata trailer matching persisted INPUT event format
        trailer = build_live_input_trailer(ctx)
        content = f"{user_input}\n\n{trailer}" if trailer else user_input

        ctx.add_decision(self.name, f"User input: {len(content)} chars")
        return MessageBlock(
            block_id=f"input-{seq}",
            context_layer="living_context",
            reasoning_slot="dialogue_frontier",
            ephemeral=False,
            history_kind="input_event",
            source_kind="human_input",
            subtype="user_request",
            source_reference=SourceReference(kind="flow_input", ref="original_input"),
            transition_behavior="living_to_ossified",
            content=content,
            sequence=seq + 1000,  # High sequence: after promoted blocks
        )

    def _classify_observation_blocks(
        self,
        ctx: PromptContext,
        rendering: dict[str, Any],
        seq: int,
    ) -> list[MessageBlock]:
        """Create blocks for the tool observation and instruction.

        Uses message_rendering to determine placement:
        - working_evidence/assistant: observation block + separate instruction block
        - synthetic_driver/user: combined observation+instruction block
        - No rendering: observation as working_evidence, instruction as synthetic_driver

        When ``renderer_key`` is present in ``rendering``, calls the prompt-layer
        renderer to format the raw observation data instead of using the
        template-rendered text in ``ctx.tool_observation``.
        """
        if not ctx.tool_observation and not ctx.user_prompt:
            return []

        if rendering:
            validate_rendering_fields(rendering)

        # Render observation content via prompt-layer renderer when available
        observation_content = render_observation_content(ctx, rendering)

        target_slot = str(rendering.get("reasoning_slot", "working_evidence"))
        target_role = str(rendering.get("prompt_role", "assistant"))

        if target_slot == "synthetic_driver" and target_role == "user":
            return self._build_combined_observation_block(ctx, seq, observation_content)
        return self._build_separate_observation_blocks(
            ctx, rendering, target_slot, target_role, seq, observation_content,
        )

    _ORIGINAL_REQUEST_RE = re.compile(r"\n+Original request:.*$", re.DOTALL)

    def _effective_user_prompt(self, ctx: PromptContext) -> str:
        """Resolve the user prompt, composing empty-observation notice with step driver.

        In plan continuation, GuidanceStage may have replaced the base
        result-processor prompt with the current step contract.  Preserve both:
        empty-observation notice first, current-step driver second.

        Non-plan empty observations keep replacement behavior to avoid pairing
        "no results" with stale normal guidance.
        """
        prompt = ctx.user_prompt or ""
        empty_prompt = ctx.user_prompt_when_observation_empty or ""

        if not ctx.observation_is_empty or not empty_prompt:
            return prompt

        if ctx.has_focused_plan and prompt:
            # Strip "Original request:" from the empty-observation notice
            # before composing.  The full request is already in settled
            # history; leaving it here causes the DOTALL stripping regex
            # in _build_instruction_content to consume the step driver.
            cleaned = self._ORIGINAL_REQUEST_RE.sub("", empty_prompt).rstrip()
            return f"{cleaned}\n\n{prompt}"

        return empty_prompt

    def _build_combined_observation_block(
        self,
        ctx: PromptContext,
        seq: int,
        observation_content: str | None = None,
    ) -> list[MessageBlock]:
        """Build a single combined observation+instruction block (synthetic_driver/user).

        Combined blocks are user-role instruction messages — they do NOT carry
        a metadata trailer.  Trailers belong on persisted communication events
        (assistant-role observations handled by _build_separate_observation_blocks).
        """
        parts: list[str] = []
        obs = observation_content if observation_content is not None else ctx.tool_observation
        if obs:
            parts.append(obs)
        effective_prompt = self._effective_user_prompt(ctx)
        if effective_prompt:
            parts.append(effective_prompt)
        combined = "\n\n".join(parts)
        if not combined.strip():
            return []
        ctx.add_decision(self.name, f"Combined observation+instruction: {len(combined)} chars")
        return [MessageBlock(
            block_id=f"obs-{seq}",
            context_layer="living_context",
            reasoning_slot="synthetic_driver",
            ephemeral=True,
            history_kind="none",
            source_kind="response_processor_output",
            subtype="combined_findings",
            source_reference=SourceReference(
                kind="process_key",
                ref=observation_process_key(ctx),
            ),
            transition_behavior="filtered",
            content=combined,
            sequence=seq,
        )]

    def _build_separate_observation_blocks(
        self,
        ctx: PromptContext,
        rendering: dict[str, Any],
        target_slot: str,
        target_role: str,
        seq: int,
        observation_content: str | None = None,
    ) -> list[MessageBlock]:
        """Build separate observation and instruction blocks (working_evidence/assistant)."""
        blocks: list[MessageBlock] = []
        obs = observation_content if observation_content is not None else ctx.tool_observation

        if obs:
            # Append metadata trailer matching persisted OUTPUT event format
            trailer = build_live_observation_trailer(ctx)
            if trailer:
                obs = f"{obs}\n\n{trailer}"
            obs_layer: ContextLayer = rendering.get("context_layer", "living_context")
            obs_ephemeral = bool(rendering.get("ephemeral", True))
            obs_hk = str(rendering.get("history_kind", "none"))
            obs_tb = str(rendering.get("transition_behavior", "filtered"))
            obs_source: SourceKind = "response_processor_output"
            blocks.append(MessageBlock(
                block_id=f"obs-{seq}",
                context_layer=obs_layer,
                reasoning_slot=target_slot,  # type: ignore[arg-type]
                ephemeral=obs_ephemeral,
                history_kind=obs_hk,  # type: ignore[arg-type]
                source_kind=obs_source,
                subtype="observation",
                source_reference=SourceReference(
                    kind="process_key",
                    ref=observation_process_key(ctx),
                ),
                transition_behavior=obs_tb,  # type: ignore[arg-type]
                content=obs,
                sequence=seq,
            ))
            ctx.add_decision(self.name, f"Tool observation ({target_slot}): {len(obs)} chars")
            seq += 1

        instruction = self._build_instruction_content(ctx)
        if instruction:
            blocks.append(MessageBlock(
                block_id=f"instr-{seq}",
                context_layer="living_context",
                reasoning_slot="synthetic_driver",
                ephemeral=True,
                history_kind="none",
                source_kind="runtime_instruction",
                subtype="processor_instruction",
                source_reference=SourceReference(kind="template", ref="user_prompt"),
                transition_behavior="filtered",
                content=instruction,
                sequence=seq,
            ))
            ctx.add_decision(self.name, f"Instruction: {len(instruction)} chars")

        return blocks

    def _classify_generated_files_block(
        self, ctx: PromptContext, seq: int,
    ) -> MessageBlock | None:
        """Create a working_evidence block for generated files summary."""
        generated_files = extract_generated_files_from_history(ctx)

        observation_file = extract_file_from_observation(ctx)
        if observation_file:
            existing_ids = {f.get("blob_id") for f in generated_files}
            if observation_file.get("blob_id") not in existing_ids:
                generated_files.append(observation_file)

        if not generated_files:
            return None

        summary = format_generated_files_summary(generated_files)
        return MessageBlock(
            block_id=f"genfiles-{seq}",
            context_layer="living_context",
            reasoning_slot="working_evidence",
            ephemeral=True,
            history_kind="none",
            source_kind="prompt_asset",
            subtype="generated_files",
            source_reference=SourceReference(kind="scan", ref="generated_files"),
            transition_behavior="filtered",
            content=f"[Recent files generated by system]\n{summary}",
            sequence=seq,
        )

    @staticmethod
    def _classify_playbook_section_block(
        ctx: PromptContext, seq: int,
    ) -> MessageBlock | None:
        """Create a working_evidence block for the hydrated playbook section.

        The section content is loaded by ContextStage from the thinking service
        and stored on ctx.playbook_section_content. This method just wraps it
        in the correct MessageBlock for v7-compliant ordering.
        """
        if not ctx.playbook_section_content:
            return None

        section_id = ctx.playbook_section_id or "unknown"
        ctx.add_decision("api", f"Playbook section block: {section_id} ({len(ctx.playbook_section_content)} chars)")

        return MessageBlock(
            block_id=f"playbook-{seq}",
            context_layer="living_context",
            reasoning_slot="working_evidence",
            ephemeral=True,
            history_kind="none",
            source_kind="prompt_asset",
            subtype="playbook_section",
            source_reference=SourceReference(kind="playbook", ref=section_id),
            transition_behavior="filtered",
            content=ctx.playbook_section_content,
            sequence=seq,
        )

    @staticmethod
    def _classify_discovered_schema_block(
        ctx: PromptContext, seq: int,
    ) -> MessageBlock | None:
        """Create a working_evidence block for discovered process schema.

        The schema text is computed by CatalogStage and stored on
        ctx.discovered_schema_text.  Placed after observation blocks
        so the model sees: observation → schema details → instruction.
        """
        if not ctx.discovered_schema_text:
            return None

        ctx.add_decision(
            "api",
            f"Discovered schema block: {len(ctx.discovered_schema_text)} chars",
        )

        return MessageBlock(
            block_id=f"disc-schema-{seq}",
            context_layer="living_context",
            reasoning_slot="working_evidence",
            ephemeral=True,
            history_kind="none",
            source_kind="discovered_schema",
            subtype="invocation_schema",
            source_reference=SourceReference(
                kind="discovery", ref="query_process_registry",
            ),
            transition_behavior="filtered",
            content=ctx.discovered_schema_text,
            sequence=seq,
        )

    @staticmethod
    def _classify_step_guidance_blocks(
        ctx: PromptContext, seq: int,
    ) -> list[MessageBlock]:
        """Create blocks for step guidance messages from GuidanceStage.

        Guidance messages are computed by GuidanceStage and stored on
        ctx.step_guidance_messages.  Each becomes either a working_evidence
        block (assistant role) or a synthetic_driver block (user role).
        """
        blocks: list[MessageBlock] = []
        for i, msg in enumerate(ctx.step_guidance_messages):
            role = msg.get("role", "assistant")
            content = msg.get("content", "")
            if not content:
                continue

            if role == "user":
                blocks.append(MessageBlock(
                    block_id=f"step-driver-{seq + i}",
                    context_layer="living_context",
                    reasoning_slot="synthetic_driver",
                    ephemeral=True,
                    history_kind="none",
                    source_kind="runtime_instruction",
                    subtype="step_driver",
                    source_reference=SourceReference(
                        kind="guidance", ref="step_instruction",
                    ),
                    transition_behavior="filtered",
                    content=content,
                    sequence=seq + i,
                ))
            else:
                blocks.append(MessageBlock(
                    block_id=f"step-guidance-{seq + i}",
                    context_layer="living_context",
                    reasoning_slot="working_evidence",
                    ephemeral=True,
                    history_kind="none",
                    source_kind="prompt_asset",
                    subtype="step_guidance",
                    source_reference=SourceReference(
                        kind="guidance", ref="guidance_article",
                    ),
                    transition_behavior="filtered",
                    content=content,
                    sequence=seq + i,
                ))

        if blocks:
            ctx.add_decision(
                "api",
                f"Step guidance blocks: {len(blocks)} ({sum(1 for b in blocks if b.prompt_role == 'assistant')} assistant, "
                f"{sum(1 for b in blocks if b.prompt_role == 'user')} user)",
            )

        return blocks

    def _build_instruction_content(self, ctx: PromptContext) -> str:
        """Build instruction content from user_prompt and relevant memories.

        Excludes the instruction when:
        - The user input is already the instruction (opening turn)
        - The observation rendering is synthetic_driver (already combined)

        On the opening turn (no plan, no observation) the live user
        input appears as its own dialogue-frontier block, so the
        instruction block is suppressed entirely — returning empty
        prevents an extra USER message containing only the
        ``Original request:`` reprise.

        Relevant memories are only appended on continuation turns (when
        a focused plan or tool observation exists). On the opening turn
        the model has not yet executed recall, so pre-fetched semantic
        recall results would leak stale context from prior flows.
        """
        is_opening_turn = not ctx.has_focused_plan and not ctx.tool_observation
        if is_opening_turn:
            return ""

        parts: list[str] = []

        if ctx.attachment_summary:
            parts.append(ctx.attachment_summary)

        effective_prompt = self._effective_user_prompt(ctx)

        if effective_prompt:
            prompt = effective_prompt
            # Strip "Original request:" reprise for plan continuation turns.
            # The original request is already in conversation history; the
            # Codex NS06 fixtures never include it in the instruction block.
            if ctx.has_focused_plan:
                prompt = re.sub(
                    r"\n+Original request:.*$", "", prompt, flags=re.DOTALL,
                ).rstrip()
            if prompt:
                parts.append(prompt)

        if ctx.relevant_memories:
            relevant_content = self._format_memories(
                ctx.relevant_memories, APPENDIX_RELEVANT_CONTEXT_TITLE,
            )
            parts.append(relevant_content)

        return "\n\n".join(parts)

    @staticmethod
    def _extract_user_input_text(ctx: PromptContext) -> str | None:
        """Extract current user input from resolved_action_params."""
        prompt_part = ctx.resolved_action_params.get("prompt", {})
        user_part = prompt_part.get("user", {}) if isinstance(prompt_part, dict) else {}
        flow_input = user_part.get("flow_input", {}) if isinstance(user_part, dict) else {}
        raw = flow_input.get("original_input") if isinstance(flow_input, dict) else None
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    @staticmethod
    def _input_already_in_history(user_input: str, ctx: PromptContext) -> bool:
        """Check if user input is already present in conversation history.

        Uses startswith because persisted INPUT events may have a JSON
        metadata trailer appended by ContextStage.
        """
        for msg in reversed(ctx.conversation_history):
            if msg.get("role") == "user" and msg.get("content", "").startswith(user_input):
                return True
        return False

    # -- Ordering and serialization (Section 7) --

    @staticmethod
    def _order_blocks(blocks: list[MessageBlock]) -> list[MessageBlock]:
        """Order blocks by context_layer, reasoning_slot, sequence.

        Pure function: reads sort_key from each block, returns sorted list.
        No classification, promotion, or persistence logic.
        """
        return sorted(blocks, key=lambda b: b.sort_key)

    @staticmethod
    def _serialize_blocks(blocks: list[MessageBlock]) -> list[dict[str, str]]:
        """Convert ordered MessageBlocks to raw message dicts for the LLM API."""
        return [{"role": resolve_role(b), "content": b.content} for b in blocks]

    @staticmethod
    def _has_real_user_input(ctx: PromptContext) -> bool:
        """Check if resolved_action_params contain a non-empty original_input."""
        prompt_part = ctx.resolved_action_params.get("prompt", {})
        user_part = prompt_part.get("user", {}) if isinstance(prompt_part, dict) else {}
        flow_input = user_part.get("flow_input", {}) if isinstance(user_part, dict) else {}
        original_input = flow_input.get("original_input") if isinstance(flow_input, dict) else None
        return bool(original_input and isinstance(original_input, str) and original_input.strip())

    def _build_initial_system_messages(self, ctx: PromptContext) -> list[dict[str, str]]:
        """Build system messages for delegated mode (no context_id).

        Focused memories are appended here for delegated mode (no suffix assembly).
        For platform mode, _build_platform_prefix + _append_focused_memories handle this.
        """
        messages: list[dict[str, str]] = []

        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
            ctx.add_decision(self.name, f"System message: {len(ctx.system_prompt)} chars")

        if ctx.identity_memories:
            content = self._format_identity(ctx.identity_memories)
            messages.append({"role": "system", "content": content})
            ctx.add_decision(self.name, f"Identity context: {len(ctx.identity_memories)} items")

        if ctx.conversation_history:
            messages.extend(ctx.conversation_history)
            ctx.add_decision(
                self.name, f"Conversation history: {len(ctx.conversation_history)} messages"
            )

        if ctx.focused_memories:
            memories = self._filter_focus_for_wbs_execution(ctx)
            memories = compact_focused_memories(memories)
            for part in format_focused_parts(
                memories,
                history_memory_ids=ctx.history_memory_ids,
            ):
                messages.append({"role": "assistant", "content": part})
            ctx.add_decision(self.name, f"Active focus: {len(memories)} items")

        return messages

    def _append_tool_observation(
        self, messages: list[dict[str, str]], ctx: PromptContext
    ) -> None:
        """Append tool observation as an assistant message.

        Tool observations (action results, error context) use assistant role so
        the model sees "I already processed this and got results." This prevents
        confusing tool results with new user requests (user role) and avoids
        system messages being rehoisted to the top of context by inference
        backends, which would break prefix caching.
        """
        if not ctx.tool_observation:
            return

        messages.append({"role": "assistant", "content": ctx.tool_observation})
        ctx.add_decision(self.name, f"Tool observation: {len(ctx.tool_observation)} chars")

    def _append_generated_files_summary(
        self, messages: list[dict[str, str]], ctx: PromptContext,
    ) -> None:
        """Append generated files summary (delegated mode only)."""
        generated_files = extract_generated_files(messages)

        observation_file = extract_file_from_observation(ctx)
        if observation_file:
            existing_ids = {f.get("blob_id") for f in generated_files}
            if observation_file.get("blob_id") not in existing_ids:
                generated_files.append(observation_file)

        if not generated_files:
            return

        generated_summary = format_generated_files_summary(generated_files)
        messages.append({
            "role": "user",
            "content": f"[Recent files generated by system]\n{generated_summary}",
        })
        ctx.add_decision(self.name, f"Added {len(generated_files)} generated file(s) to context")

    def _build_messages_delegated_mode(self, ctx: PromptContext) -> list[dict[str, str]]:
        """Build messages with cache-friendly ordering for delegated mode.

        Same structure as platform mode to preserve prefix caching:

        Stable prefix (cached by LLM):
        1. System prompt
        2. Identity context (identity_memories)
        3. Conversation history (if any)

        Dynamic suffix (appended to user prompt):
        4. User prompt + appendix (relevant_memories + environment)

        Args:
            ctx: PromptContext without context_id

        Returns:
            List of message dicts
        """
        # Reuse initial system messages builder (always generate, no stored events)
        messages = self._build_initial_system_messages(ctx)

        self._append_generated_files_summary(messages, ctx)
        self._append_tool_observation(messages, ctx)
        self._append_delegated_user_message(messages, ctx)

        if ctx.output_schema:
            _inject_json_format_instruction_into_messages(messages)

        return messages

    def _append_delegated_user_message(
        self, messages: list[dict[str, str]], ctx: PromptContext
    ) -> None:
        """Append user message with relevant memories for delegated mode."""
        user_content = self._build_delegated_user_content(ctx)
        if not user_content:
            return

        messages.append({"role": "user", "content": user_content})
        ctx.add_decision(self.name, f"User message: {len(user_content)} chars")

    def _build_delegated_user_content(self, ctx: PromptContext) -> str:
        """Build user message content with relevant memories.

        Relevant memories are gated to continuation turns only (same
        rationale as ``_build_instruction_content``).
        """
        parts: list[str] = []

        if ctx.user_prompt:
            parts.append(ctx.user_prompt)

        is_opening_turn = not ctx.has_focused_plan and not ctx.tool_observation
        if ctx.relevant_memories and not is_opening_turn:
            relevant_content = self._format_memories(ctx.relevant_memories, APPENDIX_RELEVANT_CONTEXT_TITLE)
            parts.append(relevant_content)
            ctx.add_decision(self.name, f"Added relevant context: {len(ctx.relevant_memories)} items")

        return "\n\n".join(parts)

    def _format_memories(
        self,
        memories: list[dict[str, Any]],
        section_title: str,
    ) -> str:
        """Format memory items as context content.

        Uses bullet-point format for clean, scannable output.
        Memories are sorted by content for deterministic ordering (cache-friendly).

        Args:
            memories: List of memory dicts
            section_title: Title for this memory section

        Returns:
            Formatted string for system message
        """
        lines: list[str] = [f"{section_title}:"]

        # Sort memories by content for deterministic ordering (preserves LLM cache)
        def get_sort_key(mem: dict[str, Any]) -> str:
            if "content" in mem:
                content = mem["content"]
                return str(content) if content else ""
            if "text" in mem:
                return str(mem["text"])
            return ""

        sorted_memories = sorted(memories, key=get_sort_key)

        for mem in sorted_memories:
            # Handle different memory formats
            if "content" in mem:
                content = mem["content"]
                if isinstance(content, str) and content:
                    lines.append(f"- {content}")
                elif content:
                    lines.append(f"- {json.dumps(content)}")
            elif "text" in mem:
                text = str(mem["text"])
                if text:
                    lines.append(f"- {text}")
            elif "source" in mem:
                # Skip meta-only entries
                continue
            else:
                # Fallback: serialize the whole memory
                lines.append(f"- {json.dumps(mem)}")

        return "\n".join(lines)

    def _format_identity(self, memories: list[dict[str, Any]]) -> str:
        """Format identity memories as plain content without section headers.

        Identity is unique to each Solet and should appear as natural
        first-person statements, not as bullet-point context.

        Args:
            memories: List of identity memory dicts

        Returns:
            Concatenated identity content
        """
        contents: list[str] = []

        for mem in memories:
            if "content" in mem:
                content = mem["content"]
                if isinstance(content, str) and content:
                    contents.append(content)
                elif content:
                    contents.append(json.dumps(content))
            elif "text" in mem:
                text = str(mem["text"])
                if text:
                    contents.append(text)

        return " ".join(contents)
