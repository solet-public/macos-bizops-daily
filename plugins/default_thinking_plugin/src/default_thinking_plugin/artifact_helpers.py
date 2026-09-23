"""Artifact helpers — static utility functions for artifact authoring.

Pure functions extracted from the thinking plugin for artifact content
manipulation, validation, and formatting.
"""

from __future__ import annotations

import json
import logging
import re

# ── Playbook section helpers ──────────────────────────────────────

SECTION_ID_RE = re.compile(r"<!--\s*section:\s*(\w+)\s*-->")


def extract_section(playbook_text: str, section_id: str) -> str:
    """Extract a section from a playbook by section ID.

    Raises ValueError if the section ID is not found.
    """
    lines = playbook_text.splitlines(keepends=True)
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        match = SECTION_ID_RE.search(line)
        if match:
            if start_idx is not None:
                end_idx = i
                break
            if match.group(1) == section_id:
                start_idx = i + 1

    if start_idx is None:
        msg = f"Section ID '{section_id}' not found in playbook"
        raise ValueError(msg)

    return "".join(lines[start_idx:end_idx]).strip()


def list_section_ids(playbook_text: str) -> list[str]:
    """Return all section IDs found in a playbook, in document order."""
    return SECTION_ID_RE.findall(playbook_text)


# ── Artifact content helpers ──────────────────────────────────────


def validate_no_unresolved_placeholders(content: str, artifact_type: str) -> None:
    """Raise ValueError if content contains unresolved placeholder markers.

    Catches both ``<<UPPERCASE>>`` and ``<lowercase>`` conventions,
    normalizing to uppercase for comparison.
    """
    # Match <<WORD>> or <word> (but not HTML-like tags such as <br> or <div>)
    uppercase = re.findall(r"<<[A-Z_]+>>", content)
    lowercase = re.findall(r"<([a-z][a-z0-9_]*)>", content)
    # Normalize lowercase to <<UPPERCASE>> for consistent reporting
    normalized = [f"<<{p.upper()}>>" for p in lowercase]
    placeholders = uppercase + normalized
    if placeholders:
        unique = sorted(set(placeholders))
        msg = f"{artifact_type} contains unresolved placeholders: {', '.join(unique)}"
        raise ValueError(msg)


def validate_arguments_labels(content: str, artifact_id: str) -> None:
    """Reject WBS content with bare JSON after sub-steps (missing Arguments: label).

    Every sub-step's JSON block must be preceded by an ``Arguments:``
    label line.  A flush JSON line after the sub-step description is
    invalid and will not be parsed by the plan parser.
    """
    lines = content.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        # Check if the previous non-blank line is a sub-step (a), b), etc.)
        # rather than an Arguments: label
        prev_idx = i - 1
        while prev_idx >= 0 and not lines[prev_idx].strip():
            prev_idx -= 1
        if prev_idx < 0:
            continue
        prev_stripped = lines[prev_idx].strip()
        if re.match(r"^[a-z]\)\s+", prev_stripped) and "Arguments:" not in prev_stripped:
            raise ValueError(f"{artifact_id}: bare JSON at line {i + 1} after sub-step — missing 'Arguments:' label. Every JSON block must be preceded by an indented 'Arguments:' line.")


def extract_manifest_title(content: str) -> str:
    """Extract the Title field from Work Manifest content."""
    for line in content.splitlines():
        if line.startswith("Title:"):
            return line[len("Title:") :].strip()[:200]
    return content[:200]


def extract_fenced_block(text: str, language: str) -> str:
    """Extract content from a fenced code block with the given language tag.

    Returns the content between ``` markers, or the full text if no
    fence is found.
    """
    pattern = re.compile(
        rf"```{re.escape(language)}\s*\n(.*?)\n```",
        re.DOTALL,
    )
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


# Reserve ~200 chars for the chunk preamble that the knowledge plugin
# prepends (Source line, Article Role, Article Tags).  The embedding
# limit is 3000, so sections must stay under 2800 to leave room.
_SECTION_WARN_CHARS = 2500
_SECTION_HARD_LIMIT_CHARS = 2800

_SECTION_SPLIT_RE = re.compile(r"(?m)^(#{1,4}\s)")


def validate_section_sizes(
    content: str,
    artifact_id: str,
    *,
    warn_chars: int = _SECTION_WARN_CHARS,
    hard_limit_chars: int = _SECTION_HARD_LIMIT_CHARS,
) -> None:
    """Reject generated artifacts with oversized sections.

    Splits on ``#``, ``##``, and ``###`` headers and measures each section.
    Raises ``ValueError`` naming the offending section if any exceeds
    *hard_limit_chars*.  Logs a warning for sections between
    *warn_chars* and *hard_limit_chars*.
    """
    logger = logging.getLogger(__name__)

    parts = _SECTION_SPLIT_RE.split(content)
    current = parts[0]
    i = 1
    while i < len(parts):
        header_marker = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        section = header_marker + body
        i += 2

        if len(current) + len(section) > warn_chars and current.strip():
            _check_section(current.strip(), artifact_id, warn_chars, hard_limit_chars, logger)
            current = section
        else:
            current += section

    if current.strip():
        _check_section(current.strip(), artifact_id, warn_chars, hard_limit_chars, logger)


