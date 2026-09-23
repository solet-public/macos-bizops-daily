"""Dialogue frontier logic for APIStage.

Pure functions extracted from APIStage for identifying and
promoting dialogue frontier blocks (Section 14).
"""

from __future__ import annotations

from ananta.core.prompts.context import MessageBlock, PromptContext


def has_live_user_input(blocks: list[MessageBlock]) -> bool:
    """Check if any block is a live human input in the dialogue frontier."""
    return any(
        b.source_kind == "human_input"
        and b.reasoning_slot == "dialogue_frontier"
        for b in blocks
    )


def live_input_sequence(blocks: list[MessageBlock]) -> int:
    """Get the sequence number of the live user input block."""
    return next(
        b.sequence for b in blocks
        if b.source_kind == "human_input"
        and b.reasoning_slot == "dialogue_frontier"
    )


def compute_promote_set(
    blocks: list[MessageBlock],
    settled_indices: list[int],
) -> set[int]:
    """Determine which settled_history block indices to promote.

    Rules (Section 14):
    - No user messages in history: promote only the last assistant
    - Only user message is the first: promote only the last assistant (conservative)
    - Multiple user messages: promote all assistants after the last user
    """
    last_user_pos = -1
    for idx in settled_indices:
        if blocks[idx].prompt_role == "user":
            last_user_pos = idx

    if last_user_pos < 0 or last_user_pos == settled_indices[0]:
        # No user messages, or only user message is the first -- conservative
        for idx in reversed(settled_indices):
            if blocks[idx].prompt_role == "assistant":
                return {idx}
        return set()

    # Promote all assistants after the last user message
    return {
        idx for idx in settled_indices
        if idx > last_user_pos and blocks[idx].prompt_role == "assistant"
    }


def rebuild_with_promoted(
    blocks: list[MessageBlock],
    promote_set: set[int],
    live_input_seq: int,
    ctx: PromptContext,
    stage_name: str,
) -> list[MessageBlock]:
    """Rebuild the block list with promoted entries moved to dialogue_frontier."""
    new_blocks: list[MessageBlock] = []
    promote_seq = live_input_seq - len(promote_set)

    for i, b in enumerate(blocks):
        if i in promote_set:
            new_blocks.append(MessageBlock(
                block_id=b.block_id,
                context_layer="living_context",
                reasoning_slot="dialogue_frontier",
                ephemeral=False,
                history_kind=b.history_kind,
                source_kind="persisted_event",
                subtype=b.subtype,
                source_reference=b.source_reference,
                transition_behavior="promoted",
                content=b.content,
                sequence=promote_seq,
                prompt_role=b.prompt_role,
            ))
            promote_seq += 1
            ctx.add_decision(
                stage_name,
                f"frontier_decision: block_id={b.block_id}, action=promoted, "
                f"reason=contiguous_assistant_run",
            )
        else:
            new_blocks.append(b)
            if b.reasoning_slot == "settled_history":
                ctx.add_decision(
                    stage_name,
                    f"frontier_decision: block_id={b.block_id}, action=settled",
                )

    return new_blocks


def identify_dialogue_frontier(
    blocks: list[MessageBlock],
    ctx: PromptContext,
    stage_name: str,
) -> list[MessageBlock]:
    """Identify and promote dialogue frontier blocks.

    On user-triggered turns, the live user input is dialogue_frontier.
    Assistant messages after the last user message in settled_history
    are promoted into dialogue_frontier.

    On platform-triggered turns (no live input), no promotion occurs.
    """
    if not has_live_user_input(blocks):
        ctx.add_decision(
            stage_name, "frontier: platform-triggered turn, no promotion",
        )
        return blocks

    settled_indices = [
        i for i, b in enumerate(blocks)
        if b.reasoning_slot == "settled_history"
    ]
    if not settled_indices:
        ctx.add_decision(
            stage_name, "frontier: no settled history to promote from",
        )
        return blocks

    promote = compute_promote_set(blocks, settled_indices)
    if not promote:
        ctx.add_decision(stage_name, "frontier: no blocks to promote")
        return blocks

    return rebuild_with_promoted(
        blocks, promote, live_input_sequence(blocks), ctx, stage_name,
    )
