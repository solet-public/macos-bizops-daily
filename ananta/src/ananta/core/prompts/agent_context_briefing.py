"""Agent-context briefing — group prompt blocks into a retrieval/provenance-first
structured briefing instead of a serialized model message array.

Phase 2 of the coding-agent substrate plan (§3.3/§3.4): ``assemble_agent_context``
gives a frontier agent the grounding a Qwen prompt would have gotten — process
catalog, plan state, guidance, support articles, the answer contract — as
STRUCTURED DATA with provenance, so the agent can request grounding without
asking the solet to think. It is a briefing service, not a prompt shrinker: by default
every block is included (no destructive window-fitting unless the caller supplies
a budget).

The heavy machinery (pipeline factory + stages) produces a tuple of
:class:`~ananta.core.prompts.context.MessageBlock` — each already carrying a
``source_kind`` (provenance class), a ``reasoning_slot``, and a
``source_reference`` (kind + ref). This module is the PURE boundary function that
turns those blocks (plus the decode ``output_schema``) into the briefing dict the
service verb returns. Keeping it pure and pipeline-free is what lets the Phase 2
smoke assert deterministic shape + provenance offline with no model and no
orchestrator collaborators.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ananta.core.prompts.context import MessageBlock

# ---------------------------------------------------------------------------
# Closed bundle vocabulary
# ---------------------------------------------------------------------------

# Named bundles the briefing exposes. Closed set — a block whose source_kind is
# not mapped lands in ``other`` so nothing is silently dropped (default-to-more).
BUNDLE_FRAME = "frame"
BUNDLE_CONVERSATION = "conversation"
BUNDLE_PLAN_STATE = "plan_state"
BUNDLE_GUIDANCE = "guidance"
BUNDLE_PROCESS_CATALOG = "process_catalog"
BUNDLE_ANSWER_CONTRACT = "answer_contract"
BUNDLE_OTHER = "other"

BUNDLE_NAMES: tuple[str, ...] = (
    BUNDLE_FRAME,
    BUNDLE_CONVERSATION,
    BUNDLE_PLAN_STATE,
    BUNDLE_GUIDANCE,
    BUNDLE_PROCESS_CATALOG,
    BUNDLE_ANSWER_CONTRACT,
    BUNDLE_OTHER,
)

# source_kind -> bundle. source_kind is the provenance class assigned at block
# construction (context.py ``SourceKind``). This is the primary routing key.
_SOURCE_KIND_TO_BUNDLE: dict[str, str] = {
    "system_template": BUNDLE_FRAME,
    "runtime_instruction": BUNDLE_FRAME,
    "persisted_event": BUNDLE_CONVERSATION,
    "human_input": BUNDLE_CONVERSATION,
    "model_output": BUNDLE_CONVERSATION,
    "response_processor_output": BUNDLE_CONVERSATION,
    "focus_state": BUNDLE_PLAN_STATE,
    "prompt_asset": BUNDLE_GUIDANCE,
    "process_catalog": BUNDLE_PROCESS_CATALOG,
    "discovered_schema": BUNDLE_ANSWER_CONTRACT,
    "plugin_notice": BUNDLE_OTHER,
}


def _bundle_for(block: MessageBlock) -> str:
    """Route a block to its bundle by source_kind, defaulting to ``other``."""
    return _SOURCE_KIND_TO_BUNDLE.get(block.source_kind, BUNDLE_OTHER)


def _block_item(block: MessageBlock) -> dict[str, object]:
    """Project a block into a JSON-safe briefing item with provenance."""
    return {
        "content": block.content,
        "source_kind": block.source_kind,
        "subtype": block.subtype,
        "reasoning_slot": block.reasoning_slot,
        "provenance": {
            "kind": block.source_reference.kind,
            "ref": block.source_reference.ref,
        },
    }


def group_blocks_into_briefing(
    blocks: tuple[MessageBlock, ...],
    *,
    output_schema: dict[str, object] | None,
    budget: int | None = None,
) -> dict[str, object]:
    """Group ``blocks`` into the agent-context briefing dict.

    Retrieval/provenance-first: every block becomes a briefing item carrying its
    content, source_kind, subtype, reasoning_slot, and source_reference
    provenance, grouped under a named bundle. The decode ``output_schema`` is
    surfaced as ``answer_contract`` DATA (§3.4 — a decision spec handed to the
    agent, not a token-level cage).

    ``budget`` (optional): a briefing service, not a prompt shrinker. When
    ``None`` (default) every block is included. When supplied, at most that many
    blocks are retained overall (a conservative, order-preserving cap); the
    manifest records ``budget_applied`` + ``dropped`` so trimming is never
    silent. Bundle-aware proportional budgeting is a deliberate follow-up
    (TODO): v1 ships the honest "include everything unless the caller opts into a
    cap" contract the done-when requires.

    Returns a plain ``dict`` (JSON-safe) — the service verb returns it directly.
    """
    included = list(blocks)
    dropped = 0
    if budget is not None and budget >= 0 and len(included) > budget:
        dropped = len(included) - budget
        included = included[:budget]

    bundles: dict[str, list[dict[str, object]]] = {name: [] for name in BUNDLE_NAMES}
    provenance: list[dict[str, object]] = []
    available_contracts: list[str] = []
    for block in included:
        bundles[_bundle_for(block)].append(_block_item(block))
        provenance.append({
            "block_id": block.block_id,
            "source_kind": block.source_kind,
            "kind": block.source_reference.kind,
            "ref": block.source_reference.ref,
        })
        if block.source_reference.kind == "process_key":
            available_contracts.append(block.source_reference.ref)

    manifest: dict[str, object] = {
        "block_count": len(included),
        "bundle_counts": {name: len(items) for name, items in bundles.items()},
        "budget": budget,
        "budget_applied": dropped > 0,
        "dropped": dropped,
        "has_answer_contract": output_schema is not None,
    }

    return {
        "profile": "agent_context",
        "bundles": bundles,
        "answer_contract": output_schema,
        "available_contracts": available_contracts,
        "provenance": provenance,
        "manifest": manifest,
    }
