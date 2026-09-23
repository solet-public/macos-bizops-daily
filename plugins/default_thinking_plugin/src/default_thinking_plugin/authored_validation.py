"""Validation for agent-authored-by-value WBS documents (Phase 3, Seam A).

Frontier agents author Work Breakdown Structure markdown in their own
context and hand the finished document to the platform by value; the
platform validates and stores it — it does not author (origin: checkout-local
substrate architecture plan ``workbench/2026-07-01_claude_coding_agent_substrate_architecture_and_planning_v2.md``,
Phase 3). This module is the validation half of that seam: it runs the SAME
production validators the thinking-model authoring path applies before
storage (``artifact_helpers`` + the core plan parser), but in COLLECT mode —
every finding is gathered into a report instead of raising on the first
failure — so the ``validate_authored_*`` verbs can return a complete,
actionable finding list in one round trip.

Finding tiers follow the plan of record's Q4 decision:

* **errors (hard-fail, block registration)** — unknown LIVE process key,
  missing/invalid ``RESULT_PROCESSOR_KIND``, out-of-schema or
  missing-required arguments, unresolved ``Composed(...)`` references, and
  every structural check the storage path (``_store_wbs``) itself enforces
  (placeholders, phase containment, terminal steps, section sizes,
  ``Arguments:`` labels).
* **warnings (soft)** — structure-coherence findings from
  ``validate_artifact_structure``; they do not block registration.

``register_authored_*`` reuses this module (DRY) and hard-fails when the
report carries any error, BEFORE any storage side effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ananta.core.plans.contracts.action_contract import resolve_single_composed_source
from ananta.core.plans.parser import (
    assert_executable_joseki_wbs_steps_declare_kind,
    parse,
)
from ananta.error_handling import FrameworkError

from default_thinking_plugin.artifact_helpers import (
    extract_markdown_section,
    parse_wbs_process_arguments,
    validate_arguments_labels,
    validate_artifact_structure,
    validate_no_unresolved_placeholders,
    validate_section_sizes,
    validate_wbs_phase_containment,
    validate_work_item_terminal_steps,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ananta.core.plans.types import ComposedReference, ParsedPlan

# The validators raise ValueError (artifact_helpers), FrameworkError
# (helper guards), or RuntimeError (parser contract checks). Anything else
# is a genuine bug and must propagate.
_COLLECTABLE_ERRORS = (ValueError, RuntimeError, FrameworkError)

_WBS_ARTIFACT_TYPE = "work_breakdown_structure"


class AuthoredProcessLookup(Protocol):
    """Live process-registry lookup used by authored-artifact validation.

    ``get_arg_properties`` mirrors ``ProcessSchemaLookup``
    (``wbs_authoring_helpers``); ``key_exists`` is the explicit existence
    check Q4 requires — ``get_arg_properties`` returning ``{}`` is ambiguous
    between "unknown key" and "no-argument process", so unknown-key errors
    must come from an existence probe, never from an empty property dict.
    """

    def get_arg_properties(
        self, process_key: str,
    ) -> dict[str, dict[str, object]]:
        """Return per-argument property schemas, or ``{}`` when none."""
        ...

    def key_exists(self, process_key: str) -> bool:
        """Return True when the key resolves in the LIVE process registry."""
        ...


@dataclass(frozen=True)
class AuthoredValidationReport:
    """Outcome of validating an authored-by-value document."""

    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def valid(self) -> bool:
        """True when no hard-fail error was found."""
        return not self.errors


def validate_authored_wbs(
    content: str,
    wbs_id: str,
    phase_number: int,
    lookup: AuthoredProcessLookup,
) -> AuthoredValidationReport:
    """Validate an authored-by-value WBS document without storing it.

    Non-mutating dry run. Collects every finding (see the module docstring
    for the error/warning tiers) so the caller gets the complete list in
    one pass instead of fix-one-resubmit loops.
    """
    errors: list[str] = []
    _collect(errors, lambda: validate_no_unresolved_placeholders(content, wbs_id))
    _collect(
        errors,
        lambda: validate_wbs_phase_containment(content, wbs_id, phase_number),
    )
    _collect(errors, lambda: validate_work_item_terminal_steps(content, wbs_id))
    _collect(errors, lambda: validate_section_sizes(content, wbs_id))
    _collect(errors, lambda: validate_arguments_labels(content, wbs_id))

    parsed = _parse_collecting(content, errors)
    if parsed is not None:
        steps = parsed.steps
        _collect(
            errors,
            lambda: assert_executable_joseki_wbs_steps_declare_kind(steps),
        )
        errors.extend(_unknown_key_errors(parsed, lookup))
        errors.extend(_composed_reference_errors(parsed))
        # ◆B1-fix by Reviewer-B (2026-07-02): the arg-schema check now needs
        # the parsed sub-steps (to correlate Composed targets per sub-step), so
        # it runs only on a successful parse — a parse failure already yields
        # its own collected error above.
        errors.extend(_argument_schema_errors(parsed, lookup))

    warnings = validate_artifact_structure(content, _WBS_ARTIFACT_TYPE, wbs_id)
    return AuthoredValidationReport(errors=tuple(errors), warnings=tuple(warnings))


def _collect(errors: list[str], check: Callable[[], None]) -> None:
    """Run one validator, converting its raise into a collected error."""
    try:
        check()
    except _COLLECTABLE_ERRORS as exc:
        errors.append(str(exc))


def _parse_collecting(content: str, errors: list[str]) -> ParsedPlan | None:
    """Parse via the core plan parser; a parse failure is itself an error.

    ``parse`` enforces the per-step ``RESULT_PROCESSOR_KIND`` grammar
    (duplicate / invalid value / forbidden ``bridge_delivery``) and the
    structural step invariants, so its raise message is the error.
    """
    try:
        return parse(content)
    except _COLLECTABLE_ERRORS as exc:
        errors.append(f"WBS document failed to parse: {exc}")
        return None


def _unknown_key_errors(
    parsed: ParsedPlan, lookup: AuthoredProcessLookup,
) -> list[str]:
    """Q4 hard-fail: every referenced process key must exist LIVE."""
    errors: list[str] = []
    seen: set[str] = set()
    for step in parsed.steps:
        for key in step.process_keys:
            if key in seen:
                continue
            seen.add(key)
            if not lookup.key_exists(key):
                errors.append(
                    f"step {step.number}: unknown process key {key!r} — "
                    f"not in the live process registry",
                )
    return errors


def _composed_reference_errors(parsed: ParsedPlan) -> list[str]:
    """Q4 hard-fail: every ``Composed(...)`` reference must resolve."""
    errors: list[str] = []
    for step in parsed.steps:
        for bound in step.bound_sub_steps:
            for ref in bound.composed_references or ():
                errors.extend(_unresolved_source_errors(step.number, ref, parsed))
    return errors


def _unresolved_source_errors(
    step_number: int, ref: ComposedReference, parsed: ParsedPlan,
) -> list[str]:
    """Name every source step a Composed reference cannot resolve from."""
    return [
        f"step {step_number}: Composed reference for {ref.target_arg!r} "
        f"cannot resolve {ref.source_arg!r} from step {source_step}"
        for source_step in ref.source_steps
        if resolve_single_composed_source(ref, source_step, parsed) is None
    ]


def _argument_schema_errors(
    parsed: ParsedPlan, lookup: AuthoredProcessLookup,
) -> list[str]:
    """Q4 hard-fail: arguments must fit the process's invocation schema.

    Mirrors the required-argument check ``_store_wbs`` enforces via
    ``WbsAuthoringService._validate_wbs_arguments`` and adds the Q4
    out-of-schema check. Keys whose property dict is empty are skipped —
    unknown keys are already reported by the existence probe, and
    no-argument processes have nothing to check.

    ◆B1-fix by Reviewer-B (2026-07-02): iterate the parsed ``bound_sub_steps``
    so a required argument is SATISFIED when it appears in the literal
    ``Arguments:`` JSON OR is bound by a ``Composed:`` cross-step reference on
    the same sub-step (its resolution is checked separately by
    ``_composed_reference_errors``). The JSON-only view used before this fix
    falsely flagged Composed-bound required args (e.g. a render step's
    ``input_midi_file``) as missing. Only sub-steps carrying an ``Arguments:``
    block are checked — matching the prior gate; a sub-step with no Arguments
    block is a planning/document step, not an invocation.
    """
    errors: list[str] = []
    for step in parsed.steps:
        for sub in step.bound_sub_steps:
            if sub.arguments is None:
                continue
            props = lookup.get_arg_properties(sub.process_key)
            if not props:
                continue
            satisfied = set(sub.arguments) | {
                ref.target_arg for ref in sub.composed_references
            }
            errors.extend(_missing_required_errors(sub.process_key, satisfied, props))
            errors.extend(_out_of_schema_errors(sub.process_key, sub.arguments, props))
    return errors


def _missing_required_errors(
    process_key: str,
    satisfied: set[str],
    props: dict[str, dict[str, object]],
) -> list[str]:
    """Every schema-required argument must be satisfied on the sub-step.

    ◆B1-fix by Reviewer-B (2026-07-02): ``satisfied`` is the union of the
    literal ``Arguments:`` keys and the sub-step's ``Composed:`` target args,
    so a required argument bound by a cross-step reference is not falsely
    reported missing.
    """
    return [
        f"{process_key}: required argument {name!r} is missing"
        for name, prop in props.items()
        if bool(prop.get("required", False)) and name not in satisfied
    ]


def _out_of_schema_errors(
    process_key: str,
    args: dict[str, object],
    props: dict[str, dict[str, object]],
) -> list[str]:
    """Every supplied argument must exist in the process schema."""
    return [
        f"{process_key}: argument {name!r} is not in the process schema"
        for name in args
        if name not in props
    ]


# ---------------------------------------------------------------------------
# Joseki KB cards (§4.3 — ADDITIVE header parsing; step body reuses the WBS
# machinery above, never a parallel parser)
# ---------------------------------------------------------------------------

_JOSEKI_KEY_LINE_RE = re.compile(r"^JOSEKI_KEY:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
_JOSEKI_DESCRIPTION_RE = re.compile(r"^DESCRIPTION:[ \t]*(\S.*)$", re.MULTILINE)
_JOSEKI_EMBEDDING_RE = re.compile(
    r"^EMBEDDING_DESCRIPTION:[ \t]*(\S.*)$", re.MULTILINE,
)
_JOSEKI_KEY_FORMAT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_STEP_COUNT_RANGE_RE = re.compile(r"(\d+)\s*[–—-]\s*(\d+)")
_STEP_COUNT_SINGLE_RE = re.compile(r"\d+")

# Card sections that must be present and non-empty. "Valid-or-explicitly-
# absent" for the next/repair references means the SECTION must exist —
# absence is declared inside it (e.g. "None."), never by omission. Whether a
# referenced joseki key actually exists is NOT checked here: joseki live as
# KB articles (the KB is the registry per §4.3), and a deterministic verb
# has no retrieval surface to probe them — the lifecycle table only covers
# authored-by-value registrations.
_JOSEKI_REQUIRED_SECTIONS = (
    "Input Contract",
    "Output Contract",
    "Sequence",
    "Expected Step Count",
    "Next Joseki",
    "Repair Joseki",
)


def parse_joseki_key(content: str) -> str:
    """Return the card's ``JOSEKI_KEY:`` value, or ``""`` when absent."""
    match = _JOSEKI_KEY_LINE_RE.search(content)
    return match.group(1) if match else ""