def _check_section(
    section: str,
    artifact_id: str,
    warn_chars: int,
    hard_limit_chars: int,
    logger: logging.Logger,
) -> None:
    size = len(section)
    if size <= warn_chars:
        return
    heading = section.split("\n", 1)[0].strip()[:80]
    if size > hard_limit_chars:
        raise ValueError(f'Artifact {artifact_id}: section "{heading}" is {size} chars (limit {hard_limit_chars}). Split into smaller sections with ##, ###, or #### headers.')
    logger.warning(
        'Artifact %s: section "%s" is %d chars (target <%d)',
        artifact_id,
        heading,
        size,
        warn_chars,
    )


def build_section_index(
    content: str,
    artifact_id: str,
    artifact_type: str,
) -> list[dict[str, str | int]]:
    """Build a sidecar section index for a generated artifact.

    Returns a list of dicts with section heading, ordinal, and char count.
    """
    parts = _SECTION_SPLIT_RE.split(content)
    sections: list[dict[str, str | int]] = []
    current = parts[0]
    ordinal = 0
    i = 1
    while i < len(parts):
        header_marker = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        section = header_marker + body
        i += 2

        if len(current) + len(section) > _SECTION_WARN_CHARS and current.strip():
            heading = current.strip().split("\n", 1)[0].strip()[:80]
            sections.append(
                {
                    "artifact_id": artifact_id,
                    "artifact_type": artifact_type,
                    "section_heading": heading,
                    "section_ordinal": ordinal,
                    "char_count": len(current.strip()),
                }
            )
            ordinal += 1
            current = section
        else:
            current += section

    if current.strip():
        heading = current.strip().split("\n", 1)[0].strip()[:80]
        sections.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "section_heading": heading,
                "section_ordinal": ordinal,
                "char_count": len(current.strip()),
            }
        )

    return sections


# ── WBS phase-scope validators ───────────────────────────────────

_PHASE_HEADER_RE = re.compile(
    r"^##\s+Phase\s+(\d+)\.",
    re.MULTILINE,
)


def validate_wbs_phase_containment(
    content: str,
    wbs_id: str,
    phase_number: int,
) -> None:
    """Reject a generated WBS that contains content from the wrong phase.

    Checks that the WBS has exactly one ``## Phase N.`` header matching
    the requested phase and no headers for other phases.  Raises
    ``ValueError`` naming the violation.
    """
    phase_headers = _PHASE_HEADER_RE.findall(content)
    if not phase_headers:
        # No phase headers at all — acceptable for joseki-scoped fragments
        return

    wrong_phases = [int(h) for h in phase_headers if int(h) != phase_number]
    if wrong_phases:
        raise ValueError(f"WBS {wbs_id} requested for phase {phase_number} contains content for phase(s) {sorted(set(wrong_phases))}. Each WBS must be scoped to exactly one phase.")

    matching = [int(h) for h in phase_headers if int(h) == phase_number]
    if len(matching) > 1:
        raise ValueError(f"WBS {wbs_id} contains {len(matching)} '## Phase {phase_number}.' headers (expected exactly 1). Duplicate phase headers indicate a malformed WBS structure.")


def validate_work_item_terminal_steps(content: str, wbs_id: str) -> None:
    """Reject a WBS where any Work Item lacks a terminal step-state record.

    A work item without a terminal ``record_work_breakdown_structure_step_state``
    step can never be formally completed, causing the per-work-item graft
    projector to loop on that item forever.

    Only validates when the WBS contains ``### Work Item`` headers (phase-level
    WBS documents).  Joseki-scoped fragments without work items are skipped.
    """
    from ananta.core.plans.projection import parse_work_items

    items = parse_work_items(content)
    if not items:
        return  # No work items — joseki fragment or non-WBS

    missing: list[str] = []
    for item in items:
        if item.terminal_step is None:
            missing.append(f"Work Item {item.number}: {item.title}")

    if missing:
        raise ValueError(f"WBS {wbs_id} has work items without a terminal record_work_breakdown_structure_step_state step: {', '.join(missing)}. Each work item must end with a step-state record so per-work-item graft can detect completion.")


