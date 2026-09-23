"""tiers.py — the F2 policy-tier axis that makes the rulebook target-class-aware (FT-2).

The DO-NOT-FLAG screen and the L3 refute directives were project-local-tier-hardcoded: they assert
that missing try/except, absent fallbacks, no defensive validation, the *absence* of raw
SQL, and multi-tenant auth / session-isolation / rate-limiting / CSRF findings are all
non-issues (correct for a single-user localhost). On a FOREIGN target — e.g. a multi-tenant
Supabase app — those are exactly the REAL findings, so an un-tiered POLICY directive would
dispositively refute a genuine RLS/session-isolation finding. FT-2 tags every DNF rule and
every refute-directive clause with a tier and renders the active set from the target class:

  * ``UNIVERSAL`` — no-repro, taste/style, evidence-free: apply EVERYWHERE.
  * ``PROJECT_LOCAL`` — RB-FASTFAIL, absence-of-raw-SQL-is-correct, single-user category
    errors, RB-SCOPE gate-scope nits: apply ONLY when the target is the platform's own worktree.

This is the interim in-code form of the R4 tier structure (universal / project / context-
stakes) A0 ruled for the assembled rulebook; W3-C's assembler picks it up. Semantic tier
assignment stays Architect-ruled (R4-c); this module only carries the axis + the active-stack
derivation. The active stack is DERIVED from the target class (not an independent axis): a
self-vet gets both tiers; a foreign target gets only ``UNIVERSAL`` — so nobody can run a
foreign target under the platform policy tier by a run-profile mix-up.
"""

from __future__ import annotations

from enum import StrEnum


class PolicyTier(StrEnum):
    """Which policy tier a DNF rule / refute-directive clause belongs to (FT-2)."""

    UNIVERSAL = "universal"
    """Applies to every target — no-repro, taste/style, evidence-free false-positive classes."""
    PROJECT_LOCAL = "project_local"
    """Applies ONLY to the platform's own worktree — fast-fail policy, single-user category errors, RB-SCOPE."""


# The full stack (self-vet) and the foreign stack, as frozensets for cheap membership tests.
ALL_TIERS: frozenset[PolicyTier] = frozenset(PolicyTier)
UNIVERSAL_ONLY: frozenset[PolicyTier] = frozenset({PolicyTier.UNIVERSAL})


def active_tiers(*, foreign: bool) -> frozenset[PolicyTier]:
    """The active policy-tier stack for a run, DERIVED from the target class (FT-2).

    A foreign target drops the ``PROJECT_LOCAL`` tier so the project-local DNF rules + refute
    clauses (which would refute real multi-tenant / defensive-code findings) do not apply;
    a self-vet keeps both. This is the single derivation — the verifier + prompt build both
    read the active stack from here, never from an independently-settable field.
    """
    return UNIVERSAL_ONLY if foreign else ALL_TIERS