def validate_authored_joseki(
    content: str,
    lookup: AuthoredProcessLookup,
    expected_joseki_key: str | None = None,
) -> AuthoredValidationReport:
    """Validate an authored-by-value joseki KB card without storing it.

    Card-header rules are additive parsing over the verified card shape
    (``build_layered_section_stem.md``): identity line, description pair,
    the six required sections, and a declared expected step count that the
    sequence actually satisfies. The step body reuses the same core-parser
    machinery as WBS validation (unknown LIVE keys, Composed resolution,
    RPK grammar, placeholders).

    Joseki-specific tier decisions (a card is a TEMPLATE, not an execution
    document): a supplied argument must fit the process schema, but
    missing required arguments are bound-slots deferred to instantiation —
    not errors; a MISSING ``RESULT_PROCESSOR_KIND`` is a warning (the
    POR-cited verified card omits them), while a declared-but-invalid one
    is still an error.
    """
    errors: list[str] = []
    warnings: list[str] = []

    joseki_key = parse_joseki_key(content)
    errors.extend(_joseki_identity_errors(joseki_key, expected_joseki_key))
    errors.extend(_joseki_description_errors(content))
    errors.extend(_joseki_section_errors(content))

    sequence = extract_markdown_section(content, "Sequence")
    if sequence:
        _collect(
            errors,
            lambda: validate_no_unresolved_placeholders(
                sequence, joseki_key or "joseki card",
            ),
        )
        parsed = _parse_collecting(sequence, errors)
        if parsed is not None:
            errors.extend(_unknown_key_errors(parsed, lookup))
            errors.extend(_composed_reference_errors(parsed))
            errors.extend(_joseki_step_count_errors(content, len(parsed.steps)))
            warnings.extend(_joseki_missing_rpk_warnings(parsed))
        errors.extend(_joseki_argument_errors(sequence, lookup))

    return AuthoredValidationReport(errors=tuple(errors), warnings=tuple(warnings))


