"""Canonical plan parser and normalizer.

Single parser that replaces the three partial parsers in:
- default_thinking_plugin (step markers, content parsing)
- default_inference_plugin (process key extraction, step narrowing)
- context.py (playbook hydration)

All plan consumers must use this module.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ananta.core.plans.types import (
    BoundSubStep,
    ComposedReference,
    LayerPolicy,
    ParsedPlan,
    ParsedPlanStep,
)
from ananta.core.result_processing import ResultProcessorKind

if TYPE_CHECKING:
    from ananta.core.plans.types import StepMarker

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Canonical step header: [X] 1. or [ ] 2. etc.
STEP_HEADER_RE = re.compile(r"^\[([xX> \-])\]\s+(\d+)\.", re.MULTILINE)

# Process keys in parentheses: (service_interface::...) or (<POST_MESSAGE>)
_PROCESS_KEY_RE = re.compile(
    r"\(((?:plugin|service_interface)::[^)]+|<[A-Z_]+>)\)",
)
# Bare pseudo-keys without parentheses: <POST_MESSAGE>, <ORIGIN_IO>, etc.
# Models sometimes drop the surrounding parens when authoring plan text.
_BARE_PSEUDO_KEY_RE = re.compile(r"(?<!\()(<[A-Z_]+>)(?!\))")

# Playbook metadata on indented sub-lines
_PLAYBOOK_RE = re.compile(r"^\s+PLAYBOOK:\s*(\S+)", re.MULTILINE)
_PLAYBOOK_SECTION_RE = re.compile(r"^\s+PLAYBOOK_SECTION:\s*(\w+)", re.MULTILINE)
_GUIDANCE_ARTICLE_RE = re.compile(r"^\s+GUIDANCE_ARTICLE:\s*(\S+)", re.MULTILINE)
_GUIDANCE_SECTION_RE = re.compile(r"^\s+GUIDANCE_SECTION:\s*(\S+)", re.MULTILINE)
_SUPPORT_ARTICLES_RE = re.compile(r"^\s+SUPPORT_ARTICLES:\s*(.+)", re.MULTILINE)
_MIN_ACTIONS_RE = re.compile(r"^\s+MIN_ACTIONS:\s*(\d+)", re.MULTILINE)
# Step-level result-processor kind: ``RESULT_PROCESSOR_KIND: inference`` or
# ``RESULT_PROCESSOR_KIND: deterministic_continuation``.  Required for every
# executable Joseki/WBS step (see Section 17.5 landing order); parsing
# accepts missing-annotation today and enforcement turns on once the KB and
# authoring prompts are updated.
_RESULT_PROCESSOR_KIND_RE = re.compile(
    r"^\s+RESULT_PROCESSOR_KIND:\s*(\S+)", re.MULTILINE,
)
# Step-level pull-mode auto-submission opt-in (Phase 4 / Q15):
# ``AUTO_SAFE: true``.  The flag is an EXPLICIT author marker; the pull
# engine honors it only for steps that also declare
# ``RESULT_PROCESSOR_KIND: deterministic_continuation`` AND pass the full
# deterministic-continuation validation.  Any value other than ``true``
# (case-insensitive) is a parse error — no silent coercion.
_AUTO_SAFE_RE = re.compile(r"^\s+AUTO_SAFE:\s*(\S+)", re.MULTILINE)
# Match an entire LAYER_POLICY: line. Body may carry any of:
#   knowledge_layers=[2, 3]
#   min_knowledge_layer=2
#   max_knowledge_layer=1
#   include_unlayered=true
# Multiple knobs separated by commas or whitespace are allowed.
_LAYER_POLICY_LINE_RE = re.compile(r"^\s+LAYER_POLICY:\s*(.+?)\s*$", re.MULTILINE)
_LAYER_LIST_RE = re.compile(r"knowledge_layers\s*=\s*\[([^\]]*)\]")
_MIN_LAYER_RE = re.compile(r"min_knowledge_layer\s*=\s*(\d+)")
_MAX_LAYER_RE = re.compile(r"max_knowledge_layer\s*=\s*(\d+)")
_INCLUDE_UNLAYERED_RE = re.compile(
    r"include_unlayered\s*=\s*(true|false)", re.IGNORECASE,
)
_PLAN_GUIDANCE_ARTICLE_RE = re.compile(r"^PLAN_GUIDANCE_ARTICLE:\s*(\S+)", re.MULTILINE)
_PLAN_GUIDANCE_SECTION_RE = re.compile(r"^PLAN_GUIDANCE_SECTION:\s*(\S+)", re.MULTILINE)

# Bare numbered steps without markers: "1. Step description"
_BARE_STEP_RE = re.compile(r"^(\d+)\.", re.MULTILINE)

# Sub-step label: a), b), c), etc.
_SUB_STEP_LABEL_RE = re.compile(r"^\s+([a-z])\)\s+")

# Arguments block: indented "Arguments:" followed by single-line JSON
_ARGUMENTS_RE = re.compile(r"^\s+Arguments:\s*$")
_ARGUMENTS_JSON_RE = re.compile(r"^\s*(\{.*\})\s*$")

# Composed reference: cross-step argument derivation
# Examples:
#   Composed: input_midi_file = output_phrase_id from step 1 + "_mid"
#   Composed: input_audio_files = output_phrase_id from steps 1,3,5 + "_wav"
_COMPOSED_RE = re.compile(
    r"^\s+Composed:\s+(\w+)\s*=\s*(\w+)\s+from\s+steps?\s+"
    r"([\d,\s]+)\s*(?:\+\s*\"([^\"]*)\")?\s*$",
)

# Catch-all for any line starting with "Composed:" — used to warn on
# malformed lines that don't match the supported grammar.
_COMPOSED_ANY_RE = re.compile(r"^\s+Composed:\s+")

# Malformed embedded marker: [X] 8. [-] Await USER message
# The outer marker is wrong; the embedded one is the real intent.
_EMBEDDED_MARKER_RE = re.compile(
    r"^\[([xX> \-])\]\s+(\d+)\.\s+\[([xX> \-])\]\s+",
    re.MULTILINE,
)

# Model sometimes collapses canonical structure onto a single line inside
# upsert_plan.arguments.content. Repair that before parsing.
_INLINE_STEP_HEADER_RE = re.compile(
    r"(?<!\n)[ \t]+(?=\[[xX> \-]\]\s+\d+\.)",
)
_INLINE_METADATA_RE = re.compile(
    r"(?<!\n)[ \t]+(?=(?:GUIDANCE_ARTICLE|GUIDANCE_SECTION|SUPPORT_ARTICLES|PLAYBOOK|PLAYBOOK_SECTION|PLAN_GUIDANCE_ARTICLE|PLAN_GUIDANCE_SECTION|MIN_ACTIONS|LAYER_POLICY|RESULT_PROCESSOR_KIND|AUTO_SAFE|JOSEKI):)",
)
_INLINE_SUBSTEP_RE = re.compile(
    r"(?<!\n)[ \t]+(?=[a-z]\)\s+[A-Z<])",
)


# ---------------------------------------------------------------------------
# Content normalization (input boundary)
# ---------------------------------------------------------------------------


def _fix_embedded_markers(text: str) -> str:
    """Rewrite malformed embedded status markers into canonical step headers.

    Fixes lines like ``[>] 8. [-] Await USER message`` →
    ``[-] 8. Await USER message``.  The embedded (inner) marker wins
    because it carries the semantic intent (e.g. skip).
    """
    return _EMBEDDED_MARKER_RE.sub(r"[\3] \2. ", text)


def _rehydrate_inline_plan_structure(text: str) -> str:
    """Split collapsed step headers, metadata, and sub-steps onto new lines.

    Example:
    ``[ ] 7. Step ...    GUIDANCE_ARTICLE: foo.md    a) First action``
    becomes:
    ``[ ] 7. Step ...\n    GUIDANCE_ARTICLE: foo.md\n    a) First action``
    """
    text = _INLINE_STEP_HEADER_RE.sub("\n", text)
    text = _INLINE_METADATA_RE.sub("\n    ", text)
    text = _INLINE_SUBSTEP_RE.sub("\n    ", text)
    return text


def normalize_content(content: str) -> str:
    """Normalize model-submitted plan content into canonical plain text.

    Handles:
    - ``␤`` (U+2424) → newline
    - ``\\n`` literal → newline
    - lone backslash line separators → newline
    - inline step headers / metadata / sub-steps → split onto new lines
    - malformed embedded markers → inner marker wins
    - bare numbered steps → adds ``[ ]`` markers

    Raises ``ValueError`` if the content cannot be parsed as plan steps.
    """
    plan_text = content.replace("\u2424", "\n").replace("\\n", "\n")

    # Model sometimes uses backslash as line separator instead of ␤.
    # Only apply if the result is still a single line (no real newlines).
    if "\n" not in plan_text and "\\" in plan_text:
        plan_text = plan_text.replace("\\", "\n")

    # Repair inline structure before parsing.
    plan_text = _rehydrate_inline_plan_structure(plan_text)

    # Fix malformed embedded markers before any other parsing.
    plan_text = _fix_embedded_markers(plan_text)

    # Already has step markers — accept as canonical
    if STEP_HEADER_RE.search(plan_text):
        return plan_text.strip()

    # Bare numbered steps — add [ ] markers
    if _BARE_STEP_RE.search(plan_text):
        plan_text = _BARE_STEP_RE.sub(r"[ ] \1.", plan_text)
        return plan_text.strip()

    msg = (
        "content must be plan steps in plain text. Example: "
        "[>] 1. First step\n"
        "    a) Sub-step (service_interface::knowledge_service::search)\n"
        "[ ] 2. Next step"
    )
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _normalize_marker(raw: str) -> StepMarker:
    """Normalize a raw marker character to the canonical set."""
    if raw.upper() == "X":
        return "X"
    if raw == ">":
        return ">"
    if raw == "-":
        return "-"
    return " "


# Backtick-quoted segments that may contain parenthesized process keys
# describing future steps, not actions for the current step.
_BACKTICK_RE = re.compile(r"`[^`]*`")


def _extract_process_keys(block_lines: list[str]) -> tuple[str, ...]:
    """Extract all process keys from a step's lines, in declaration order.

    Duplicate keys are preserved so that steps with multiple calls to
    the same process (e.g. two ``ffmpeg_volume`` adjustments) report the
    correct total count for schema ``minItems``/``maxItems``.

    Skips matches inside backtick-quoted strings — these describe
    future-tail content the model should write, not actions for the
    current step.
    """
    keys: list[str] = []
    seen_positions: set[int] = set()
    for line in block_lines:
        cleaned = _BACKTICK_RE.sub("", line)
        # Parenthesized keys: (service_interface::...) or (<POST_MESSAGE>)
        for match in _PROCESS_KEY_RE.finditer(cleaned):
            keys.append(match.group(1).strip())
            seen_positions.add(match.start())
        # Bare pseudo-keys: <POST_MESSAGE> without parens (model sometimes drops them)
        for match in _BARE_PSEUDO_KEY_RE.finditer(cleaned):
            if match.start() not in seen_positions:
                keys.append(match.group(1).strip())
    return tuple(keys)


def _split_header_and_steps(lines: list[str]) -> tuple[tuple[str, ...], list[str]]:
    """Split plan lines into header lines and step lines."""
    header_end = len(lines)
    for i, line in enumerate(lines):
        if STEP_HEADER_RE.match(line):
            header_end = i
            break
    return tuple(lines[:header_end]), lines[header_end:]


def _strip_trailing_blank_lines(block: list[str]) -> list[str]:
    """Remove trailing blank/whitespace-only lines from a step block.

    Inter-step blank separators added by the renderer must not be absorbed
    into ``step.lines``; otherwise each read-modify-write cycle accumulates
    an extra blank line.
    """
    while block and block[-1].strip() == "":
        block.pop()
    return block


def _group_step_blocks(step_lines: list[str]) -> list[tuple[str, int, list[str]]]:
    """Group step lines into (marker, number, lines) blocks."""
    blocks: list[tuple[str, int, list[str]]] = []
    current_marker = ""
    current_num = 0
    current_block: list[str] = []

    for line in step_lines:
        match = STEP_HEADER_RE.match(line)
        if match:
            if current_block:
                blocks.append((
                    current_marker,
                    current_num,
                    _strip_trailing_blank_lines(current_block),
                ))
            current_marker = match.group(1)
            current_num = int(match.group(2))
            current_block = [line]
        elif current_block:
            current_block.append(line)

    if current_block:
        blocks.append((
            current_marker,
            current_num,
            _strip_trailing_blank_lines(current_block),
        ))
    return blocks


# Indented metadata keywords that get four-space normalization
_METADATA_PREFIXES = (
    "GUIDANCE_ARTICLE:",
    "GUIDANCE_SECTION:",
    "SUPPORT_ARTICLES:",
    "MIN_ACTIONS:",
    "PLAYBOOK:",
    "PLAYBOOK_SECTION:",
    "RESULT_PROCESSOR_KIND:",
    "AUTO_SAFE:",
)

# Indented sub-step lines: a) ..., b) ..., etc.
_SUBSTEP_LINE_RE = re.compile(r"^\s+[a-z]\)\s")


def _normalize_indented_plan_line(line: str) -> str:
    """Normalize a single indented plan line (metadata or sub-step).

    - Blank lines → ``""``
    - Metadata lines → four-space indent + stripped content
    - Sub-step lines → four-space indent + stripped content
    - Everything else → right-stripped, unchanged
    """
    stripped = line.rstrip()
    if not stripped:
        return ""
    content = stripped.lstrip()
    if any(content.startswith(prefix) for prefix in _METADATA_PREFIXES):
        return f"    {content}"
    if _SUBSTEP_LINE_RE.match(line):
        return f"    {content}"
    return stripped


def _canonicalize_step_block(block_lines: list[str]) -> list[str]:
    """Canonicalize a step block: normalize indentation, drop internal blanks.

    Returns a clean block with:
    - line 0: step header (trailing whitespace stripped)
    - lines 1+: metadata and sub-steps only, four-space indented, no blanks
    """
    if not block_lines:
        return block_lines
    result = [block_lines[0].rstrip()]
    for line in block_lines[1:]:
        normalized = _normalize_indented_plan_line(line)
        if normalized:
            result.append(normalized)
    return result


def _extract_composed_references(
    block_lines: list[str],
    start: int,
) -> tuple[tuple[ComposedReference, ...], int]:
    """Extract ``Composed:`` references following an Arguments block.

    Scans forward from *start* collecting consecutive ``Composed:``
    lines.  Returns the parsed references and the index of the last
    consumed line.

    Raises ``ValueError`` if a ``Composed:`` line does not match the
    supported grammar.  The only supported form is::

        Composed: target = source from step N + "suffix"
        Composed: target = source from steps N1,N2,... + "suffix"
    """
    refs: list[ComposedReference] = []
    i = start
    while i < len(block_lines):
        # Check if this line is a Composed: line at all
        if not _COMPOSED_ANY_RE.match(block_lines[i]):
            break
        # Try the supported grammar
        match = _COMPOSED_RE.match(block_lines[i])
        if not match:
            msg = (
                "COMPOSED_MALFORMED: line does not match supported grammar "
                "(expected: Composed: target = source from step N + \"suffix\"): "
                f"{block_lines[i].strip()}"
            )
            raise ValueError(msg)
        target_arg = match.group(1)
        source_arg = match.group(2)
        steps_str = match.group(3)
        suffix = match.group(4) or ""
        source_steps = tuple(
            int(s.strip()) for s in steps_str.split(",") if s.strip()
        )
        if not source_steps:
            msg = (
                "COMPOSED_MALFORMED: no valid step numbers: "
                f"{block_lines[i].strip()}"
            )
            raise ValueError(msg)
        refs.append(ComposedReference(
            target_arg=target_arg,
            source_arg=source_arg,
            source_steps=source_steps,
            suffix=suffix,
        ))
        i += 1
    return tuple(refs), i


def _coerce_json_value(value: object) -> object:
    """Pass through WBS JSON values without type coercion.

    Previously coerced string-typed numbers (``"0.1"`` → ``0.1``), but
    this broke processes that require string arguments containing numeric
    content (e.g., ffmpeg_chorus ``delays: "40|60"``).  The execution
    layer's validation handles type checking against the process schema.
    """
    return value


def _parse_arguments_block(
    block_lines: list[str],
    i: int,
    label: str,
) -> tuple[dict[str, object] | None, int]:
    """Parse an Arguments: block starting at block_lines[i].

    Returns (arguments_dict_or_None, new_index) where new_index points
    past the last consumed line.
    """
    import json
    import logging

    logger = logging.getLogger(__name__)
    arguments: dict[str, object] | None = None
    json_match = _ARGUMENTS_JSON_RE.match(block_lines[i])
    if json_match:
        try:
            raw_args = json.loads(json_match.group(1))
            arguments = (
                {k: _coerce_json_value(v) for k, v in raw_args.items()}
                if isinstance(raw_args, dict) else raw_args
            )
        except json.JSONDecodeError:
            logger.warning(
                "Malformed Arguments JSON at sub-step %s: %s",
                label, block_lines[i].strip(),
            )
    return arguments, i


def _extract_bound_sub_steps(
    block_lines: list[str],
) -> tuple[BoundSubStep, ...]:
    """Extract bound sub-steps with optional Arguments and Composed blocks.

    Scans for sub-step lines (``a)``, ``b)``, etc.) that contain a
    process key, then checks whether the following lines carry an
    ``Arguments:`` header and a single-line JSON payload, optionally
    followed by ``Composed:`` cross-step references.

    Lenient: malformed JSON is logged and the sub-step gets
    ``arguments=None``.
    """
    results: list[BoundSubStep] = []
    i = 0
    while i < len(block_lines):
        line = block_lines[i]
        label_match = _SUB_STEP_LABEL_RE.match(line)
        if not label_match:
            i += 1
            continue
        key_match = _PROCESS_KEY_RE.search(line)
        if not key_match:
            i += 1
            continue
        label = label_match.group(1)
        process_key = key_match.group(1)
        arguments: dict[str, object] | None = None
        composed: tuple[ComposedReference, ...] = ()
        if i + 2 < len(block_lines) and _ARGUMENTS_RE.match(block_lines[i + 1]):
            arguments, _ = _parse_arguments_block(block_lines, i + 2, label)
            i += 2
            composed, i = _extract_composed_references(block_lines, i + 1)
            i -= 1
        else:
            composed, end = _extract_composed_references(block_lines, i + 1)
            if composed:
                i = end - 1
        results.append(BoundSubStep(
            label=label,
            process_key=process_key,
            arguments=arguments,
            composed_references=composed,
        ))
        i += 1
    return tuple(results)


def _build_step(raw_marker: str, number: int, block_lines: list[str]) -> ParsedPlanStep:
    """Build a ParsedPlanStep from a raw block."""
    block_lines = _canonicalize_step_block(block_lines)
    block_text = "\n".join(block_lines)
    playbook_match = _PLAYBOOK_RE.search(block_text)
    section_match = _PLAYBOOK_SECTION_RE.search(block_text)
    guidance_article_match = _GUIDANCE_ARTICLE_RE.search(block_text)
    guidance_section_match = _GUIDANCE_SECTION_RE.search(block_text)
    support_articles_match = _SUPPORT_ARTICLES_RE.search(block_text)
    min_actions_match = _MIN_ACTIONS_RE.search(block_text)

    support_articles: tuple[str, ...] = ()
    if support_articles_match:
        raw = support_articles_match.group(1)
        support_articles = tuple(
            a.strip() for a in raw.split(",") if a.strip()
        )

    layer_policy = _parse_layer_policy(block_text)
    result_processor_kind = _parse_result_processor_kind(block_text, number)
    auto_safe = _parse_auto_safe(block_text, number)

    return ParsedPlanStep(
        marker=_normalize_marker(raw_marker),
        number=number,
        lines=tuple(block_lines),
        process_keys=_extract_process_keys(block_lines),
        playbook_id=playbook_match.group(1) if playbook_match else None,
        playbook_section_id=section_match.group(1) if section_match else None,
        guidance_article=(
            guidance_article_match.group(1) if guidance_article_match else None
        ),
        guidance_section_id=(
            guidance_section_match.group(1) if guidance_section_match else None
        ),
        support_articles=support_articles,
        bound_sub_steps=_extract_bound_sub_steps(block_lines),
        min_actions=(
            int(min_actions_match.group(1)) if min_actions_match else None
        ),
        layer_policy=layer_policy,
        result_processor_kind=result_processor_kind,
        auto_safe=auto_safe,
    )


def _parse_auto_safe(block_text: str, step_number: int) -> bool:
    """Extract the step-level ``AUTO_SAFE:`` annotation (Phase 4 / Q15).

    Returns ``False`` when the step has no annotation.  Raises
    ``ValueError`` on a duplicate annotation or on any value other than
    ``true``/``false`` (case-insensitive) — the flag is an explicit
    author opt-in for pull-mode auto-submission, so a malformed value
    must fail loudly rather than silently default.
    """
    matches = _AUTO_SAFE_RE.findall(block_text)
    if not matches:
        return False
    if len(matches) > 1:
        msg = (
            f"AUTO_SAFE_DUPLICATE: step {step_number} declares "
            f"AUTO_SAFE more than once: {matches!r}"
        )
        raise ValueError(msg)
    raw = str(matches[0]).strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    msg = (
        f"AUTO_SAFE_INVALID: step {step_number} declares "
        f"AUTO_SAFE={matches[0]!r}; allowed values: ['true', 'false']"
    )
    raise ValueError(msg)


def _parse_result_processor_kind(
    block_text: str, step_number: int,
) -> ResultProcessorKind | None:
    """Extract the step-level ``RESULT_PROCESSOR_KIND:`` annotation.

    Returns ``None`` when the step has no annotation.  Raises
    ``ValueError`` when the step declares the annotation more than
    once, with a value that is not a known
    :class:`ResultProcessorKind` member, or with
    ``bridge_delivery`` — the latter is platform-set on direct MCP
    invocations only and must never appear in plan or WBS step text
    (handoff 2026-05-10 Section 7).
    """
    matches = _RESULT_PROCESSOR_KIND_RE.findall(block_text)
    if not matches:
        return None
    if len(matches) > 1:
        msg = (
            f"RESULT_PROCESSOR_KIND_DUPLICATE: step {step_number} declares "
            f"RESULT_PROCESSOR_KIND more than once: {matches!r}"
        )
        raise ValueError(msg)
    raw = matches[0].strip()
    try:
        kind = ResultProcessorKind(raw)
    except ValueError as exc:
        allowed = sorted(
            k.value for k in ResultProcessorKind
            if k is not ResultProcessorKind.BRIDGE_DELIVERY
        )
        msg = (
            f"RESULT_PROCESSOR_KIND_INVALID: step {step_number} declares "
            f"RESULT_PROCESSOR_KIND={raw!r}; allowed values: {allowed}"
        )
        raise ValueError(msg) from exc
    if kind is ResultProcessorKind.BRIDGE_DELIVERY:
        msg = (
            f"RESULT_PROCESSOR_KIND_FORBIDDEN: step {step_number} "
            f"declares RESULT_PROCESSOR_KIND='bridge_delivery'; bridge "
            "delivery is platform-set on direct MCP process_call "
            "invocations only and must never appear in plan or WBS "
            "step text."
        )
        raise ValueError(msg)
    return kind


def _parse_layer_policy(block_text: str) -> LayerPolicy | None:
    """Extract a ``LayerPolicy`` from the step's ``LAYER_POLICY:`` line.

    Returns ``None`` when the step has no annotation or the body parses
    empty. Silently drops malformed values; an empty policy is treated as
    "no policy" so a typo cannot accidentally constrain retrieval.
    """
    line_match = _LAYER_POLICY_LINE_RE.search(block_text)
    if line_match is None:
        return None
    body = line_match.group(1)
    policy = LayerPolicy(
        knowledge_layers=_parse_layer_list_field(body),
        min_knowledge_layer=_parse_positive_layer_field(body, _MIN_LAYER_RE),
        max_knowledge_layer=_parse_positive_layer_field(body, _MAX_LAYER_RE),
        include_unlayered=_parse_include_unlayered_field(body),
    )
    return None if policy.is_empty else policy


def _parse_layer_list_field(body: str) -> tuple[int, ...] | None:
    """Parse the ``layers=[...]`` list; return ``None`` on missing/malformed."""
    list_match = _LAYER_LIST_RE.search(body)
    if list_match is None:
        return None
    raw_layers = list_match.group(1)
    try:
        parsed_layers = tuple(
            int(s.strip()) for s in raw_layers.split(",") if s.strip()
        )
    except ValueError:
        return None
    if parsed_layers and all(layer >= 1 for layer in parsed_layers):
        return parsed_layers
    return None


def _parse_positive_layer_field(body: str, regex: Any) -> int | None:
    """Parse a single ``min/max=N`` field; return ``None`` if missing or non-positive."""
    match = regex.search(body)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value >= 1 else None


def _parse_include_unlayered_field(body: str) -> bool | None:
    """Parse the ``include_unlayered=true|false`` flag; return ``None`` if missing."""
    match = _INCLUDE_UNLAYERED_RE.search(body)
    if match is None:
        return None
    return match.group(1).lower() == "true"


def parse(plan_text: str) -> ParsedPlan:
    """Parse plan text into a ``ParsedPlan``.

    The input should be canonical plain text (output of ``normalize_content``
    or stored plan content).  This function:

    1. splits header lines from step lines;
    2. groups step lines into ``ParsedPlanStep`` blocks;
    3. extracts process keys and playbook metadata per step;
    4. validates structural invariants.
    """
    header_lines, step_lines = _split_header_and_steps(plan_text.splitlines())
    header_text = "\n".join(header_lines)
    plan_guidance_article_match = _PLAN_GUIDANCE_ARTICLE_RE.search(header_text)
    plan_guidance_section_match = _PLAN_GUIDANCE_SECTION_RE.search(header_text)

    if not step_lines:
        return ParsedPlan(
            header_lines=header_lines,
            steps=(),
            plan_guidance_article=(
                plan_guidance_article_match.group(1)
                if plan_guidance_article_match else None
            ),
            plan_guidance_section_id=(
                plan_guidance_section_match.group(1)
                if plan_guidance_section_match else None
            ),
        )

    steps = [_build_step(m, n, bl) for m, n, bl in _group_step_blocks(step_lines)]
    _validate_invariants(steps)
    if _is_joseki_wbs_execution_document(header_lines):
        assert_executable_joseki_wbs_steps_declare_kind(tuple(steps))
    return ParsedPlan(
        header_lines=header_lines,
        steps=tuple(steps),
        plan_guidance_article=(
            plan_guidance_article_match.group(1)
            if plan_guidance_article_match else None
        ),
        plan_guidance_section_id=(
            plan_guidance_section_match.group(1)
            if plan_guidance_section_match else None
        ),
    )


def _check_single_active(steps: list[ParsedPlanStep]) -> None:
    """Demote extra active markers — keep only the first ``[>]``.

    The model occasionally emits two ``[>]`` markers when writing a
    fresh plan.  Rather than rejecting the whole plan, downgrade every
    ``[>]`` after the first to ``[ ]`` so the plan is valid.

    Logs a warning so the authoring bug is visible.

    Steps are frozen dataclasses, so demoted steps are replaced in-place
    in the list with new instances.
    """
    import logging

    first_seen = False
    demoted: list[int] = []
    for i, step in enumerate(steps):
        if step.is_active:
            if first_seen:
                from dataclasses import replace
                steps[i] = replace(step, marker=" ")
                demoted.append(step.number)
            else:
                first_seen = True
    if demoted:
        logging.getLogger(__name__).warning(
            "PLAN_PARSE: Demoted extra [>] markers on steps %s to [ ] "
            "— model emitted multiple active markers",
            demoted,
        )


def _renumber_if_needed(steps: list[ParsedPlanStep]) -> None:
    """Renumber steps to be strictly increasing if the model restarted numbering.

    Some WBS documents use per-section local numbering (1-8 Toccata, 1-8
    Allemande, etc.) instead of globally sequential numbers.  Rather than
    crashing, renumber them 1..N and log a warning.  The renumbered text
    lines are also patched so that downstream serialization stays consistent.
    """
    from dataclasses import replace

    needs_renumber = any(
        steps[i].number <= steps[i - 1].number for i in range(1, len(steps))
    )
    if not needs_renumber:
        return

    import logging

    logger = logging.getLogger(__name__)
    old_numbers = [s.number for s in steps]
    for i, step in enumerate(steps):
        new_num = i + 1
        if step.number != new_num:
            # Patch the header line so serialized text matches
            new_lines = list(step.lines)
            if new_lines:
                old_num = step.number
                def _renumber(m: re.Match[str], _old: int = old_num, _new: int = new_num) -> str:
                    return m.group(0).replace(f"{_old}.", f"{_new}.")
                new_lines[0] = STEP_HEADER_RE.sub(
                    _renumber,
                    new_lines[0],
                    count=1,
                )
            steps[i] = replace(step, number=new_num, lines=tuple(new_lines))
    logger.warning(
        "PLAN_PARSE: Renumbered %d steps — original sequence %s "
        "had non-increasing numbers; now 1..%d",
        len(steps),
        old_numbers,
        len(steps),
    )


def _check_no_active_skipped(steps: list[ParsedPlanStep]) -> None:
    """Raise if any step is both active and skipped."""
    skipped_numbers = {s.number for s in steps if s.is_skipped}
    for s in steps:
        if s.marker == ">" and s.number in skipped_numbers:
            msg = f"Step {s.number} cannot be both active [>] and skipped [-]"
            raise ValueError(msg)


def _validate_invariants(steps: list[ParsedPlanStep]) -> None:
    """Enforce plan structural invariants."""
    _check_single_active(steps)
    _renumber_if_needed(steps)
    _check_no_active_skipped(steps)


# Header markers that identify a Joseki/WBS execution document.  Their
# presence in the plan header (or projected fragment header) triggers
# RESULT_PROCESSOR_KIND enforcement on every executable step.
_JOSEKI_WBS_EXECUTION_HEADERS: tuple[str, ...] = (
    "ACTIVE_WBS:",
    "JOSEKI_KEY:",
    "WORK_ITEM:",
)


def _is_joseki_wbs_execution_document(header_lines: tuple[str, ...]) -> bool:
    """Return ``True`` when the header carries a Joseki/WBS execution marker.

    The marker set is exact (string prefix match on the stripped line).
    Once any of these is present, every step with at least one process key
    must declare ``RESULT_PROCESSOR_KIND``.
    """
    for line in header_lines:
        stripped = line.strip()
        if any(stripped.startswith(marker) for marker in _JOSEKI_WBS_EXECUTION_HEADERS):
            return True
    return False


def assert_executable_joseki_wbs_steps_declare_kind(
    steps: tuple[ParsedPlanStep, ...],
) -> None:
    """Raise when an executable Joseki/WBS step omits the annotation.

    A step is *executable* in a Joseki/WBS document when its
    :attr:`process_keys` is non-empty (per handoff Section 6).
    Steps with no process keys (`[-] N. Await USER message`,
    `[X] N. Phase execution segment grafted`) are exempt.
    """
    missing: list[int] = [
        s.number
        for s in steps
        if s.process_keys and s.result_processor_kind is None
    ]
    if missing:
        msg = (
            f"RESULT_PROCESSOR_KIND_MISSING: executable Joseki/WBS step(s) "
            f"{missing} omit the RESULT_PROCESSOR_KIND annotation. Every "
            f"step with a process key in a Joseki/WBS execution document "
            f"must declare exactly one RESULT_PROCESSOR_KIND: "
            f"'inference' or 'deterministic_continuation'."
        )
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Normalization (output)
# ---------------------------------------------------------------------------


def _replace_marker(line: str, new_marker: str) -> str:
    """Replace the marker character in a step header line."""
    match = STEP_HEADER_RE.match(line)
    if not match:
        return line
    full = match.group(0)
    marker_start = match.start(1) - match.start(0)
    marker_end = match.end(1) - match.start(0)
    prefix = full[:marker_start]
    suffix = full[marker_end:]
    rest = line[match.end():]
    return f"{prefix}{new_marker}{suffix}{rest}"


def _render_steps(
    header_lines: tuple[str, ...],
    steps: tuple[ParsedPlanStep, ...],
    marker_map: dict[int, str],
) -> str:
    """Render a plan with updated markers."""
    result: list[str] = list(header_lines)
    for step in steps:
        new_marker = marker_map.get(step.number) or step.marker
        for i, line in enumerate(step.lines):
            if i == 0:
                result.append(_replace_marker(line, new_marker))
            else:
                result.append(line)
    return "\n".join(result)


def build_skip_set(plan: ParsedPlan) -> frozenset[int]:
    """Build the authoritative skip set for a plan.

    Includes all ``[-]`` steps.
    """
    return frozenset(s.number for s in plan.steps if s.is_skipped)


def normalize_for_completed_step(
    plan: ParsedPlan,
    completed_step: int,
    *,
    skip_steps: frozenset[int] | None = None,
) -> str:
    """Mark ``completed_step`` as ``[X]``, activate next executable step.

    Used by ``upsert_plan`` during normal execution flow.

    Args:
        plan: The parsed submitted plan content.
        completed_step: Step number being completed.
        skip_steps: Authoritative set of ``[-]`` step numbers from the
            *existing* focused plan.  When provided, only these steps are
            treated as skipped — any ``[-]`` markers the model introduced
            in the submitted content are ignored.  When ``None`` (new plan
            creation with no prior plan), the submitted plan's own ``[-]``
            markers are trusted.  Use :func:`build_skip_set` to construct
            this set — it excludes ``Await USER message`` checkpoint steps
            so they are treated as real pause steps.

    Rules:
        - steps ≤ ``completed_step`` → ``[X]`` (unless skipped)
        - first non-skipped step after ``completed_step`` → ``[>]``
        - all later non-skipped steps → ``[ ]``
        - skipped steps are always preserved as ``[-]``
    """
    def _is_skipped(step: ParsedPlanStep) -> bool:
        if skip_steps is not None:
            if step.number in skip_steps:
                return True
            # For future steps (beyond completed_step), trust submitted [-]
            # markers.  This covers newly authored skip steps from planning
            # extensions — they aren't in the existing plan's skip set because
            # they didn't exist when the skip set was captured.
            if step.number > completed_step and step.is_skipped:
                return True
            return False
        return step.is_skipped

    # Find next executable step after completed_step
    next_active: int | None = None
    for step in plan.steps:
        if step.number > completed_step and not _is_skipped(step):
            next_active = step.number
            break

    marker_map: dict[int, str] = {}
    for step in plan.steps:
        if _is_skipped(step):
            marker_map[step.number] = "-"
        elif step.number <= completed_step:
            marker_map[step.number] = "X"
        elif step.number == next_active:
            marker_map[step.number] = ">"
        else:
            marker_map[step.number] = " "

    return _render_steps(plan.header_lines, plan.steps, marker_map)


def normalize_for_new_plan_install(plan: ParsedPlan) -> str:
    """Set first executable step to ``[>]``, all others to ``[ ]``.

    Used by planning-originated plan installation (e.g. playbook-driven
    plan generation, WBS-projected plan replacement).  Does NOT
    auto-complete step 1.

    Rules:
    - first non-skipped step → ``[>]``
    - all other non-skipped steps → ``[ ]``
    - ``[-]`` steps are always preserved (including ``Await USER message``
      checkpoint steps — ``advance_plan_markers`` marks them ``[X]`` when
      advancing past them)
    """
    first_executable: int | None = None
    for step in plan.steps:
        if not step.is_skipped:
            first_executable = step.number
            break

    marker_map: dict[int, str] = {}
    for step in plan.steps:
        if step.is_skipped:
            marker_map[step.number] = "-"
        elif step.number == first_executable:
            marker_map[step.number] = ">"
        else:
            marker_map[step.number] = " "

    return _render_steps(plan.header_lines, plan.steps, marker_map)


# ---------------------------------------------------------------------------
# Plan advancement (platform-owned marker bookkeeping)
# ---------------------------------------------------------------------------


def _scan_advancement_targets(
    lines: list[str],
) -> tuple[int | None, int | None, list[int]]:
    """Find the current, next, and intermediate halt-step line indices.

    Scans *lines* for the active ``[>]`` step and the next pending
    ``[ ]`` step.  If a ``[-]`` (await-user) step is encountered
    between them, advancement halts there — the ``[-]`` step becomes
    the next target so the platform stops and waits for user input.
    """
    current_idx: int | None = None
    next_idx: int | None = None
    skipped_indices: list[int] = []

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("[>]") and current_idx is None:
            current_idx = i
        elif stripped.startswith("[ ]") and current_idx is not None:
            next_idx = i
            break
        elif stripped.startswith("[-]") and current_idx is not None:
            # Halt at the await-user step — do not skip past it.
            next_idx = i
            break

    return current_idx, next_idx, skipped_indices


def advance_plan_markers(plan_text: str) -> str | None:
    """Mark the current ``[>]`` step as ``[X]``, activate next step.

    Pure text transformation — no side effects.

    ``[-]`` (await-user) steps halt advancement.  When the next step
    after ``[>]`` is ``[-]``, it becomes the new ``[>]`` and the
    platform stops until user input arrives.

    When the ``[>]`` step is the last executable step (no ``[ ]`` or
    ``[-]`` after it), it is marked ``[X]`` with no new ``[>]``
    activated.  This allows the plan to reach ``is_complete``.

    Returns the modified plan text, or ``None`` if no ``[>]`` step
    exists (plan already complete or no plan).
    """
    lines = plan_text.splitlines()
    current_idx, next_idx, skipped_indices = _scan_advancement_targets(lines)

    if current_idx is None:
        return None

    lines[current_idx] = lines[current_idx].replace("[>]", "[X]", 1)
    for idx in skipped_indices:
        lines[idx] = lines[idx].replace("[-]", "[X]", 1)
    if next_idx is not None:
        # Activate the next step — works for both [ ] and [-] markers.
        line = lines[next_idx]
        line = line.replace("[ ]", "[>]", 1).replace("[-]", "[>]", 1)
        lines[next_idx] = line

    return "\n".join(lines)


def preserve_existing_markers(
    existing: ParsedPlan, submitted: ParsedPlan,
) -> str:
    """Render submitted plan content with markers from the existing plan.

    Existing steps keep their current marker state.  New steps (not in the
    existing plan) default to ``[ ]``.  This ensures the platform controls
    marker state while the model controls step content.
    """
    existing_markers: dict[int, str] = {
        s.number: s.marker for s in existing.steps
    }
    marker_map: dict[int, str] = {}
    for step in submitted.steps:
        if step.number in existing_markers:
            marker_map[step.number] = existing_markers[step.number]
        else:
            marker_map[step.number] = " "
    return _render_steps(submitted.header_lines, submitted.steps, marker_map)


def render_plan_steps(plan: ParsedPlan) -> str:
    """Render plan steps as raw text for knowledge-base storage.

    Strips window metadata (timestamp, ``ACTIVE_PLAN:``, ``Full plan:``
    references) that ``_build_plan_window()`` prepends.  Preserves
    structural headers like ``WORK_MANIFEST:`` and ``ACTIVE_WBS:``
    that are part of the plan content, not window decoration.
    """
    # Keep structural headers (WORK_MANIFEST, ACTIVE_WBS) but strip
    # window headers (timestamp, ACTIVE_PLAN:, Full plan:).
    structural_headers = tuple(
        line for line in plan.header_lines
        if line.strip().startswith(("WORK_MANIFEST:", "ACTIVE_WBS:"))
    )
    marker_map: dict[int, str] = {
        s.number: s.marker for s in plan.steps
    }
    return _render_steps(structural_headers, plan.steps, marker_map)


# ---------------------------------------------------------------------------
# Planning-extension rewrite validation
# ---------------------------------------------------------------------------

# Extracts the step title: everything after "[X] N. " on the first line.
_STEP_TITLE_RE = re.compile(r"^\[[xX> \-]\]\s+\d+\.\s*(.*)$")


def _extract_step_title(step: ParsedPlanStep) -> str:
    """Return the step's title text, stripped of marker and number."""
    first_line = step.lines[0] if step.lines else ""
    match = _STEP_TITLE_RE.match(first_line)
    return match.group(1).strip() if match else first_line.strip()


