"""Tests for the Wave-2 inference dispatcher — reply parsing + transport + wiring.

Runnable under pytest or directly (``python -m
code_vetting_plugin.verify.tests.test_inference``).
"""

from __future__ import annotations

from ...toolrun import ToolOutcome
from ..dispatch import SkepticRequest, SkepticVote
from ..inference import (
    InferenceSkepticDispatcher,
    RecordedTransport,
    SubprocessSkepticTransport,
    parse_skeptic_reply,
    reply_key,
)
from ..lenses import DEFAULT_LENSES, SkepticLens
from ..rulebook import load_rulebook
from ..verifier import AdversarialVerifier, VerificationPolicy, aggregate_votes
from .sample_findings import sample_candidates

_POLICY = VerificationPolicy()


def _runner_ok(argv: list[str], **_kwargs: object) -> ToolOutcome:
    return ToolOutcome(returncode=0, stdout="vote: REFUTE\ndispositive: false\nrule_id: RB-STATE\nrationale: x", stderr="")


def _runner_timeout(argv: list[str], **_kwargs: object) -> ToolOutcome:
    return ToolOutcome(returncode=-1, stdout="", stderr="timed out", timed_out=True)


def test_parse_refute_dispositive_with_rule() -> None:
    text = "vote: REFUTE\ndispositive: true\nrule_id: F2§4.1\nrationale: absent try/except is policy."
    response = parse_skeptic_reply(text, SkepticLens.POLICY)
    assert response.vote is SkepticVote.REFUTE
    assert response.dispositive is True
    assert response.rule_id == "F2§4.1"
    assert "policy" in response.rationale


def test_parse_uphold() -> None:
    text = "vote: UPHOLD\ndispositive: false\nrule_id: null\nrationale: real RB-STATE violation."
    response = parse_skeptic_reply(text, SkepticLens.CORRECTNESS)
    assert response.vote is SkepticVote.UPHOLD
    assert response.dispositive is False
    assert response.rule_id is None


def test_uphold_cannot_be_dispositive() -> None:
    text = "vote: UPHOLD\ndispositive: true\nrationale: dispositive only applies to a kill."
    response = parse_skeptic_reply(text, SkepticLens.POLICY)
    assert response.vote is SkepticVote.UPHOLD
    assert response.dispositive is False


def test_unreadable_reply_is_uncertain() -> None:
    response = parse_skeptic_reply("the model rambled without a verdict", SkepticLens.REPRODUCE)
    assert response.vote is SkepticVote.UNCERTAIN


def test_prose_vote_without_field_label() -> None:
    response = parse_skeptic_reply("On balance I would UPHOLD this finding.", SkepticLens.CORRECTNESS)
    assert response.vote is SkepticVote.UPHOLD


def test_recorded_transport_missing_reply_raises() -> None:
    transport = RecordedTransport({})
    finding = sample_candidates()[0]
    request = SkepticRequest(finding, SkepticLens.POLICY, "prompt")
    raised = False
    try:
        transport.infer(request)
    except KeyError:
        raised = True
    assert raised, "a missing recorded reply must fail loud"


def test_inference_dispatcher_parses_all() -> None:
    finding = sample_candidates()[0]
    replies = {
        reply_key(finding.finding_id, SkepticLens.CORRECTNESS): "vote: UPHOLD\nrationale: real.",
        reply_key(finding.finding_id, SkepticLens.POLICY): "vote: UPHOLD\nrationale: not policy noise.",
        reply_key(finding.finding_id, SkepticLens.REPRODUCE): "vote: REFUTE\nrationale: no evidence.",
    }
    dispatcher = InferenceSkepticDispatcher(RecordedTransport(replies))
    requests = [SkepticRequest(finding, lens, "p") for lens in DEFAULT_LENSES]
    responses = dispatcher.evaluate_batch(requests)
    assert [r.vote for r in responses] == [SkepticVote.UPHOLD, SkepticVote.UPHOLD, SkepticVote.REFUTE]
    assert aggregate_votes(responses, _POLICY).value == "confirmed"


def test_subprocess_transport_returns_stdout() -> None:
    transport = SubprocessSkepticTransport(cwd=".", runner=_runner_ok)
    request = SkepticRequest(sample_candidates()[0], SkepticLens.POLICY, "prompt")
    assert "REFUTE" in transport.infer(request)


def test_subprocess_transport_timeout_is_uncertain() -> None:
    transport = SubprocessSkepticTransport(cwd=".", runner=_runner_timeout)
    request = SkepticRequest(sample_candidates()[0], SkepticLens.POLICY, "prompt")
    response = parse_skeptic_reply(transport.infer(request), SkepticLens.POLICY)
    assert response.vote is SkepticVote.UNCERTAIN


def test_subprocess_dispatch_end_to_end() -> None:
    finding = sample_candidates()[0]
    dispatcher = InferenceSkepticDispatcher(SubprocessSkepticTransport(cwd=".", runner=_runner_ok))
    requests = [SkepticRequest(finding, lens, "p") for lens in DEFAULT_LENSES]
    responses = dispatcher.evaluate_batch(requests)
    assert all(r.vote is SkepticVote.REFUTE for r in responses)


def test_inference_end_to_end_through_verifier() -> None:
    rulebook = load_rulebook()
    finding = sample_candidates()[0]
    replies = {
        reply_key(finding.finding_id, lens): "vote: REFUTE\ndispositive: false\nrationale: refuted."
        for lens in DEFAULT_LENSES
    }
    verifier = AdversarialVerifier(InferenceSkepticDispatcher(RecordedTransport(replies)), rulebook)
    outcomes = verifier.verify([finding])
    assert outcomes[0].verdict.value == "refuted"
    assert outcomes[0].finding.finding_id == finding.finding_id


_TESTS = (
    test_parse_refute_dispositive_with_rule,
    test_parse_uphold,
    test_uphold_cannot_be_dispositive,
    test_unreadable_reply_is_uncertain,
    test_prose_vote_without_field_label,
    test_recorded_transport_missing_reply_raises,
    test_inference_dispatcher_parses_all,
    test_subprocess_transport_returns_stdout,
    test_subprocess_transport_timeout_is_uncertain,
    test_subprocess_dispatch_end_to_end,
    test_inference_end_to_end_through_verifier,
)


def main() -> int:
    """Run every test directly (no pytest dependency)."""
    for test in _TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(_TESTS)} L3 inference tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