def _joseki_identity_errors(
    joseki_key: str,
    expected_joseki_key: str | None,
) -> list[str]:
    """The ``JOSEKI_KEY:`` line is the card's stable identity."""
    if not joseki_key:
        return ["joseki card is missing its JOSEKI_KEY: line"]
    errors: list[str] = []
    if not _JOSEKI_KEY_FORMAT_RE.match(joseki_key):
        errors.append(
            f"JOSEKI_KEY {joseki_key!r} is not a stable key "
            f"(expected lowercase snake_case)",
        )
    if expected_joseki_key is not None and joseki_key != expected_joseki_key:
        errors.append(
            f"JOSEKI_KEY {joseki_key!r} does not match the requested key "
            f"{expected_joseki_key!r}",
        )
    return errors


def _joseki_description_errors(content: str) -> list[str]:
    """Human description + retrieval description, present and distinct."""
    description_match = _JOSEKI_DESCRIPTION_RE.search(content)
    embedding_match = _JOSEKI_EMBEDDING_RE.search(content)
    errors: list[str] = []
    if description_match is None:
        errors.append("joseki card is missing its DESCRIPTION: line")
    if embedding_match is None:
        errors.append("joseki card is missing its EMBEDDING_DESCRIPTION: line")
    if (
        description_match is not None
        and embedding_match is not None
        and description_match.group(1).strip().lower()
        == embedding_match.group(1).strip().lower()
    ):
        errors.append(
            "DESCRIPTION and EMBEDDING_DESCRIPTION must not duplicate each "
            "other — the first is the human description, the second is "
            "retrieval text",
        )
    return errors


