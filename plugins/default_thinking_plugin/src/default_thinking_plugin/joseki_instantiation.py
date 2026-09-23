"""Mechanical joseki-card → WBS instantiation (the ``run_joseki`` driver, §4.1).

Turns a registered joseki card + concrete bindings into a joseki-scoped WBS
document ready for ``register_authored_work_breakdown_structure`` — with NO
inference and NO judgment. Design origin: checkout-local design spec
(``workbench/2026-07-05_run_joseki_driver_design_spec.md`` §4.1, v3 review-CLEAR).

ADMISSION RULE (mirrors the pull engine's Q15 posture): v1 instantiates ONLY
cards that are CLOSED-WORLD under the given bindings. A card that needs
judgment to instantiate is a typed ``JOSEKI_NOT_MECHANIZABLE`` rejection that
NAMES every blocker — it is never silently routed to inference. Blockers:

* a ``<<BIND:name>>`` token with no matching binding (and, symmetrically, a
  supplied binding no token consumes — almost always a caller typo);
* any unresolved ``<<…>>`` / ``<placeholder>`` token after substitution;
* an executable step missing ``RESULT_PROCESSOR_KIND:`` or process keys the
  card's plan syntax cannot express mechanically;
* an ``Arguments:`` block that does not parse as a JSON object;
* a child-joseki reference (v1 scope line: inline child expansion is a
  follow-up; none of the campaign's target cards compose children — flagged
  to review as a deliberate deviation from the WBS-authoring guidance's
  expand-children rule).

The emitted document APPENDS the terminal ``complete_joseki_run`` step
(deterministic continuation, bound to the pre-minted ``wbs_id``) so run
completion is itself a plan step (spec §0/F2), and re-parses its own output
with the CORE plan parser (``core/plans/parser.py`` — never a parallel
parser) as a structural self-check before returning.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ananta.core.plans.parser import parse
from ananta.error_handling import FrameworkError

from default_thinking_plugin.constants import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ananta.core.plans.types import ParsedPlan

# The terminal step appended to every instantiated run WBS. Completion of the
# run is itself a plan step: EDGE_SINK verb, resolves the run row by wbs_id.
_COMPLETE_RUN_PROCESS_KEY = "service_interface::thinking_service::complete_joseki_run"

_BIND_TOKEN_RE = re.compile(r"<<BIND:([A-Za-z0-9_.-]+)>>")
_UNRESOLVED_TOKEN_RE = re.compile(r"<<[^>]*>>|<[a-z_]+>")
_SEQUENCE_HEADER_RE = re.compile(r"^##\s+Sequence\s*$", re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r"^##\s+\S", re.MULTILINE)
_CHILD_JOSEKI_RE = re.compile(r"joseki/[a-z0-9_]+\.md|JOSEKI:\s*\S", re.IGNORECASE)
_ARGUMENTS_LINE_RE = re.compile(r"^\s*Arguments:\s*$")
_STEP_HEADER_RE = re.compile(r"^\[ \] (\d+)\.")


@dataclass(frozen=True)
class InstantiatedWbs:
    """The mechanical instantiation product."""

    wbs_id: str
    content: str
    executable_step_count: int
    terminal_step_number: int


def mint_run_wbs_id(joseki_key: str) -> str:
    """A caller-supplied WBS id for one run: ``wbs-jrun-<key>-<suffix>``.

    The id is minted BEFORE registration so the terminal step can bind it —
    ``register_authored_work_breakdown_structure`` takes the id by value
    (verified against the composition-path convention: the id lives in the
    authored document header).
    """
    suffix = uuid.uuid4().hex[:12]
    slug = re.sub(r"[^a-z0-9_]+", "_", joseki_key.lower())[:40]
    return f"wbs-jrun-{slug}-{suffix}"


def instantiate_joseki_wbs(
    *,
    card_content: str,
    joseki_key: str,
    bindings: Mapping[str, object],
    wbs_id: str,
    manifest_id: str,
) -> InstantiatedWbs:
    """Instantiate *card_content* into a registered-ready run WBS document.

    Raises ``FrameworkError(JOSEKI_NOT_MECHANIZABLE)`` with EVERY blocker
    named when the card is not closed-world under *bindings*.
    """
    sequence = _extract_sequence(card_content, joseki_key)
    substituted, blockers = _substitute_bindings(sequence, bindings)
    blockers.extend(_static_blockers(substituted))

    parsed = parse(substituted) if not blockers else None
    if parsed is not None:
        blockers.extend(_parsed_blockers(parsed))

    if blockers or parsed is None:
        bullets = "\n".join(f"  - {b}" for b in blockers)
        raise FrameworkError(
            message=(
                f"joseki {joseki_key!r} is not mechanizable under the given "
                f"bindings ({len(blockers)} blocker(s)) — author the card "
                f"closed-world or fix the bindings:\n{bullets}"
            ),
            error_code=ErrorCode.JOSEKI_NOT_MECHANIZABLE,
        )

    executable_steps = [s for s in parsed.steps if s.process_keys]
    terminal_number = max(s.number for s in parsed.steps) + 1
    document = _emit_document(
        joseki_key=joseki_key,
        wbs_id=wbs_id,
        manifest_id=manifest_id,
        sequence=substituted,
        terminal_number=terminal_number,
    )
    _self_check(document, joseki_key, terminal_number)
    return InstantiatedWbs(
        wbs_id=wbs_id,
        content=document,
        executable_step_count=len(executable_steps) + 1,
        terminal_step_number=terminal_number,
    )


# -- extraction and substitution ---------------------------------------------


def _extract_sequence(card_content: str, joseki_key: str) -> str:
    """The card's ``## Sequence`` section body (steps in plan syntax)."""
    header = _SEQUENCE_HEADER_RE.search(card_content)
    if header is None:
        raise FrameworkError(
            message=(
                f"joseki {joseki_key!r} card has no '## Sequence' section — "
                f"not a runnable card"
            ),
            error_code=ErrorCode.JOSEKI_NOT_MECHANIZABLE,
        )
    body_start = header.end()
    next_section = _NEXT_SECTION_RE.search(card_content, body_start)
    body_end = next_section.start() if next_section else len(card_content)
    return card_content[body_start:body_end].strip("\n")


