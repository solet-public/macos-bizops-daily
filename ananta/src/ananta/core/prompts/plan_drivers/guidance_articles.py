"""Guidance article loading, section extraction, and text processing.

Pure functions for reading, slicing, and encoding step guidance
articles referenced by plan steps.  No plugin or context dependencies.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ananta.core.plans.types import ParsedPlan, ParsedPlanStep

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

_PLAN_STEP_LINE_RE = re.compile(
    r"^\[([xX> \-])\]\s+\d+\."
    r"|^\s+(?:GUIDANCE_ARTICLE|GUIDANCE_SECTION|PLAYBOOK|PLAYBOOK_SECTION):"
    r"|^\s+[a-z]\) ",
)

_DRIVER_REINFORCEMENT_HEADING = "### Driver Reinforcement"
_MAX_DRIVER_REINFORCEMENT_CHARS = 2000


# ── Protocol ─────────────────────────────────────────────────────────


class GuidanceArticleReader(Protocol):
    """Reads guidance articles by filename."""

    def read_article(self, article_name: str) -> str | None: ...


# ── Article path validation ──────────────────────────────────────────


def validate_article_name(article_name: str) -> bool:
    """Check that an article name is a safe, relative ``.md`` filename."""
    from pathlib import PurePosixPath

    candidate = PurePosixPath(article_name)
    if candidate.is_absolute() or candidate.name != article_name:
        logger.warning(
            "STEP_GUIDANCE: Invalid guidance article reference '%s'",
            article_name,
        )
        return False
    if candidate.suffix != ".md":
        logger.warning(
            "STEP_GUIDANCE: Guidance article must be a .md file: %s",
            article_name,
        )
        return False
    return True


# ── Article resolution ───────────────────────────────────────────────


def resolve_step_guidance_text(
    parsed: ParsedPlan,
    step: ParsedPlanStep,
    reader: GuidanceArticleReader,
) -> str | None:
    """Load and optionally section-slice the guidance article for a step."""
    article_name = step.guidance_article or parsed.plan_guidance_article
    if not article_name:
        return None
    if not validate_article_name(article_name):
        return None
    guidance_text = reader.read_article(article_name)
    if guidance_text is None:
        return None
    section_id = step.guidance_section_id or parsed.plan_guidance_section_id
    if section_id:
        section_text = extract_guidance_section(guidance_text, section_id)
        if section_text:
            preamble = extract_article_preamble(guidance_text)
            guidance_text = (
                f"{preamble}\n\n{section_text}" if preamble else section_text
            )
        else:
            # Fail-fast: a missing GUIDANCE_SECTION means the plan
            # references a non-existent section anchor. Silently
            # injecting the full article causes runaway token usage
            # (Assignment 1 runaway prevention). Log as error and
            # return None so the step proceeds without guidance
            # rather than with an oversized full-article injection.
            logger.error(
                "STEP_GUIDANCE: Section '%s' not found in %s — "
                "skipping guidance (fix the section anchor in the plan "
                "or the heading in the article)",
                section_id,
                article_name,
            )
            return None
    return encode_plan_steps_as_newline_proxy(guidance_text)


# ── Section extraction ───────────────────────────────────────────────


def extract_guidance_section(
    article_text: str,
    section_id: str,
) -> str | None:
    """Extract a markdown section by heading text or slug."""
    lines = article_text.splitlines()
    target_idx: int | None = None
    target_level = 0
    for idx, line in enumerate(lines):
        match = _MARKDOWN_HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        slug = slugify_guidance_section(title)
        if section_id in (title, slug):
            target_idx = idx
            target_level = level
            break
    if target_idx is None:
        return None
    end_idx = len(lines)
    for idx in range(target_idx + 1, len(lines)):
        match = _MARKDOWN_HEADING_RE.match(lines[idx])
        if not match:
            continue
        level = len(match.group(1))
        if level <= target_level:
            end_idx = idx
            break
    return "\n".join(lines[target_idx:end_idx]).rstrip()


def slugify_guidance_section(title: str) -> str:
    """Normalize a heading title into a stable slug."""
    lowered = title.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-")


def extract_article_preamble(article_text: str) -> str:
    """Return content before the first ``##`` or deeper heading.

    This captures the article-level ``# Title`` and any introductory
    paragraph so that section-extracted guidance retains document
    context.  Returns an empty string when no preamble exists.
    """
    lines = article_text.splitlines()
    for idx, line in enumerate(lines):
        match = _MARKDOWN_HEADING_RE.match(line)
        if match and len(match.group(1)) >= 2:
            return "\n".join(lines[:idx]).rstrip()
    return ""


# ── Driver reinforcement ────────────────────────────────────────────


def extract_driver_reinforcement(
    guidance_text: str,
) -> tuple[str, str]:
    """Extract the optional ``### Driver Reinforcement`` subsection.

    Returns ``(reinforcement_text, remaining_guidance)`` where
    ``reinforcement_text`` is the content of the subsection (empty
    string if absent) and ``remaining_guidance`` is the guidance
    text with the subsection stripped.
    """
    heading = _DRIVER_REINFORCEMENT_HEADING
    lines = guidance_text.splitlines()
    start_idx: int | None = None
    heading_level = 3  # ### = level 3

    for idx, line in enumerate(lines):
        if line.strip() == heading.strip():
            start_idx = idx
            break

    if start_idx is None:
        return "", guidance_text

    # Find the end of the subsection (next heading at same or higher level)
    end_idx = len(lines)
    for idx in range(start_idx + 1, len(lines)):
        match = _MARKDOWN_HEADING_RE.match(lines[idx])
        if match and len(match.group(1)) <= heading_level:
            end_idx = idx
            break

    # Extract the reinforcement content (skip the heading line itself)
    reinforcement_lines = lines[start_idx + 1 : end_idx]
    reinforcement = "\n".join(reinforcement_lines).strip()

    if len(reinforcement) > _MAX_DRIVER_REINFORCEMENT_CHARS:
        logger.warning(
            "DRIVER_REINFORCEMENT: %d chars exceeds %d char threshold -- "
            "consider trimming the ### Driver Reinforcement subsection",
            len(reinforcement),
            _MAX_DRIVER_REINFORCEMENT_CHARS,
        )

    # Rebuild guidance without the reinforcement subsection
    remaining_lines = lines[:start_idx] + lines[end_idx:]
    remaining = "\n".join(remaining_lines).rstrip()

    return reinforcement, remaining


# ── Newline proxy encoding ───────────────────────────────────────────


def encode_plan_steps_as_newline_proxy(text: str) -> str:
    r"""Encode plan step blocks with U+2424 line separators.

    Plan step lines (``[X] N.``, ``[>] N.``, ``[ ] N.``, ``[-] N.``),
    indented step metadata, ``    a) ...`` sub-steps, and blank lines
    between steps are joined with U+2424 so the model sees the encoding
    it should use in ``upsert_plan`` content strings.
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        if _PLAN_STEP_LINE_RE.match(lines[i]):
            block, i = _gather_plan_step_block(lines, i)
            had_trailing_blank = bool(block and block[-1] == "")
            while block and block[-1] == "":
                block.pop()
            result.append("\u2424".join(block))
            if had_trailing_blank:
                result.append("")
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


def _gather_plan_step_block(lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect lines belonging to one plan-step block starting at ``start``.

    Returns ``(block_lines, next_index)``. A block continues across step
    headers, blank lines, and indented continuation lines until a non-step
    flush-left line breaks it.
    """
    block: list[str] = [lines[start]]
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if (
            _PLAN_STEP_LINE_RE.match(line)
            or line == ""
            or line.startswith((" ", "\t"))
        ):
            block.append(line)
            i += 1
        else:
            break
    return block, i