def _joseki_section_errors(content: str) -> list[str]:
    """Every required card section must be present with a non-empty body."""
    errors: list[str] = []
    for section_name in _JOSEKI_REQUIRED_SECTIONS:
        section = extract_markdown_section(content, section_name)
        body = "\n".join(section.splitlines()[1:]).strip()
        if not body:
            errors.append(
                f"joseki card is missing a non-empty '{section_name}' section",
            )
    return errors


def _joseki_step_count_errors(content: str, actual_steps: int) -> list[str]:
    """The declared expected step count must cover the actual sequence."""
    section = extract_markdown_section(content, "Expected Step Count")
    body = "\n".join(section.splitlines()[1:]).strip()
    if not body:
        return []  # absence is already an error from the section check
    range_match = _STEP_COUNT_RANGE_RE.search(body)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
    else:
        single_match = _STEP_COUNT_SINGLE_RE.search(body)
        if single_match is None:
            return [
                "'Expected Step Count' section does not declare a numeric "
                "step count",
            ]
        low = high = int(single_match.group(0))
    if not low <= actual_steps <= high:
        return [
            f"sequence has {actual_steps} step(s) but the card declares an "
            f"expected step count of {low}–{high}",
        ]
    return []


def _joseki_missing_rpk_warnings(parsed: ParsedPlan) -> list[str]:
    """Missing RPK on a card step is soft — cards are templates.

    The instantiated WBS fragment MUST declare it (the WBS validator and
    the core parser enforce that); on the card itself the verified
    exemplar omits it, so absence warns instead of blocking. A declared
    but INVALID value still fails in ``_parse_collecting``.
    """
    missing = [
        step.number
        for step in parsed.steps
        if step.process_keys and step.result_processor_kind is None
    ]
    if not missing:
        return []
    return [
        f"step(s) {missing} do not declare RESULT_PROCESSOR_KIND — the "
        f"instantiated WBS fragment must add it",
    ]


def _joseki_argument_errors(
    sequence: str, lookup: AuthoredProcessLookup,
) -> list[str]:
    """Supplied arguments must fit the schema; unbound ones are slots."""
    errors: list[str] = []
    for process_key, args in parse_wbs_process_arguments(sequence):
        props = lookup.get_arg_properties(process_key)
        if not props:
            continue
        errors.extend(_out_of_schema_errors(process_key, args, props))
    return errors
