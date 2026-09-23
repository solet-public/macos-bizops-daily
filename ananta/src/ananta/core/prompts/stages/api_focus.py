"""Focus and plan formatting for APIStage.

Pure functions extracted from APIStage for formatting focused
memories and plan content.  Includes artifact compaction — focused
planning artifacts (Work Manifest, Composition Design Document,
Composition Sketch) are stored in full but rendered as compact stubs
to keep the model's context lean during planning steps.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ananta.core.prompts.context import ACTIVE_PLAN_MARKER

# Fraction of max_char_count allocated to focused memory injection.
# Focused memories are rendered from the focus buffer.  Conversation history
# duplicates are removed earlier by content-prefix deduplication; compact
# artifact receipts that carry source_memory_id are not full document copies.
FOCUS_BUDGET_FRACTION = 0.15


def coerce_focus_content(mem: dict[str, Any]) -> str | None:
    """Extract and coerce the content field to a non-empty string.

    Returns ``None`` when the memory has no usable content.
    """
    content = mem.get("content", "")
    if isinstance(content, str):
        return content if content else None
    return json.dumps(content) if content else None


def normalize_plan_whitespace(plan_text: str) -> str:
    """Collapse runs of 3+ newlines to exactly 2 (one blank line between steps).

    Model-produced plans may use extra blank lines between steps
    (e.g. ``\\n\\n\\n`` from double ``\\u2424`` separators). This normalizes
    to at most one blank line between any two lines.
    """
    return re.sub(r"\n{3,}", "\n\n", plan_text)


def strip_plan_args(plan_text: str) -> str:
    """Strip ARGS lines from plan text -- safety net for legacy plans.

    New-format plans store arguments in the knowledge base and the
    focused memory never contains ARGS lines.  This method remains
    as a safety net for any legacy plans still in the memory store.
    """
    return "\n".join(
        line for line in plan_text.splitlines()
        if not line.strip().startswith("ARGS:")
    )


def prepare_plan_content(content: str) -> str:
    """Strip ARGS and normalize whitespace for plan content."""
    content = strip_plan_args(content)
    return normalize_plan_whitespace(content)


def should_skip_non_plan(
    mem: dict[str, Any],
    known_ids: set[str],
) -> bool:
    """Return True if a non-plan memory should be skipped.

    Kept for API compatibility with older callers.  ID-based suppression is
    intentionally disabled: history events often contain compact artifact
    receipts with ``source_memory_id`` rather than the full focused document.
    Suppressing the focused item in that case drops the only full artifact
    copy from the prompt.
    """
    return False


def format_focused_parts(
    memories: list[dict[str, Any]],
    history_memory_ids: set[str] | None = None,
) -> list[str]:
    """Format focused memories as separate message parts.

    Each focused item becomes its own assistant message content.
    Plans get ARGS stripped and whitespace normalized.
    Conversation-history duplicates are removed before this function by
    content-prefix deduplication.  Do not suppress focused artifacts merely
    because their ``memory_id`` appears as ``source_memory_id`` on a persisted
    compact receipt; those receipts are not full document copies.

    A character budget (``FOCUS_BUDGET_FRACTION`` of 262 K default
    max context) caps total injected content.  The plan always
    counts toward budget but is never suppressed.
    """
    budget = int(262144 * FOCUS_BUDGET_FRACTION)  # ~39 K chars
    used = 0

    parts: list[str] = []
    for mem in memories:
        content = coerce_focus_content(mem)
        if content is None:
            continue

        if ACTIVE_PLAN_MARKER in content:
            content = prepare_plan_content(content)

        if used + len(content) > budget and parts:
            # Budget exhausted -- stop adding more items.
            # Always allow the first item (plan) through.
            break
        parts.append(content)
        used += len(content)
    return parts


# ---------------------------------------------------------------------------
# Artifact compaction
# ---------------------------------------------------------------------------
#
# During planning steps, focused artifacts (Work Manifest, Pipeline Spec)
# are stored in full for platform-internal use (schema extraction,
# bound-argument lifting) but rendered as compact stubs so the model
# context stays lean.
#
# The canonical prompt architecture defines which sections each artifact
# type should expose:
#   Work Manifest  → header + Committed Design Decisions (Global only) + Delivery Identity
#
# During WBS execution, artifacts are dropped entirely by the separate
# _filter_focus_for_wbs_execution filter in APIStage.

_MANIFEST_COMPACT_SECTIONS: frozenset[str] = frozenset({
    "Committed Design Decisions",
    "Delivery Identity",
})

_PHASE_SUBSECTION_PREFIX = "Phase "


def _extract_compact_sections(
    content: str,
    keep_sections: frozenset[str],
    *,
    strip_phase_subsections: bool = False,
) -> str:
    """Extract header block and whitelisted ``##`` sections from an artifact.

    The header block (everything before the first ``## ``) is always
    kept — it contains the artifact ID fields and status line.

    Within ``## Committed Design Decisions``, phase-specific
    subsections (``Phase 1 Source Palette:``, etc.) are stripped when
    *strip_phase_subsections* is True, keeping only the ``Global:``
    block.
    """
    lines = content.splitlines()
    result: list[str] = []
    in_kept_section = True  # Header block (before first ##) is always kept
    in_phase_subsection = False

    for line in lines:
        if line.startswith("## "):
            section_name = line[3:].strip()
            in_kept_section = section_name in keep_sections
            in_phase_subsection = False
            if in_kept_section:
                result.append(line)
            continue

        if not in_kept_section:
            continue

        # For Work Manifest: skip Phase N subsections within Committed
        # Design Decisions, keeping only Global
        if strip_phase_subsections:
            stripped = line.strip()
            if stripped.startswith(_PHASE_SUBSECTION_PREFIX) and stripped.endswith(":"):
                in_phase_subsection = True
                continue
            if stripped.startswith("Global:"):
                in_phase_subsection = False
            if in_phase_subsection:
                continue

        result.append(line)

    text = "\n".join(result).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def compact_artifact_content(content: str) -> str:
    """Compact a focused artifact to essential sections for prompt rendering.

    Returns the content unchanged for non-artifact items (plans, unknown
    content).  Known artifact types are trimmed to their canonical compact
    stub form.
    """
    if ACTIVE_PLAN_MARKER in content:
        return content  # Plans use their own windowing

    head = content[:200]
    if "# Work Manifest" in head or "MANIFEST ID:" in head:
        return _extract_compact_sections(
            content, _MANIFEST_COMPACT_SECTIONS, strip_phase_subsections=True,
        )
    return content


def compact_focused_memories(
    memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return copies of focused memories with compacted artifact content.

    Non-artifact memories and plans pass through unchanged.  The
    original memory dicts are not mutated — compacted copies are
    created only when the content is actually shortened.
    """
    result: list[dict[str, Any]] = []
    for mem in memories:
        content = mem.get("content", "")
        if isinstance(content, str) and content:
            compacted = compact_artifact_content(content)
            if compacted != content:
                mem = {**mem, "content": compacted}
        result.append(mem)
    return result