def _substitute_bindings(
    sequence: str, bindings: Mapping[str, object],
) -> tuple[str, list[str]]:
    """Replace ``<<BIND:name>>`` tokens; report unknown/unused as blockers."""
    blockers: list[str] = []
    tokens = set(_BIND_TOKEN_RE.findall(sequence))
    unknown = sorted(tokens - set(bindings))
    unused = sorted(set(bindings) - tokens)
    blockers.extend(
        f"binding slot <<BIND:{name}>> has no supplied binding" for name in unknown
    )
    blockers.extend(
        f"supplied binding {name!r} matches no <<BIND:…>> slot in the card"
        for name in unused
    )

    def _replace(match: re.Match[str]) -> str:
        value = bindings.get(match.group(1))
        return _render_binding(value) if value is not None else match.group(0)

    return _BIND_TOKEN_RE.sub(_replace, sequence), blockers


def _render_binding(value: object) -> str:
    """A binding value rendered into the document.

    Strings substitute verbatim (they land inside JSON string literals or
    prose); everything else renders as compact JSON so lists/numbers/bools
    stay machine-true inside ``Arguments:`` blocks.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


# -- blocker collection --------------------------------------------------------


def _static_blockers(sequence: str) -> list[str]:
    """Text-level blockers: leftover tokens, child joseki, bad Arguments."""
    blockers = [
        f"unresolved placeholder {token!r} after binding substitution"
        for token in sorted(
            {m.group(0) for m in _UNRESOLVED_TOKEN_RE.finditer(sequence)},
        )
    ]
    child = _CHILD_JOSEKI_RE.search(sequence)
    if child is not None:
        blockers.append(
            f"child-joseki reference {child.group(0)!r} — v1 does not expand "
            f"child joseki (author the sequence inline)"
        )
    blockers.extend(_argument_block_blockers(sequence))
    return blockers


def _argument_block_blockers(sequence: str) -> list[str]:
    """Every ``Arguments:`` block must be followed by one JSON object line."""
    blockers: list[str] = []
    lines = sequence.splitlines()
    for index, line in enumerate(lines):
        if not _ARGUMENTS_LINE_RE.match(line):
            continue
        payload = lines[index + 1].strip() if index + 1 < len(lines) else ""
        try:
            parsed = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            step = _nearest_step_number(lines, index)
            blockers.append(
                f"step {step}: Arguments block does not parse as a JSON "
                f"object (got {payload[:60]!r})"
            )
    return blockers


def _nearest_step_number(lines: list[str], index: int) -> str:
    """The ``[ ] N.`` step number governing *index*, for blocker messages."""
    for line in reversed(lines[: index + 1]):
        match = _STEP_HEADER_RE.match(line.strip())
        if match:
            return match.group(1)
    return "?"


def _parsed_blockers(parsed: ParsedPlan) -> list[str]:
    """Structural blockers from the CORE parser's view of the sequence."""
    blockers: list[str] = []
    if not parsed.steps:
        blockers.append("card sequence parsed to zero steps")
    for step in parsed.steps:
        if not step.process_keys:
            continue  # non-executable control step (await_user) — legal
        if step.result_processor_kind is None:
            blockers.append(
                f"step {step.number}: executable step missing "
                f"RESULT_PROCESSOR_KIND annotation"
            )
        elif step.result_processor_kind.value != "deterministic_continuation":
            # v1 SCOPE (Rev-A build delta-2 F3, option b): inference-kind
            # steps are rejected at AUTHORING time — the v1 stall detector
            # cannot distinguish a durable inference DEFER from a stall, so
            # an inference-bearing run would be false-failed at the attempts
            # cap. Deferred-vertex-aware stall detection = backlog JOS-05.
            blockers.append(
                f"step {step.number}: RESULT_PROCESSOR_KIND "
                f"{step.result_processor_kind.value!r} — v1 runs are "
                f"deterministic-only (inference steps rejected until JOS-05)"
            )
        if not step.bound_sub_steps:
            blockers.append(
                f"step {step.number}: no bound sub-steps — every executable "
                f"step needs its process key(s) with Arguments"
            )
    return blockers


