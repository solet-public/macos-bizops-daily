"""The adversarial verifier core — vote aggregation, F1 stamping, run summary.

Given L1/L2 candidate findings and a skeptic dispatcher, run each finding
through the perspective-diverse lenses, aggregate the refutations, and re-stamp
the F1 record (design brief §3.3). The aggregation is where precision is bought:

  1. Any *dispositive* refutation (a DO-NOT-FLAG kill, F2 §4) refutes the finding
     outright, overriding the vote — "L3 kills it on sight."
  2. Otherwise a strict majority of refute votes refutes it. An ``UNCERTAIN``
     vote counts toward refutation (default to refuted when in doubt).
  3. Only findings no lens could refute, that a majority uphold, are confirmed.

L3 creates no findings — every input is stamped ``confirmed`` or ``refuted`` and
promoted to ``layer=L3_verified`` via :meth:`Finding.with_verdict`, preserving
``finding_id`` so the candidate→verdict trail holds. The report renders the
confirmed set; the metrics count all three verdicts (F1 §3).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..models import Finding, Verdict
from .dispatch import SkepticDispatcher, SkepticRequest, SkepticResponse, SkepticVote
from .lenses import DEFAULT_LENSES, SkepticLens
from .prompts import build_skeptic_prompt
from .rulebook import Rulebook
from .tiers import ALL_TIERS, PolicyTier


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """How lens votes combine. Defaults encode the design-brief §3.3 contract."""

    lenses: tuple[SkepticLens, ...] = DEFAULT_LENSES
    uncertain_counts_as_refute: bool = True


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """One verified finding: the re-stamped record plus the lens verdicts that
    produced it (the per-finding audit trail feeding the survival-rate metric)."""

    finding: Finding
    responses: tuple[SkepticResponse, ...]

    @property
    def verdict(self) -> Verdict:
        return self.finding.verdict


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    """Run-level metrics (F1 §3): the suite measuring its own precision."""

    total: int
    confirmed: int
    refuted: int
    dispositive_refutations: int
    survival_rate: float | None
    refutes_by_lens: dict[str, int] = field(default_factory=dict)
    confirmed_by_dimension: dict[str, int] = field(default_factory=dict)


def _refute_majority(lens_count: int) -> int:
    """Strict majority threshold — 2 of 3 for the default roster."""
    return lens_count // 2 + 1


def _counts_toward_refute(vote: SkepticVote, policy: VerificationPolicy) -> bool:
    if vote is SkepticVote.REFUTE:
        return True
    return vote is SkepticVote.UNCERTAIN and policy.uncertain_counts_as_refute


def aggregate_votes(responses: Sequence[SkepticResponse], policy: VerificationPolicy) -> Verdict:
    """Combine one finding's lens verdicts into a confirmed/refuted decision."""
    if not responses:
        raise ValueError("cannot aggregate an empty response set — every finding must be reviewed")
    if any(response.dispositive and response.vote is SkepticVote.REFUTE for response in responses):
        return Verdict.REFUTED
    refute_count = sum(1 for response in responses if _counts_toward_refute(response.vote, policy))
    if refute_count >= _refute_majority(len(responses)):
        return Verdict.REFUTED
    return Verdict.CONFIRMED


@dataclass(frozen=True, slots=True)
class AdversarialVerifier:
    """Runs candidate findings through the refute-harness and stamps verdicts."""

    dispatcher: SkepticDispatcher
    rulebook: Rulebook
    policy: VerificationPolicy = VerificationPolicy()
    context_provider: Callable[[Finding], str] | None = None
    # RIDER-1: set True by the substrate selector when the skeptic runs OFF the
    # operator's own session (subscription/claude -p or an advertised bridge).
    # Redacts the forwarded PROMPT (sensitive evidence + raw code pack); the local
    # heuristic pre-screen still sees the original request.finding (never forwarded).
    off_operator: bool = False
    # FT-2: the active policy-tier stack, DERIVED from the target class (universal always;
    # project_local only on a self-vet). Filters the refute-directive clauses so a foreign
    # target's POLICY prompt drops the project-local category-error grounds. Default = self-vet.
    active_tiers: frozenset[PolicyTier] = ALL_TIERS

    def verify(self, findings: Sequence[Finding]) -> list[VerificationOutcome]:
        """Verify every candidate finding; return one outcome per input, in order."""
        requests = self._build_requests(findings)
        responses = self.dispatcher.evaluate_batch(requests)
        if len(responses) != len(requests):
            raise ValueError(f"dispatcher returned {len(responses)} responses for {len(requests)} requests")
        return self._assemble(findings, responses)

    def _context_for(self, finding: Finding) -> str:
        return "" if self.context_provider is None else self.context_provider(finding)

    def _build_requests(self, findings: Sequence[Finding]) -> list[SkepticRequest]:
        # W3-C §40 completion: the preamble tier-filters from the assembled stack, matching the
        # directive — a foreign target drops the project_local sections (the platform rulebook + DNF moat).
        preamble = self.rulebook.render_preamble(self.active_tiers)
        requests: list[SkepticRequest] = []
        for finding in findings:
            extra = self._context_for(finding)
            for lens in self.policy.lenses:
                prompt = build_skeptic_prompt(
                    finding, lens, preamble, extra_context=extra, off_operator=self.off_operator, tiers=self.active_tiers
                )
                requests.append(SkepticRequest(finding, lens, prompt))
        return requests

    def _assemble(self, findings: Sequence[Finding], responses: Sequence[SkepticResponse]) -> list[VerificationOutcome]:
        stride = len(self.policy.lenses)
        outcomes: list[VerificationOutcome] = []
        for index, finding in enumerate(findings):
            group = tuple(responses[index * stride : (index + 1) * stride])
            verdict = aggregate_votes(group, self.policy)
            outcomes.append(VerificationOutcome(finding.with_verdict(verdict), group))
        return outcomes


def confirmed_findings(outcomes: Sequence[VerificationOutcome]) -> list[Finding]:
    """The survivors — what the run report renders."""
    return [outcome.finding for outcome in outcomes if outcome.verdict is Verdict.CONFIRMED]


def _has_dispositive_refute(outcome: VerificationOutcome) -> bool:
    return any(r.dispositive and r.vote is SkepticVote.REFUTE for r in outcome.responses)


def _refutes_by_lens(outcomes: Sequence[VerificationOutcome]) -> dict[str, int]:
    counts: dict[str, int] = {lens.value: 0 for lens in DEFAULT_LENSES}
    for outcome in outcomes:
        for response in outcome.responses:
            if response.vote is SkepticVote.REFUTE:
                counts[response.lens.value] = counts.get(response.lens.value, 0) + 1
    return counts


def _confirmed_by_dimension(outcomes: Sequence[VerificationOutcome]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.verdict is Verdict.CONFIRMED:
            key = outcome.finding.dimension.value
            counts[key] = counts.get(key, 0) + 1
    return counts


def summarize(outcomes: Sequence[VerificationOutcome]) -> VerificationSummary:
    """Compute the run-level precision metrics from the verified outcomes."""
    total = len(outcomes)
    confirmed = sum(1 for outcome in outcomes if outcome.verdict is Verdict.CONFIRMED)
    return VerificationSummary(
        total=total,
        confirmed=confirmed,
        refuted=total - confirmed,
        dispositive_refutations=sum(1 for outcome in outcomes if _has_dispositive_refute(outcome)),
        survival_rate=None if total == 0 else confirmed / total,
        refutes_by_lens=_refutes_by_lens(outcomes),
        confirmed_by_dimension=_confirmed_by_dimension(outcomes),
    )