def _check_prefix_preserved(
    prefix_steps: list[ParsedPlanStep],
    submitted_by_number: dict[int, ParsedPlanStep],
    boundary: int,
) -> str | None:
    """Check that all immutable prefix steps exist and match."""
    for existing_step in prefix_steps:
        submitted_step = submitted_by_number.get(existing_step.number)
        if submitted_step is None:
            return (
                f"Step {existing_step.number} is missing from the "
                f"submitted plan. Steps 1-{boundary - 1} "
                f"must be preserved."
            )
        existing_title = _extract_step_title(existing_step)
        submitted_title = _extract_step_title(submitted_step)
        if existing_title != submitted_title:
            return (
                f"Step {existing_step.number} title changed. "
                f"Expected: {existing_title!r}  "
                f"Got: {submitted_title!r}  "
                f"Steps 1-{boundary - 1} must be preserved."
            )
    return None


def _check_no_prefix_insertions(
    submitted: ParsedPlan,
    existing_prefix_numbers: frozenset[int],
    boundary: int,
) -> str | None:
    """Check no extra steps were inserted before the boundary."""
    for step in submitted.steps:
        if step.number < boundary and step.number not in existing_prefix_numbers:
            return (
                f"Step {step.number} was inserted before the "
                f"rewrite boundary. "
                f"Steps 1-{boundary - 1} must be preserved."
            )
    return None


def validate_planning_extension_rewrite(
    existing: ParsedPlan,
    submitted: ParsedPlan,
    rewrite_boundary: int,
) -> str | None:
    """Validate that a planning-extension rewrite preserves the prefix.

    A planning-extension step may only rewrite steps **at or after**
    ``rewrite_boundary``.  Steps before the boundary must remain
    structurally unchanged: same step numbers and same titles.

    Returns:
        ``None`` if valid, or an error message describing the violation.
    """
    prefix_steps = [
        s for s in existing.steps if s.number < rewrite_boundary
    ]
    submitted_by_number = {s.number: s for s in submitted.steps}

    error = _check_prefix_preserved(
        prefix_steps, submitted_by_number, rewrite_boundary,
    )
    if error:
        return error

    error = _check_no_prefix_insertions(
        submitted,
        frozenset(s.number for s in prefix_steps),
        rewrite_boundary,
    )
    if error:
        return error

    has_tail = any(
        s.number >= rewrite_boundary for s in submitted.steps
    )
    if not has_tail:
        return (
            f"No authored tail found starting at Step "
            f"{rewrite_boundary}. The planning-extension must "
            f"author the future tail."
        )

    return None