# -- emission -------------------------------------------------------------------


def _emit_document(
    *,
    joseki_key: str,
    wbs_id: str,
    manifest_id: str,
    sequence: str,
    terminal_number: int,
) -> str:
    """The full joseki-scoped WBS document, terminal step appended."""
    terminal = (
        f"[ ] {terminal_number}. Record joseki run completion\n"
        f"    RESULT_PROCESSOR_KIND: deterministic_continuation\n"
        f"    a) Record the run terminal state and run evidence "
        f"({_COMPLETE_RUN_PROCESS_KEY})\n"
        f"        Arguments:\n"
        f'        {{"wbs_id": "{wbs_id}"}}'
    )
    # Joseki-scoped FRAGMENT shape — deliberately NO "### Work Item" header
    # and NO "## Phase" header. Both registrar validators bless exactly this
    # shape ("joseki-scoped fragments" in validate_work_item_terminal_steps /
    # validate_wbs_phase_containment); a Work Item header would drag the run
    # WBS into per-work-item graft accounting, whose terminal
    # record_work_breakdown_structure_step_state contract cannot follow an
    # EDGE_SINK completion step (the chain ends at complete_joseki_run).
    return (
        f"# Work Breakdown Structure\n\n"
        f"WBS ID: {wbs_id}\n"
        f"WORK_MANIFEST: {manifest_id}\n"
        f"JOSEKI_KEY: {joseki_key}\n"
        f"WORK_ITEM: joseki-run\n"
        f"Status: ready\n\n"
        f"{sequence}\n\n"
        f"{terminal}\n"
    )


def _self_check(document: str, joseki_key: str, terminal_number: int) -> None:
    """Re-parse the emitted document with the core parser (fail loud)."""
    reparsed = parse(document)
    terminal = next(
        (s for s in reparsed.steps if s.number == terminal_number), None,
    )
    if terminal is None or _COMPLETE_RUN_PROCESS_KEY not in terminal.process_keys:
        raise FrameworkError(
            message=(
                f"instantiated WBS for {joseki_key!r} failed its structural "
                f"self-check — the terminal completion step did not survive "
                f"the core parser round-trip"
            ),
            error_code=ErrorCode.JOSEKI_CARD_DEFECT,
        )
