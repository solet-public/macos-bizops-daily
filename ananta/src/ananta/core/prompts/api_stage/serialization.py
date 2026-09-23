"""Block ordering and serialization for the LLM API.

Owns the full block-to-message conversion:
1. **Role assignment** — maps ``(reasoning_slot, source_kind)`` to a prompt
   role via a complete, fail-closed mapping table.
2. **Adjacent same-role merge** — consecutive blocks with the same resolved
   role merge into a single message separated by ``"\\n\\n"``.
3. **Single-system-message handling** — when
   ``SerializationSpec.supports_multiple_system_messages`` is False, all
   system-role blocks merge into one leading system message.
4. **Ordering** — blocks sorted by ``sort_key`` before serialization.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ananta.core.prompts.context import MessageBlock

if TYPE_CHECKING:
    from ananta.services.inference_service.assembly_types import SerializationSpec

logger = logging.getLogger(__name__)

# ── Role-mapping table ───────────────────────────────────────────────
#
# Every ``(reasoning_slot, source_kind)`` pair in use MUST appear here.
# Unmapped combinations raise ``ValueError`` (fail closed).
#
# Key: (reasoning_slot, source_kind)
# Value: prompt role ("system", "assistant", "user")

_ROLE_MAP: dict[tuple[str, str], str] = {
    # Static frame — system prompt and catalog
    ("static_frame", "system_template"): "system",
    ("static_frame", "process_catalog"): "system",
    ("static_frame", "plugin_notice"): "system",
    # Settled history — persisted conversation events
    ("settled_history", "persisted_event"): "from_event",  # sentinel: use event type
    # Working evidence — ephemeral assistant context
    ("working_evidence", "prompt_asset"): "assistant",
    ("working_evidence", "discovered_schema"): "assistant",
    ("working_evidence", "response_processor_output"): "assistant",
    ("working_evidence", "model_output"): "assistant",
    # Working state — focused memories and artifact dependencies
    ("working_state", "focus_state"): "assistant",
    ("working_state", "prompt_asset"): "assistant",
    ("working_state", "model_output"): "assistant",
    # Living context — stage outputs classified into blocks
    ("living_context", "discovered_schema"): "assistant",
    ("living_context", "prompt_asset"): "assistant",
    ("living_context", "runtime_instruction"): "user",
    # Dialogue frontier — current turn
    ("dialogue_frontier", "human_input"): "user",
    ("dialogue_frontier", "runtime_instruction"): "user",
    # Synthetic drivers — platform-injected instructions
    ("synthetic_driver", "runtime_instruction"): "user",
    ("synthetic_driver", "response_processor_output"): "user",
}


def resolve_role(block: MessageBlock) -> str:
    """Resolve the prompt role for a MessageBlock.

    Uses the block's existing ``prompt_role`` (transitional) when set.
    Falls back to the role-mapping table. Raises ``ValueError`` for
    unmapped combinations.
    """
    # Transitional: use the existing prompt_role until all constructors
    # are migrated to omit it and rely on the mapping table.
    if block.prompt_role:
        return block.prompt_role

    key = (block.reasoning_slot, block.source_kind)
    role = _ROLE_MAP.get(key)
    if role is None:
        raise ValueError(
            f"Unmapped (reasoning_slot, source_kind) pair: {key}. "
            f"Add an entry to _ROLE_MAP in serialization.py."
        )
    if role == "from_event":
        # For persisted events, the role is determined by the event type,
        # which is already stored in prompt_role during block construction.
        # This branch should not be reached in production.
        raise ValueError(
            f"Block {block.block_id} has source_kind='persisted_event' but "
            f"no prompt_role set. Persisted events must set prompt_role "
            f"during construction based on the event type."
        )
    return role


def order_blocks(blocks: list[MessageBlock]) -> list[MessageBlock]:
    """Order blocks by context_layer, reasoning_slot, sequence."""
    return sorted(blocks, key=lambda b: b.sort_key)


def serialize_blocks(blocks: list[MessageBlock]) -> list[dict[str, str]]:
    """Convert ordered MessageBlocks to raw message dicts for the LLM API.

    Uses ``resolve_role()`` to determine roles — supports both blocks
    with explicit ``prompt_role`` and blocks using the mapping table.
    """
    return [{"role": resolve_role(b), "content": b.content} for b in blocks]


def serialize_blocks_with_spec(
    blocks: list[MessageBlock],
    spec: SerializationSpec,
) -> list[dict[str, str]]:
    """Convert ordered MessageBlocks to messages using a SerializationSpec.

    Applies role resolution, same-role merging, and single-system-message
    handling per the spec.
    """
    ordered = order_blocks(blocks)

    # Resolve roles for all blocks
    role_content_pairs: list[tuple[str, str]] = []
    for block in ordered:
        role = resolve_role(block)
        role_content_pairs.append((role, block.content))

    # Merge adjacent same-role blocks only when the provider requires
    # role alternation.  LM Studio (and most providers) handle adjacent
    # same-role messages natively — merging destroys semantically distinct
    # message boundaries that the model relies on (e.g., guidance article
    # vs ACTIVE_PLAN focus are separate assistant messages).
    merged = role_content_pairs
    if spec.requires_role_alternation:
        merged = merge_adjacent_same_role(merged)

    # Single-system-message handling
    if not spec.supports_multiple_system_messages:
        merged = consolidate_system_messages(merged)

    return [{"role": role, "content": content} for role, content in merged]


def merge_adjacent_same_role(
    pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Merge consecutive pairs with the same role."""
    if not pairs:
        return []

    merged: list[tuple[str, str]] = []
    current_role, current_content = pairs[0]

    for role, content in pairs[1:]:
        if role == current_role:
            current_content = current_content + "\n\n" + content
        else:
            merged.append((current_role, current_content))
            current_role, current_content = role, content

    merged.append((current_role, current_content))
    return merged


def consolidate_system_messages(
    pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Merge all system-role messages into one leading system message."""
    system_parts: list[str] = []
    non_system: list[tuple[str, str]] = []

    for role, content in pairs:
        if role == "system":
            system_parts.append(content)
        else:
            non_system.append((role, content))

    result: list[tuple[str, str]] = []
    if system_parts:
        result.append(("system", "\n\n".join(system_parts)))
    result.extend(non_system)
    return result
