"""The three perspective-diverse refute lenses (design brief §3.3).

Each L3 skeptic reviews a candidate finding through exactly one lens and is
prompted to *refute* it. Diversity is the point: N identical skeptics catch
one failure mode N times; three distinct lenses catch three different ways a
finding can be a false positive.

  - ``CORRECTNESS`` — is the described defect a real defect, or a misread of
    correct code / a style opinion dressed up as a bug?
  - ``POLICY``      — does the finding trip the DO-NOT-FLAG list (F2 §4)? the platform's
    deliberate policy (fast-fail, state-interface-only, single-user) inverts
    the usual reviewer instinct; a "bug" that is actually sanctioned policy is
    a false positive by construction.
  - ``REPRODUCE``   — does it actually reproduce? Is there concrete evidence
    (a real ``file:line`` and a quoted artifact), or is it speculative?
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .tiers import ALL_TIERS, PolicyTier


class SkepticLens(StrEnum):
    """One refute perspective. The ``critic_lens`` / provenance label of a verdict."""

    CORRECTNESS = "correctness"
    POLICY = "policy"
    REPRODUCE = "reproduce"


@dataclass(frozen=True, slots=True)
class _DirectiveClause:
    """One tier-tagged sentence of a refute directive (FT-2).

    Clauses render in order, filtered by the active policy-tier stack. An
    ``PROJECT_LOCAL`` clause is DROPPED on a foreign target so the project-local refute
    grounds (single-user category errors, fast-fail-is-correct, RB-SCOPE) never refute
    a real foreign finding; ``UNIVERSAL`` clauses apply everywhere.
    """

    tier: PolicyTier
    text: str


# Each directive is an ORDERED clause list; the full stack (self-vet) rejoins to the exact
# pre-FT-2 directive text (byte-compatible), and a foreign stack drops the project_local clauses.
# The single load-bearing tier assignment (Architect ruling §38): the POLICY lens's
# fast-fail / absence-of-raw-SQL / single-user-category-error / RB-SCOPE clauses are
# ``project_local``; everything else is ``universal``.
REFUTE_CLAUSES: dict[SkepticLens, tuple[_DirectiveClause, ...]] = {
    SkepticLens.CORRECTNESS: (
        _DirectiveClause(
            PolicyTier.UNIVERSAL,
            "Refute on CORRECTNESS grounds. Assume the code is correct until the finding "
            "proves otherwise. Ask: is the described defect a genuine correctness or "
            "security fault (off-by-one, wrong error handling, race, resource leak, "
            "contract/nullability violation, injection, unsafe deserialization), or is it "
            "a taste/style opinion, a misreading of correct code, or a finding that names "
            "no real rule it breaks? If it does not pin to a concrete universal-tier or "
            "project-tier rule, it is refuted.",
        ),
    ),
    SkepticLens.POLICY: (
        _DirectiveClause(
            PolicyTier.UNIVERSAL,
            "Refute on POLICY grounds. Load the whole DO-NOT-FLAG list (F2 §4) and the "
            "project rulebook (F2 §2) under the active context profile (F2 §3).",
        ),
        _DirectiveClause(
            PolicyTier.PROJECT_LOCAL,
            "the platform's policy INVERTS the usual reviewer instinct: a missing try/except, an "
            "absent fallback, no defensive input-validation, no backwards-compat shim, and "
            "the *absence* of raw SQL are all CORRECT here, not bugs.",
        ),
        _DirectiveClause(
            PolicyTier.PROJECT_LOCAL,
            "Multi-tenant auth / session-isolation / rate-limiting / CSRF findings are "
            "category errors (single-user localhost).",
        ),
        _DirectiveClause(
            PolicyTier.PROJECT_LOCAL,
            "Gate-style nits outside the quality surface (RB-SCOPE) do not apply.",
        ),
        _DirectiveClause(
            PolicyTier.UNIVERSAL,
            "If the finding is any DO-NOT-FLAG class, it is refuted by construction — say "
            "so and mark the refutation dispositive.",
        ),
    ),
    SkepticLens.REPRODUCE: (
        _DirectiveClause(
            PolicyTier.UNIVERSAL,
            "Refute on REPRODUCIBILITY grounds. Ask: does this finding actually "
            "reproduce from the evidence given? Is there a concrete, verifiable artifact "
            "— a real repo-relative ``file:line`` and a quoted snippet or tool output — "
            "or is the claim speculative, vague, or hallucinated? A finding whose "
            "evidence does not substantiate the claim, or that points at no locatable "
            "site when its dimension needs one, does not reproduce and is refuted.",
        ),
    ),
}


def refute_directive(lens: SkepticLens, tiers: frozenset[PolicyTier] = ALL_TIERS) -> str:
    """The lens-specific adversarial instruction, rendered from the active policy-tier stack (FT-2).

    W3-C: the clauses now render FROM the assembled, hash-verified rulebook artifact (``REFUTE_CLAUSES``
    below is the ASSEMBLER's SOURCE; the runtime reads the artifact). ``project_local`` clauses drop on a
    foreign target so the project-local refute grounds never refute a real foreign finding. Byte-identical to
    the pre-assembly directive (clauses stored verbatim) — the self-vet render is the W3C-1 regression bar.
    """
    from .rulebook import load_rulebook  # local import — the artifact is the runtime source of truth (no cycle)

    return load_rulebook().refute_directive(lens.value, tiers)


DEFAULT_LENSES: tuple[SkepticLens, ...] = (
    SkepticLens.CORRECTNESS,
    SkepticLens.POLICY,
    SkepticLens.REPRODUCE,
)
"""The v1 roster: three lenses, so majority-refute is 2-of-3 (design brief §3.3)."""
