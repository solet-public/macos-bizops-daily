"""Unit + end-to-end tests for the L3 adversarial verifier.

Runnable under pytest or directly (``python -m
code_vetting_plugin.verify.tests.test_verifier``). Uses the real F2 rulebook
and the synthetic candidate set — no mocks; the heuristic dispatcher is a real,
deterministic substrate.
"""

from __future__ import annotations

from ...models import Layer, Verdict
from ..dispatch import (
    HeuristicSkepticDispatcher,
    SkepticRequest,
    SkepticResponse,
    SkepticVote,
)
from ..lenses import DEFAULT_LENSES, SkepticLens, refute_directive
from ..prompts import build_skeptic_prompt
from ..rulebook import load_rulebook
from ..verifier import (
    AdversarialVerifier,
    VerificationPolicy,
    aggregate_votes,
    summarize,
)
from .sample_findings import sample_candidates, sample_cases

_POLICY = VerificationPolicy()


def _response(lens: SkepticLens, vote: SkepticVote, *, dispositive: bool = False) -> SkepticResponse:
    return SkepticResponse(lens=lens, vote=vote, dispositive=dispositive, rationale="test")


def test_dispositive_refute_overrides_majority() -> None:
    votes = [
        _response(SkepticLens.POLICY, SkepticVote.REFUTE, dispositive=True),
        _response(SkepticLens.CORRECTNESS, SkepticVote.UPHOLD),
        _response(SkepticLens.REPRODUCE, SkepticVote.UPHOLD),
    ]
    assert aggregate_votes(votes, _POLICY) is Verdict.REFUTED


def test_majority_refute() -> None:
    votes = [
        _response(SkepticLens.POLICY, SkepticVote.UPHOLD),
        _response(SkepticLens.CORRECTNESS, SkepticVote.REFUTE),
        _response(SkepticLens.REPRODUCE, SkepticVote.REFUTE),
    ]
    assert aggregate_votes(votes, _POLICY) is Verdict.REFUTED


def test_single_dissent_confirms() -> None:
    votes = [
        _response(SkepticLens.POLICY, SkepticVote.UPHOLD),
        _response(SkepticLens.CORRECTNESS, SkepticVote.UPHOLD),
        _response(SkepticLens.REPRODUCE, SkepticVote.REFUTE),
    ]
    assert aggregate_votes(votes, _POLICY) is Verdict.CONFIRMED


def test_uncertain_counts_as_refute() -> None:
    votes = [
        _response(SkepticLens.POLICY, SkepticVote.UPHOLD),
        _response(SkepticLens.CORRECTNESS, SkepticVote.UNCERTAIN),
        _response(SkepticLens.REPRODUCE, SkepticVote.UNCERTAIN),
    ]
    assert aggregate_votes(votes, _POLICY) is Verdict.REFUTED


def test_empty_response_set_raises() -> None:
    raised = False
    try:
        aggregate_votes([], _POLICY)
    except ValueError:
        raised = True
    assert raised, "aggregating an empty response set must fail loud"


def test_heuristic_policy_lens_kills_do_not_flag() -> None:
    rulebook = load_rulebook()
    dispatcher = HeuristicSkepticDispatcher(rulebook)
    # The try/except trap (case index 3).
    trap = sample_cases()[3].finding
    request = SkepticRequest(trap, SkepticLens.POLICY, "")
    (response,) = dispatcher.evaluate_batch([request])
    assert response.vote is SkepticVote.REFUTE
    assert response.dispositive is True
    assert response.rule_id is not None


def test_heuristic_correctness_lens_refutes_unpinned() -> None:
    rulebook = load_rulebook()
    dispatcher = HeuristicSkepticDispatcher(rulebook)
    vague = sample_cases()[8].finding  # "this code could be cleaner"
    request = SkepticRequest(vague, SkepticLens.CORRECTNESS, "")
    (response,) = dispatcher.evaluate_batch([request])
    assert response.vote is SkepticVote.REFUTE
    assert response.dispositive is False


def test_heuristic_reproduce_lens_refutes_unlocatable() -> None:
    rulebook = load_rulebook()
    dispatcher = HeuristicSkepticDispatcher(rulebook)
    unlocatable = sample_cases()[9].finding  # code-site (security) with no line
    request = SkepticRequest(unlocatable, SkepticLens.REPRODUCE, "")
    (response,) = dispatcher.evaluate_batch([request])
    assert response.vote is SkepticVote.REFUTE


def test_end_to_end_matches_ground_truth() -> None:
    rulebook = load_rulebook()
    verifier = AdversarialVerifier(HeuristicSkepticDispatcher(rulebook), rulebook)
    cases = sample_cases()
    outcomes = verifier.verify([case.finding for case in cases])
    assert len(outcomes) == len(cases)
    for case, outcome in zip(cases, outcomes, strict=True):
        expected = Verdict.CONFIRMED if case.expect_confirmed else Verdict.REFUTED
        assert outcome.verdict is expected, f"{case.finding.file}: {case.note}"


def test_stamping_preserves_id_and_promotes_layer() -> None:
    rulebook = load_rulebook()
    verifier = AdversarialVerifier(HeuristicSkepticDispatcher(rulebook), rulebook)
    candidates = sample_candidates()
    outcomes = verifier.verify(candidates)
    for original, outcome in zip(candidates, outcomes, strict=True):
        assert outcome.finding.finding_id == original.finding_id, "finding_id must survive re-stamp"
        assert outcome.finding.layer is Layer.L3_VERIFIED, "L3 must promote the layer"
        assert outcome.finding.verdict in (Verdict.CONFIRMED, Verdict.REFUTED)


def test_summary_survival_rate() -> None:
    rulebook = load_rulebook()
    verifier = AdversarialVerifier(HeuristicSkepticDispatcher(rulebook), rulebook)
    cases = sample_cases()
    outcomes = verifier.verify([case.finding for case in cases])
    summary = summarize(outcomes)
    expected_confirmed = sum(1 for case in cases if case.expect_confirmed)
    assert summary.total == len(cases)
    assert summary.confirmed == expected_confirmed
    assert summary.refuted == len(cases) - expected_confirmed
    assert summary.survival_rate is not None
    assert abs(summary.survival_rate - expected_confirmed / len(cases)) < 1e-9


def test_prompt_carries_rulebook_finding_and_lens() -> None:
    finding = sample_candidates()[0]
    preamble = "RULEBOOK-SENTINEL-TEXT"
    prompt = build_skeptic_prompt(finding, SkepticLens.POLICY, preamble)
    assert preamble in prompt
    assert finding.constraint_violated in prompt
    assert refute_directive(SkepticLens.POLICY) in prompt
    assert "REFUTE" in prompt


_TESTS = (
    test_dispositive_refute_overrides_majority,
    test_majority_refute,
    test_single_dissent_confirms,
    test_uncertain_counts_as_refute,
    test_empty_response_set_raises,
    test_heuristic_policy_lens_kills_do_not_flag,
    test_heuristic_correctness_lens_refutes_unpinned,
    test_heuristic_reproduce_lens_refutes_unlocatable,
    test_end_to_end_matches_ground_truth,
    test_stamping_preserves_id_and_promotes_layer,
    test_summary_survival_rate,
    test_prompt_carries_rulebook_finding_and_lens,
)


def main() -> int:
    """Run every test directly (no pytest dependency)."""
    for test in _TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(_TESTS)} L3 verifier tests passed ({len(DEFAULT_LENSES)} lenses).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
