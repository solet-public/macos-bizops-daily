"""policy_tier_smoke.py — FT-2 (directive half): the POLICY refute directive is tier-rendered.

The load-bearing catch (Architect ruling §36–39): the L3 POLICY refute directive hardcodes
project-local-tier grounds — "a missing try/except / absent fallback / *absence* of raw SQL are CORRECT",
"Multi-tenant auth / session-isolation / rate-limiting / CSRF are category errors (single-user
localhost)", "RB-SCOPE gate nits do not apply". On a FOREIGN multi-tenant target those refute
REAL findings. FT-2 tags each clause (universal vs project_local) and renders from the active
tier stack. This smoke pins the DIRECTIVE half (lenses/prompts/verifier — landing now); the DNF
DO-NOT-FLAG rule tags (rulebook) + the active_tiers wiring from target_class land WITH it after
the B3 freeze lifts, as one atomic FT-2 slice (they must land together or foreign L3 is wrong).

Pins: the self-vet render (ALL_TIERS) is BYTE-IDENTICAL to the pre-FT-2 directive (self L3
unchanged); the foreign render (universal only) DROPS the project_local clauses; the assembled
skeptic prompt omits them on a foreign stack; active_tiers derives the stack from the class;
the verifier carries the field. Hermetic. Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from code_vetting_plugin.verify.dispatch import (
    HeuristicSkepticDispatcher,
    SkepticRequest,
    SkepticVote,
)
from code_vetting_plugin.verify.lenses import SkepticLens, refute_directive
from code_vetting_plugin.verify.prompts import build_skeptic_prompt
from code_vetting_plugin.verify.rulebook import (
    KEYWORD_RULES,
    load_rulebook,
)
from code_vetting_plugin.verify.tiers import ALL_TIERS, UNIVERSAL_ONLY, PolicyTier, active_tiers
from code_vetting_plugin.verify.verifier import AdversarialVerifier

_CHECKS_RUN: list[str] = []

# The exact pre-FT-2 POLICY directive — the self-vet render MUST reproduce it byte-for-byte.
_PRE_FT2_POLICY_DIRECTIVE = (
    "Refute on POLICY grounds. Load the whole DO-NOT-FLAG list (F2 §4) and the "
    "project rulebook (F2 §2) under the active context profile (F2 §3). the platform's "
    "policy INVERTS the usual reviewer instinct: a missing try/except, an absent "
    "fallback, no defensive input-validation, no backwards-compat shim, and the "
    "*absence* of raw SQL are all CORRECT here, not bugs. Multi-tenant auth / "
    "session-isolation / rate-limiting / CSRF findings are category errors "
    "(single-user localhost). Gate-style nits outside the quality surface "
    "(RB-SCOPE) do not apply. If the finding is any DO-NOT-FLAG class, it is "
    "refuted by construction — say so and mark the refutation dispositive."
)

# The project_local fragments that MUST vanish on a foreign target.
_PROJECT_LOCAL_ONLY_FRAGMENTS = (
    "Multi-tenant auth",
    "category errors (single-user localhost)",
    "*absence* of raw SQL",
    "RB-SCOPE",
)


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _check_active_tiers() -> None:
    _check("active_tiers(self) == both tiers", active_tiers(foreign=False) == ALL_TIERS, str(active_tiers(foreign=False)))
    _check("active_tiers(foreign) == universal only", active_tiers(foreign=True) == UNIVERSAL_ONLY, str(active_tiers(foreign=True)))
    _check("PROJECT_LOCAL is in the self stack, absent from the foreign stack", PolicyTier.PROJECT_LOCAL in ALL_TIERS and PolicyTier.PROJECT_LOCAL not in UNIVERSAL_ONLY, "")


def _check_directive_render() -> None:
    self_render = refute_directive(SkepticLens.POLICY, ALL_TIERS)
    _check("self-vet POLICY directive is BYTE-IDENTICAL to the pre-FT-2 text", self_render == _PRE_FT2_POLICY_DIRECTIVE, self_render)
    _check("refute_directive default == the full self stack (byte-compat)", refute_directive(SkepticLens.POLICY) == _PRE_FT2_POLICY_DIRECTIVE, "default")

    foreign_render = refute_directive(SkepticLens.POLICY, UNIVERSAL_ONLY)
    for fragment in _PROJECT_LOCAL_ONLY_FRAGMENTS:
        _check(f"foreign POLICY directive DROPS project_local fragment {fragment!r}", fragment not in foreign_render, foreign_render)
    _check("foreign POLICY directive KEEPS the universal base", "Refute on POLICY grounds." in foreign_render, foreign_render)
    _check("foreign POLICY directive KEEPS the universal DO-NOT-FLAG dispositive clause", "refuted by construction" in foreign_render, foreign_render)
    # The universal lenses are unaffected by the tier stack (no project_local clauses).
    _check("CORRECTNESS directive is tier-invariant (all-universal)", refute_directive(SkepticLens.CORRECTNESS, ALL_TIERS) == refute_directive(SkepticLens.CORRECTNESS, UNIVERSAL_ONLY), "correctness")
    _check("REPRODUCE directive is tier-invariant (all-universal)", refute_directive(SkepticLens.REPRODUCE, ALL_TIERS) == refute_directive(SkepticLens.REPRODUCE, UNIVERSAL_ONLY), "reproduce")


def _finding() -> Finding:
    return Finding.build(
        run_id="vr-ft2",
        layer=Layer.L2_CRITIC,
        dimension=Dimension.SECURITY,
        severity=Severity.HIGH,
        file="src/auth.ts",
        line=12,
        constraint_violated="critic:missing-rls",
        evidence="table exposed without a row-level-security policy",
        provenance=Provenance(source="critic:security"),
        context_profile=ContextProfile.PRODUCTION,
    )


def _check_prompt_and_verifier() -> None:
    finding = _finding()
    self_prompt = build_skeptic_prompt(finding, SkepticLens.POLICY, "RULEBOOK")
    foreign_prompt = build_skeptic_prompt(finding, SkepticLens.POLICY, "RULEBOOK", tiers=UNIVERSAL_ONLY)
    _check("self-vet prompt (default) KEEPS the single-user category-error clause", "category errors (single-user localhost)" in self_prompt, "self prompt")
    for fragment in _PROJECT_LOCAL_ONLY_FRAGMENTS:
        _check(f"foreign skeptic prompt DROPS {fragment!r} (a real RLS finding is not pre-refuted)", fragment not in foreign_prompt, "foreign prompt")

    # The verifier carries the active_tiers field and defaults to the full self stack.
    rulebook = load_rulebook()
    dispatcher = HeuristicSkepticDispatcher(rulebook)
    default_verifier = AdversarialVerifier(dispatcher, rulebook)
    _check("AdversarialVerifier.active_tiers defaults to the self stack", default_verifier.active_tiers == ALL_TIERS, str(default_verifier.active_tiers))
    foreign_verifier = AdversarialVerifier(dispatcher, rulebook, active_tiers=UNIVERSAL_ONLY)
    _check("AdversarialVerifier accepts a foreign active_tiers stack", foreign_verifier.active_tiers == UNIVERSAL_ONLY, str(foreign_verifier.active_tiers))


def _multitenant_finding() -> Finding:
    """A finding whose fix trips the F2§4.4-MULTITENANT DNF rule (project_local)."""
    return Finding.build(
        run_id="vr-ft2",
        layer=Layer.L2_CRITIC,
        dimension=Dimension.SECURITY,
        severity=Severity.HIGH,
        file="src/db.ts",
        line=3,
        constraint_violated="critic:tenant-leak",
        evidence="the query returns rows across tenants",
        fix_suggestion="add session isolation and per-tenant rate limiting",
        provenance=Provenance(source="critic:security"),
        context_profile=ContextProfile.PRODUCTION,
    )


def _check_dnf_screen() -> None:
    _check(
        "every keyword DNF rule is tagged project_local (FT-2)",
        all(rule.tier is PolicyTier.PROJECT_LOCAL for rule in KEYWORD_RULES),
        str([rule.rule_id for rule in KEYWORD_RULES if rule.tier is not PolicyTier.PROJECT_LOCAL]),
    )
    rulebook = load_rulebook()
    finding = _multitenant_finding()
    self_hit = rulebook.screen_do_not_flag(finding, ALL_TIERS)
    _check("self-vet DNF screen: the multitenant finding trips F2§4.4", self_hit is not None and self_hit.rule_id == "F2§4.4-MULTITENANT", str(self_hit))
    foreign_hit = rulebook.screen_do_not_flag(finding, UNIVERSAL_ONLY)
    _check("foreign DNF screen: the multitenant finding trips NOTHING (all-project_local screen disables)", foreign_hit is None, str(foreign_hit))

    # End-to-end through the heuristic POLICY pre-screen: a real foreign finding survives.
    request = SkepticRequest(finding, SkepticLens.POLICY, "prompt")
    self_resp = HeuristicSkepticDispatcher(rulebook, ALL_TIERS).evaluate_batch([request])[0]
    _check("self-vet heuristic POLICY: dispositive REFUTE on the multitenant finding", self_resp.vote is SkepticVote.REFUTE and self_resp.dispositive, str(self_resp))
    foreign_resp = HeuristicSkepticDispatcher(rulebook, UNIVERSAL_ONLY).evaluate_batch([request])[0]
    _check("foreign heuristic POLICY: UPHOLD, NOT dispositive — the real RLS finding survives to L3", foreign_resp.vote is SkepticVote.UPHOLD and not foreign_resp.dispositive, str(foreign_resp))


def main() -> int:
    try:
        _check_active_tiers()
        _check_directive_render()
        _check_prompt_and_verifier()
        _check_dnf_screen()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"policy_tier_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
