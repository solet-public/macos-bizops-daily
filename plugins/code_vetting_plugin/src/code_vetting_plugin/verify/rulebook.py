"""The F2 rulebook context object — the moat made concrete (design brief §3.1).

Loads the ASSEMBLED, hash-pinned rulebook (W3-C C1) and exposes: the tier-filtered reviewer
preamble every skeptic is seeded with, the tier-filtered refute directives, and the DO-NOT-FLAG
screen (F2 §4) as structured queries so the deterministic heuristic dispatcher can kill the
unambiguous false-positive classes cheaply, before spending any inference.

Source of truth at runtime: ``rulebook/assembled_rulebook.json`` — a committed, in-package build
artifact whose whole-artifact hash is verified FAIL-LOUD at every load (a corrupt/tampered moat
raises rather than silently seeding skeptics wrong). The in-code ``KEYWORD_RULES`` / directive
clauses (``verify/lenses``) are the ASSEMBLER's SOURCE (W3-C); the runtime reconstructs from the
artifact. This RETIRES two interims: B3a's ``workbench/``-anchored doc path (the artifact ships IN
the package, release-copy safe via ``importlib.resources``) and FT-2 §40's non-tier-filtered
preamble (the preamble now renders FROM the assembled stack, tier-filtered). ``scanners/rulebook_sync``
(W3C-1b) re-derives the manifest from the live sources to catch a stale/tampered artifact.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import re
from dataclasses import dataclass

from ..models import Dimension, Finding
from ..rulebook.manifest import verify_artifact
from .tiers import ALL_TIERS, PolicyTier

_ARTIFACT_PACKAGE = "code_vetting_plugin.rulebook"
_ARTIFACT_NAME = "assembled_rulebook.json"

# Dimensions that sweep the WHOLE tracked tree, not just the gate scope — leak / safety scanners
# (F2 §4.8 note; targets.py). RB-SCOPE exclusion never applies: a secret in workbench/ is still a secret.
_SAFETY_DIMENSIONS: frozenset[Dimension] = frozenset({Dimension.SECRETS, Dimension.IDENTITY_LEAK, Dimension.HIDDEN_UNICODE})

# Operator-tooling paths outside the platform quality surface (RB-SCOPE).
_SCOPE_EXCLUDED_PLUGIN_SUBDIR = re.compile(r"^plugins/[^/]+/(?:research|tools|migrations|parity_tests)/")
_SCOPE_EXCLUDED_TOPLEVEL: tuple[str, ...] = ("workbench/", "deployment/")
# A test path — the pyright gate excludes tests (F2 §4.10).
_TEST_PATH = re.compile(r"(?:^|/)tests?/|(?:^|/)test_[^/]*\.py$|_test\.py$")


@dataclass(frozen=True, slots=True)
class DoNotFlagRule:
    """One F2 §4 false-positive class (FT-2 tier-tagged). ``triggers`` are lowercase substrings that
    mark a finding as this class; a hit is a dispositive refutation.

    Every rule is ``project_local`` — each encodes an platform-specific inverted-policy stance (fast-fail /
    no-raw-SQL / single-user / RB-SCOPE) that must NOT dispositively refute a real foreign finding.
    On a foreign target the whole keyword/scope/test-any screen disables (the active-tier stack drops
    ``PROJECT_LOCAL``), so an project-local policy rule never refutes e.g. a genuine multi-tenant RLS finding.
    """

    rule_id: str
    summary: str
    tier: PolicyTier
    triggers: tuple[str, ...] = ()


# --- ASSEMBLER SOURCE (W3-C): the in-code DNF rules the assembler reads into the artifact. The
# runtime reconstructs equivalents FROM the artifact; rulebook_sync (W3C-1b) verifies they match. ---
KEYWORD_RULES: tuple[DoNotFlagRule, ...] = (
    DoNotFlagRule(
        "F2§4.1-FASTFAIL-RECOVERY",
        "Missing try/except / absent fallback / 'add error recovery' — RB-FASTFAIL policy, not a bug",
        PolicyTier.PROJECT_LOCAL,
        (
            "try/except", "try / except", "add error handling", "missing error handling",
            "no error handling", "add a fallback", "add fallback", "error recovery",
            "wrap in try", "catch the exception", "should catch", "graceful degradation",
            "degrade gracefully", "handle the failure gracefully",
        ),
    ),
    DoNotFlagRule(
        "F2§4.2-DEFENSIVE",
        "'Add defensive checks / validate inputs more' where code fails fast deliberately — policy",
        PolicyTier.PROJECT_LOCAL,
        ("add defensive", "defensive programming", "defensive check", "validate inputs more", "add more validation", "add a guard clause"),
    ),
    DoNotFlagRule(
        "F2§4.3-RAWSQL-ABSENCE",
        "'Why not just query the table' / use raw SQL — RB-STATE forbids the shortcut",
        PolicyTier.PROJECT_LOCAL,
        ("query the table directly", "why not just query", "use raw sql", "raw sql would be", "just use a join", "simpler to query directly"),
    ),
    DoNotFlagRule(
        "F2§4.4-MULTITENANT",
        "Multi-tenant auth / session isolation / per-user rate limiting / CSRF — RB-SINGLEUSER category error",
        PolicyTier.PROJECT_LOCAL,
        ("multi-tenant", "multitenant", "session isolation", "tenant isolation", "per-user rate", "rate limiting", "rate-limit", "csrf token", "add rate limiting"),
    ),
    DoNotFlagRule(
        "F2§4.5-CICD",
        "'No CI/CD / add GitHub Actions / branch protection' — git is deliberately not load-bearing",
        PolicyTier.PROJECT_LOCAL,
        ("ci/cd", "github actions", "branch protection", "no ci pipeline", "add ci ", "continuous integration pipeline", "no automated pipeline"),
    ),
    DoNotFlagRule(
        "F2§4.6-BACKCOMPAT",
        "'Add backwards-compatibility / version / deprecation shims' — RB-FASTFAIL forbids them",
        PolicyTier.PROJECT_LOCAL,
        ("backwards compat", "backward compat", "backwards-compat", "backward-compat", "version shim", "compatibility shim", "deprecation shim"),
    ),
    DoNotFlagRule(
        "F2§4.7-GODCLASS-SIZE",
        "Large plugin class / many methods — RB-COHERENCE: size alone is not a god class",
        PolicyTier.PROJECT_LOCAL,
        ("too many methods", "class is too large", "class is too big", "large class with", "split this class", "too many public methods"),
    ),
)
SCOPE_RULE = DoNotFlagRule(
    "F2§4.8-SCOPE",
    "Gate-style nit in operator-tooling (workbench/deployment/research/tools/migrations/parity_tests) — RB-SCOPE",
    PolicyTier.PROJECT_LOCAL,
)
TEST_ANY_RULE = DoNotFlagRule(
    "F2§4.10-TEST-ANY",
    "Any/broad types in a test file — the pyright gate excludes tests",
    PolicyTier.PROJECT_LOCAL,
)


def _rule_from_entry(entry: dict[str, object]) -> DoNotFlagRule:
    triggers = entry.get("triggers")
    return DoNotFlagRule(
        rule_id=str(entry["rule_id"]),
        summary=str(entry["summary"]),
        tier=PolicyTier(str(entry["tier"])),
        triggers=tuple(str(t) for t in triggers) if isinstance(triggers, list) else (),
    )


@dataclass(frozen=True, slots=True)
class Rulebook:
    """The loaded F2 context object, reconstructed from the assembled artifact: the tier-tagged
    directives + preamble sections + the DO-NOT-FLAG screen. Tier-filtered per the active stack."""

    directives: dict[str, tuple[tuple[str, str], ...]]  # lens -> ((tier, text), ...) VERBATIM
    keyword_rules: tuple[DoNotFlagRule, ...]
    scope_rule: DoNotFlagRule
    test_any_rule: DoNotFlagRule
    preamble_sections: tuple[tuple[str, str], ...]  # ordered (tier, body)
    source_path: str

    def render_preamble(self, tiers: frozenset[PolicyTier] = ALL_TIERS) -> str:
        """The reviewer preamble, tier-filtered from the assembled stack (FT-2 §40 completion): a
        foreign target drops the project_local sections (the platform rulebook + the project-local policy DNF moat)."""
        return "\n\n".join(body for tier, body in self.preamble_sections if PolicyTier(tier) in tiers)

    @property
    def preamble_text(self) -> str:
        """The full self-vet preamble (all tiers) — byte-compatible default for callers not tiering."""
        return self.render_preamble(ALL_TIERS)

    def refute_directive(self, lens: str, tiers: frozenset[PolicyTier] = ALL_TIERS) -> str:
        """The lens's refute directive, tier-filtered from the assembled clauses (byte-identical to
        the pre-assembly ``lenses.refute_directive`` — the W3C-1 regression bar)."""
        return " ".join(text for tier, text in self.directives[lens] if PolicyTier(tier) in tiers)

    def _finding_text(self, finding: Finding) -> str:
        return " ".join((finding.constraint_violated, finding.evidence, finding.fix_suggestion or "")).lower()

    def is_scope_excluded(self, path: str) -> bool:
        """True when ``path`` is operator-tooling outside the platform quality surface."""
        return path.startswith(_SCOPE_EXCLUDED_TOPLEVEL) or _SCOPE_EXCLUDED_PLUGIN_SUBDIR.search(path) is not None

    def is_test_path(self, path: str) -> bool:
        """True when ``path`` is a test file (the pyright gate excludes these)."""
        return _TEST_PATH.search(path) is not None

    def _matched_keyword_rule(self, finding: Finding, active_tiers: frozenset[PolicyTier]) -> DoNotFlagRule | None:
        text = self._finding_text(finding)
        for rule in self.keyword_rules:
            if rule.tier in active_tiers and any(trigger in text for trigger in rule.triggers):
                return rule
        return None

    def _matched_structural_rule(self, finding: Finding, active_tiers: frozenset[PolicyTier]) -> DoNotFlagRule | None:
        if self.scope_rule.tier in active_tiers and finding.dimension not in _SAFETY_DIMENSIONS and self.is_scope_excluded(finding.file):
            return self.scope_rule
        if self.test_any_rule.tier in active_tiers and finding.dimension is Dimension.TYPE_COVERAGE and self.is_test_path(finding.file):
            return self.test_any_rule
        return None

    def screen_do_not_flag(self, finding: Finding, active_tiers: frozenset[PolicyTier] = ALL_TIERS) -> DoNotFlagRule | None:
        """Return the DO-NOT-FLAG rule this finding trips (F2 §4), or ``None`` — a dispositive refutation.

        FT-2: only in-``active_tiers`` rules fire. Every rule is ``project_local``, so a foreign target
        (``UNIVERSAL_ONLY``) matches NONE — the whole screen disables and no project-local policy rule refutes a
        real foreign finding. The default (``ALL_TIERS``) is the self-vet screen.
        """
        return self._matched_keyword_rule(finding, active_tiers) or self._matched_structural_rule(finding, active_tiers)


def _read_artifact() -> dict[str, object]:
    """Read the in-package assembled rulebook (release-copy safe via importlib.resources — B3a retired)."""
    resource = importlib.resources.files(_ARTIFACT_PACKAGE).joinpath(_ARTIFACT_NAME)
    return json.loads(resource.read_text(encoding="utf-8"))


@functools.cache
def load_rulebook() -> Rulebook:
    """Load + FAIL-LOUD-verify the assembled rulebook, reconstructing the runtime context object.

    The whole-artifact hash is checked at every (cache-miss) load; a corrupt/tampered/hand-edited
    artifact raises rather than seeding skeptics against a broken moat. Cached — the artifact is
    immutable at runtime; ``load_rulebook.cache_clear()`` forces a re-read (tests only).
    """
    content = verify_artifact(_read_artifact())
    directives_raw = content["directives"]
    dnf_raw = content["dnf_rules"]
    preamble_raw = content["preamble_sections"]
    if not isinstance(directives_raw, dict) or not isinstance(dnf_raw, list) or not isinstance(preamble_raw, list):
        raise ValueError("assembled rulebook content is malformed — directives/dnf_rules/preamble_sections")
    directives = {
        str(lens): tuple((str(tier), str(text)) for tier, text in clauses)
        for lens, clauses in directives_raw.items()
    }
    by_kind: dict[str, list[DoNotFlagRule]] = {"keyword": [], "scope": [], "test_any": []}
    for entry in dnf_raw:
        by_kind[str(entry["kind"])].append(_rule_from_entry(entry))
    preamble = tuple((str(section["tier"]), str(section["body"])) for section in preamble_raw)
    return Rulebook(
        directives=directives,
        keyword_rules=tuple(by_kind["keyword"]),
        scope_rule=by_kind["scope"][0],
        test_any_rule=by_kind["test_any"][0],
        preamble_sections=preamble,
        source_path=f"{_ARTIFACT_PACKAGE}/{_ARTIFACT_NAME}",
    )
