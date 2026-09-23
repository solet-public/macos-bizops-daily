"""Assembles the skeptic prompt the inference substrate sends (design brief §3.3).

The heuristic dispatcher decides structurally and ignores this text, but the
prompt is a first-class Wave-1 deliverable: it is exactly what a live skeptic
(dispatched over the platform's peer-messaging bridge) receives at Wave-2
integration. Each prompt
seeds the reviewer with the full F2 rulebook, the finding under review, the
lens-specific refute directive, and the precision contract (default to refuted
when uncertain) — that seeding is why our verifier beats a context-blind one.
"""

from __future__ import annotations

from ..models import Finding
from .lenses import SkepticLens, refute_directive
from .redaction import redact_for_off_operator
from .tiers import ALL_TIERS, PolicyTier

_ROLE_HEADER = (
    "You are an ADVERSARIAL VERIFIER on an AI code-vetting suite. A candidate "
    "finding from an upstream critic is below. Your job is NOT to review the code "
    "afresh — it is to REFUTE this specific finding: to show it is a false "
    "positive under the platform's own rules. A finding survives only if you "
    "genuinely cannot refute it."
)

_PRECISION_CONTRACT = (
    "PRECISION RULE — high precision is the whole point of this layer. If you are "
    "uncertain whether the finding is real, answer UNCERTAIN; uncertainty is "
    "counted as refutation. When the DO-NOT-FLAG list (F2 §4) and a 'this is a "
    "real bug' reading conflict, the DO-NOT-FLAG list wins and you mark the "
    "refutation dispositive."
)

_RESPONSE_CONTRACT = (
    "Respond with exactly these fields:\n"
    "  vote: one of REFUTE | UPHOLD | UNCERTAIN "
    "(REFUTE = this finding is a false positive; UPHOLD = you could not refute it)\n"
    "  dispositive: true only if this is a DO-NOT-FLAG (F2 §4) kill that should "
    "override the other lenses, else false\n"
    "  rule_id: the F2/RB rule id your refutation rests on, or null\n"
    "  rationale: one or two sentences, citing the rule"
)


def _render_finding(finding: Finding) -> str:
    """The candidate finding, rendered for the reviewer."""
    line = "whole-file" if finding.line is None else str(finding.line)
    return (
        "CANDIDATE FINDING UNDER REVIEW\n"
        f"  dimension:            {finding.dimension.value}\n"
        f"  severity:             {finding.severity.value}\n"
        f"  file:                 {finding.file}\n"
        f"  line:                 {line}\n"
        f"  constraint_violated:  {finding.constraint_violated}\n"
        f"  evidence:             {finding.evidence}\n"
        f"  fix_suggestion:       {finding.fix_suggestion or '(none)'}\n"
        f"  provenance.source:    {finding.provenance.source}\n"
        f"  context_profile:      {finding.context_profile.value}"
    )


_EVIDENCE_WITHHELD = (
    "EVIDENCE PACK — withheld from an off-operator reviewer (RIDER-1). The raw source "
    "under review stays on the operator's machine; adjudicate from the finding record "
    "and the rulebook above."
)


def build_skeptic_prompt(
    finding: Finding,
    lens: SkepticLens,
    rulebook_preamble: str,
    *,
    extra_context: str = "",
    off_operator: bool = False,
    tiers: frozenset[PolicyTier] = ALL_TIERS,
) -> str:
    """Assemble the full refute prompt for one (finding, lens) pair.

    ``extra_context`` is an optional evidence pack — the actual source under
    review, cited precedents, and any both-sides framing from the upstream
    critic. A real (non-synthetic) adjudication seeds it so the skeptic reasons
    over the code, not just the finding record.

    ``off_operator`` (RIDER-1) is set by the substrate selector when the skeptic
    runs OFF the operator's own session (a ``claude -p`` subprocess or an advertised
    bridge backend). It withholds both leak surfaces from the forwarded prompt: the
    sensitive finding's raw evidence (via :func:`redact_for_off_operator`) and the
    raw ``extra_context`` code pack. The LOCAL inference substrate leaves it ``False``
    — the privacy profile keeps code on the machine, so it needs full evidence.

    ``tiers`` (FT-2) is the active policy-tier stack, DERIVED from the target class: a
    foreign target drops ``PROJECT_LOCAL`` so the refute directive omits the project-local
    clauses (single-user category errors etc.) that would refute a real foreign finding.
    Default = the full self-vet stack (byte-compatible with the pre-FT-2 prompt).
    """
    rendered = redact_for_off_operator(finding) if off_operator else finding
    sections = [
        _ROLE_HEADER,
        f"YOUR LENS: {lens.value}\n{refute_directive(lens, tiers)}",
        "THE RULEBOOK (F2) — review against THESE rules, not your general instincts:\n" + rulebook_preamble,
        _render_finding(rendered),
    ]
    if extra_context and not off_operator:
        sections.append("EVIDENCE PACK — the actual code under review, precedent, and both-sides framing:\n" + extra_context)
    elif extra_context:
        sections.append(_EVIDENCE_WITHHELD)
    sections.append(_PRECISION_CONTRACT)
    sections.append(_RESPONSE_CONTRACT)
    return "\n\n".join(sections)
