"""The skeptic-dispatch seam + a deterministic heuristic dispatcher.

A *dispatcher* takes skeptic requests (one per finding × lens) and returns a
verdict per request. The seam is pluggable so the substrate can change without
touching the verifier:

  - :class:`HeuristicSkepticDispatcher` (Wave 1, here) decides each lens with
    deterministic rulebook logic. It is both the cheap DO-NOT-FLAG / RB-SCOPE
    *pre-screen* that kills the unambiguous false positives without inference,
    AND the testable driver that lets the whole harness run against synthetic
    findings today.
  - the inference dispatcher (Wave 2) implements the same
    :class:`SkepticDispatcher` protocol by sending ``request.prompt`` to N
    independent reviewer sessions over the platform's peer-messaging bridge
    (``peer_send`` / ``peer_inbox``) and parsing their structured verdicts.
    It slots in with no change to :mod:`.verifier`.

Precision bias (design brief §3.3): a skeptic that cannot decide returns
``UNCERTAIN``, which the aggregator counts toward refutation — when in doubt,
the finding is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..models import Dimension, Finding
from .lenses import SkepticLens
from .rulebook import Rulebook
from .tiers import ALL_TIERS, PolicyTier

# Dimensions whose findings point at a specific line of code — a claim in one of
# these with no line cannot be located, so the heuristic reproduce lens cannot
# confirm it reproduces. Leak/inventory dimensions (secrets, license, dup, …) are
# legitimately file-level and are not held to this.
_CODE_SITE_DIMENSIONS: frozenset[Dimension] = frozenset(
    {Dimension.CORRECTNESS, Dimension.SECURITY, Dimension.COMPLEXITY, Dimension.ARCHITECTURE}
)

_RULE_ID = re.compile(r"\bRB-[A-Z0-9]+\b|F2§|\bCWE-\d+\b|\bOWASP\b", re.IGNORECASE)
_KNOWN_GATES_TOOLS: frozenset[str] = frozenset(
    {
        "radon_cc", "radon_mi", "god_class", "god-class", "sql_access", "ruff", "pyright",
        "mypy", "vulture", "whole_tree_integration", "service_interface_ast", "gitleaks",
        "trufflehog", "semgrep", "bandit", "pip-audit", "osv", "secrets", "deps", "license",
        "identity_leak", "hidden_unicode", "network_bind", "dup", "orphan", "complexity",
        "dead_code", "type_coverage",
    }
)
_CORRECTNESS_KEYWORDS: tuple[str, ...] = (
    "off-by-one", "race condition", "resource leak", "null", "injection", "deserialization",
    "path traversal", "ssrf", "use-after", "double free", "deadlock", "unbounded",
    "integer overflow", "toctou", "contract violation", "leak",
)


class SkepticVote(StrEnum):
    """A single skeptic's verdict on one finding through one lens."""

    REFUTE = "refute"
    UPHOLD = "uphold"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class SkepticRequest:
    """One (finding, lens) refutation task. ``prompt`` is what the inference
    substrate sends; the heuristic dispatcher decides from ``finding``/``lens``."""

    finding: Finding
    lens: SkepticLens
    prompt: str


@dataclass(frozen=True, slots=True)
class SkepticResponse:
    """One skeptic's refute attempt. ``dispositive`` marks a by-construction kill
    (a DO-NOT-FLAG hit) that overrides the majority vote."""

    lens: SkepticLens
    vote: SkepticVote
    dispositive: bool
    rationale: str
    rule_id: str | None = None


class SkepticDispatcher(Protocol):
    """The pluggable substrate seam. One response per request, order-aligned."""

    def evaluate_batch(self, requests: Sequence[SkepticRequest]) -> list[SkepticResponse]: ...


def _looks_rule_pinned(constraint: str) -> bool:
    """True when ``constraint`` names a real rule/gate/defect, not vague prose."""
    text = constraint.strip().lower()
    if not text:
        return False
    if _RULE_ID.search(constraint):
        return True
    if any(token in text for token in _KNOWN_GATES_TOOLS):
        return True
    return any(keyword in text for keyword in _CORRECTNESS_KEYWORDS)


@dataclass(frozen=True, slots=True)
class HeuristicSkepticDispatcher:
    """Deterministic rulebook-driven dispatcher — the Wave-1 pre-screen + test driver."""

    rulebook: Rulebook
    # FT-2: the active policy-tier stack (DERIVED from target class). Only DNF rules in
    # these tiers can dispositively refute; a foreign stack (UNIVERSAL_ONLY) disables the
    # all-project_local keyword screen. Default = the full self-vet stack.
    active_tiers: frozenset[PolicyTier] = ALL_TIERS

    def evaluate_batch(self, requests: Sequence[SkepticRequest]) -> list[SkepticResponse]:
        return [self._evaluate(request) for request in requests]

    def _evaluate(self, request: SkepticRequest) -> SkepticResponse:
        if request.lens is SkepticLens.POLICY:
            return self._policy(request.finding)
        if request.lens is SkepticLens.CORRECTNESS:
            return self._correctness(request.finding)
        return self._reproduce(request.finding)

    def _policy(self, finding: Finding) -> SkepticResponse:
        rule = self.rulebook.screen_do_not_flag(finding, self.active_tiers)
        if rule is not None:
            return SkepticResponse(
                lens=SkepticLens.POLICY,
                vote=SkepticVote.REFUTE,
                dispositive=True,
                rationale=f"DO-NOT-FLAG {rule.rule_id}: {rule.summary}",
                rule_id=rule.rule_id,
            )
        return SkepticResponse(
            lens=SkepticLens.POLICY,
            vote=SkepticVote.UPHOLD,
            dispositive=False,
            rationale="No DO-NOT-FLAG class matched; the finding is not policy-sanctioned noise.",
        )

    def _correctness(self, finding: Finding) -> SkepticResponse:
        if _looks_rule_pinned(finding.constraint_violated):
            return SkepticResponse(
                lens=SkepticLens.CORRECTNESS,
                vote=SkepticVote.UPHOLD,
                dispositive=False,
                rationale=f"Pins to a concrete rule/defect: '{finding.constraint_violated}'.",
            )
        return SkepticResponse(
            lens=SkepticLens.CORRECTNESS,
            vote=SkepticVote.REFUTE,
            dispositive=False,
            rationale=f"Names no rule it breaks ('{finding.constraint_violated}') — vague/style, not a defect.",
        )

    def _reproduce(self, finding: Finding) -> SkepticResponse:
        # F1 now guarantees non-empty evidence, so the deterministic reproduce
        # signal is locate-ability: a code-site claim with no line cannot be
        # gone-to-and-reproduced. The inference reproduce lens does the deeper
        # "does the evidence actually substantiate the claim" judgment.
        if not finding.evidence.strip():
            return SkepticResponse(
                lens=SkepticLens.REPRODUCE,
                vote=SkepticVote.REFUTE,
                dispositive=False,
                rationale="No evidence artifact — the claim does not reproduce from what was given.",
            )
        if finding.line is None and finding.dimension in _CODE_SITE_DIMENSIONS:
            return SkepticResponse(
                lens=SkepticLens.REPRODUCE,
                vote=SkepticVote.REFUTE,
                dispositive=False,
                rationale="Code-site finding with no line — cannot be located to reproduce.",
            )
        return SkepticResponse(
            lens=SkepticLens.REPRODUCE,
            vote=SkepticVote.UPHOLD,
            dispositive=False,
            rationale="Evidence is present and the finding is locatable.",
        )