def normalize_authored_markdown(content: str) -> str:
    """Strip leading blank lines before the first markdown heading.

    Only strips if the content starts with a heading after stripping;
    otherwise returns unmodified.
    """
    stripped = content.lstrip("\n\r")
    if stripped.startswith("#"):
        return stripped
    return content


# ── Markdown section extraction ────────────────────────────────────

_HEADER_RE = re.compile(r"^(#{1,4})\s+(.*)", re.MULTILINE)


def extract_markdown_section(
    content: str,
    heading: str,
    *,
    exact: bool = True,
) -> str:
    """Extract a section from a markdown document by heading match.

    Finds the first header line (``#`` through ``####``) whose text
    matches *heading* after stripping leading hash marks and whitespace.

    When *exact* is ``True`` (default), requires case-insensitive
    equality between the stripped heading text and *heading*.  When
    ``False``, uses case-insensitive substring containment.

    Returns everything from the matched header (inclusive) to the next
    same-level-or-higher header (exclusive) or end of document.  Returns
    empty string if no match is found.
    """
    heading_lower = heading.lower().strip()
    matched_start: int | None = None
    matched_level: int = 0

    for m in _HEADER_RE.finditer(content):
        level = len(m.group(1))
        text = m.group(2).strip().lower()

        if matched_start is None:
            # Looking for the target heading
            hit = (text == heading_lower) if exact else (heading_lower in text)
            if hit:
                matched_start = m.start()
                matched_level = level
        else:
            # Found the target — look for the closing header
            if level <= matched_level:
                return content[matched_start : m.start()].rstrip("\n") + "\n"

    if matched_start is not None:
        return content[matched_start:].rstrip("\n") + "\n"
    return ""




# ── Post-generation structural validators (P4) ───────────────────


def validate_artifact_structure(
    content: str,
    artifact_type: str,
    artifact_id: str,
) -> list[str]:
    """Run per-family structural checks beyond section size.

    Returns a list of validation error strings.  Empty list means
    the artifact passes all structural checks.
    """
    errors: list[str] = []

    if artifact_type == "complete_brief":
        errors.extend(_validate_brief_structure(content, artifact_id))
    elif artifact_type == "work_breakdown_structure":
        errors.extend(_validate_wbs_structure(content, artifact_id))
    elif artifact_type == "work_manifest":
        errors.extend(_validate_manifest_structure(content, artifact_id))

    return errors


def _validate_brief_structure(content: str, artifact_id: str) -> list[str]:
    """Validate Complete Brief structural requirements."""
    errors: list[str] = []
    if not content.startswith("#"):
        errors.append(f"Brief {artifact_id}: must start with # heading")
    if "<<" in content and ">>" in content:
        errors.append(f"Brief {artifact_id}: contains unresolved placeholders")
    return errors


def _validate_wbs_structure(content: str, artifact_id: str) -> list[str]:
    """Validate WBS structural requirements."""
    errors: list[str] = []
    if not content.startswith("# Work Breakdown Structure"):
        errors.append(f"WBS {artifact_id}: first line must be '# Work Breakdown Structure'")
    phase_headers = re.findall(r"^## Phase (\d+)", content, re.MULTILINE)
    if len(set(phase_headers)) > 1:
        errors.append(f"WBS {artifact_id}: contains multiple phases ({', '.join(sorted(set(phase_headers)))}). Each WBS document should cover exactly one phase.")
    return errors


def _validate_manifest_structure(
    content: str,
    artifact_id: str,
) -> list[str]:
    """Validate Work Manifest structural requirements."""
    errors: list[str] = []
    if not content.startswith("# Work Manifest"):
        errors.append(f"Manifest {artifact_id}: first line must be '# Work Manifest'")
    work_item_re = re.compile(r"^### Work Item", re.MULTILINE)
    step_re = re.compile(r"^####\s+\d+\.", re.MULTILINE)
    if work_item_re.search(content):
        errors.append(f"Manifest {artifact_id}: contains Work Item sections (work items belong in the WBS, not the manifest)")
    if step_re.search(content):
        errors.append(f"Manifest {artifact_id}: contains executable step numbers (steps belong in the WBS, not the manifest)")
    return errors


_SUBSTEP_PROCESS_KEY_RE = re.compile(
    r"\(([a-z_]+(?:::[a-z_]+){2,})\)\s*$",
    re.IGNORECASE,
)


def _skip_blank_lines(lines: list[str], start: int) -> int:
    """Advance past blank lines; return the index of the first non-blank line."""
    j = start
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j


def _scan_arguments_block(
    lines: list[str], start: int,
) -> tuple[dict[str, object] | None, int]:
    """Scan past an ``Arguments:`` header; return (parsed dict, JSON line index).

    *start* must point to the line immediately after the sub-step line. The
    returned index is the line holding the JSON payload, so a caller can resume
    scanning (e.g. for the sub-step's trailing ``Composed:`` lines) from just
    after it. Returns ``(None, -1)`` when no valid block is found.
    """
    j = _skip_blank_lines(lines, start)
    if j >= len(lines) or lines[j].strip() != "Arguments:":
        return None, -1
    j = _skip_blank_lines(lines, j + 1)
    if j >= len(lines) or not lines[j].strip().startswith("{"):
        return None, -1
    try:
        args = json.loads(lines[j].strip())
    except json.JSONDecodeError:
        return None, -1
    return (args if isinstance(args, dict) else None), j


def _scan_arguments_json(lines: list[str], start: int) -> dict[str, object] | None:
    """Scan past an ``Arguments:`` header and parse the JSON payload.

    *start* must point to the line immediately after the sub-step line.
    Returns the parsed dict or None if no valid block is found.
    """
    args, _ = _scan_arguments_block(lines, start)
    return args


_COMPOSED_TARGET_RE = re.compile(r"^\s+Composed:\s+(\w+)\s*=", re.MULTILINE)


def _scan_trailing_composed_targets(lines: list[str], start: int) -> set[str]:
    """Collect target args from the consecutive ``Composed:`` lines at *start*.

    Mirrors the core parser's ``_extract_composed_references`` scoping: only the
    ``Composed:`` lines immediately (consecutively) following a sub-step's
    ``Arguments:`` JSON belong to that sub-step. The scan stops at the first line
    that is not a canonical ``Composed: <target> = ...`` line (a blank line, the
    next sub-step, a step header, or a malformed ``Composed:`` line). A line the
    regex misses only under-extracts, which is the safe direction for a
    required-presence check — it can re-fire the pre-B1 false-positive but never
    mask a genuine miss.
    """
    targets: set[str] = set()
    j = start
    while j < len(lines):
        match = _COMPOSED_TARGET_RE.match(lines[j])
        if match is None:
            break
        targets.add(match.group(1))
        j += 1
    return targets


def parse_wbs_process_arguments(
    content: str,
) -> list[tuple[str, dict[str, object]]]:
    """Extract (process_key, arguments_dict) pairs from a WBS document.

    Walks every sub-step line of the form ``a) ... (process::key)`` and
    collects the associated ``Arguments:`` JSON block. Returns one tuple
    per sub-step that has both a recognizable process key and a parseable
    JSON arguments block.

    Lines with no trailing process key or no subsequent Arguments block
    are silently skipped (planning/document steps have neither).
    """
    lines = content.split("\n")
    result: list[tuple[str, dict[str, object]]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        m = _SUBSTEP_PROCESS_KEY_RE.search(stripped)
        if m and re.match(r"^[a-z]\)\s+", stripped):
            args = _scan_arguments_json(lines, i + 1)
            if args is not None:
                result.append((m.group(1), args))
    return result


def parse_wbs_substep_bindings(
    content: str,
) -> list[tuple[str, dict[str, object], set[str]]]:
    """Extract ``(process_key, arguments, composed_targets)`` per invocation sub-step.

    Like :func:`parse_wbs_process_arguments`, but additionally correlates each
    sub-step's ``Composed:`` target args to THAT sub-step — the consecutive
    ``Composed:`` lines immediately following the sub-step's ``Arguments:`` JSON
    — instead of a document-wide union. This mirrors the core parser's
    per-sub-step ``bound_sub_steps[...].composed_references`` scoping without
    invoking ``parse()`` (which would newly enforce ``RESULT_PROCESSOR_KIND``
    presence on RPK-omitting joseki-authoring documents — the regression the
    Phase-3 storage-path B1 deviation deliberately avoided).

    Used by the storage-path required-argument presence check so a ``Composed:``
    target on sub-step Y can no longer mask a genuinely-missing required argument
    of the same name on sub-step X.

    Only sub-steps that carry an ``Arguments:`` block are returned (matching the
    required-argument gate: a sub-step with no Arguments block is a
    planning/document step, not an invocation). Canonical ``Composed:`` format is
    required for extraction — an indented ``Composed: <target> = ...`` line
    directly after the Arguments JSON; non-canonical formatting under-extracts,
    which only re-fires the pre-B1 false-positive (the safe direction) and never
    masks a genuine miss.
    """
    lines = content.split("\n")
    result: list[tuple[str, dict[str, object], set[str]]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        m = _SUBSTEP_PROCESS_KEY_RE.search(stripped)
        if m is None or not re.match(r"^[a-z]\)\s+", stripped):
            continue
        args, json_index = _scan_arguments_block(lines, i + 1)
        if args is None:
            continue
        composed = _scan_trailing_composed_targets(lines, json_index + 1)
        result.append((m.group(1), args, composed))
    return result
