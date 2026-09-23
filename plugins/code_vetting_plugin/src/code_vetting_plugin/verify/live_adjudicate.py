"""Live L3 adjudication driver — inference substrate, real candidate.

Runs the refute-harness with the **inference** dispatcher against a real L2
candidate, seeding each lens with an evidence pack (the actual source under
review + precedent + the upstream critic's both-sides framing). The skeptic
replies are gathered out-of-band over the platform's peer-messaging bridge by
the orchestrating agent and supplied here as a recorded-replies file, so the run is
reproducible and the parse→aggregate→stamp path is exercised end-to-end.

    python -m code_vetting_plugin.verify.live_adjudicate \\
        --candidates workbench/<candidate>.json \\
        --evidence   workbench/<evidence_pack>.md \\
        --replies    workbench/<skeptic_replies>.json \\
        --out        workbench/<verified>.json \\
        --report     workbench/<adjudication>.md

The report renders the FULL per-lens audit trail (each skeptic's vote,
dispositive flag, rule, and rationale) plus the aggregated verdict — the
disagreement record is the whole point of the proof.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..models import Finding, findings_from_json, findings_to_json
from .dispatch import SkepticResponse
from .inference import InferenceSkepticDispatcher, RecordedTransport, reply_key
from .lenses import SkepticLens
from .rulebook import load_rulebook
from .verifier import AdversarialVerifier, VerificationOutcome, summarize


def _flatten_replies(raw: Mapping[str, object]) -> dict[str, str]:
    """Flatten a ``{finding_id: {lens: reply_text}}`` file to reply-key form."""
    flat: dict[str, str] = {}
    for finding_id, lens_map in raw.items():
        if not isinstance(lens_map, Mapping):
            raise TypeError(f"replies[{finding_id!r}] must be a mapping of lens → reply text")
        for lens_value, text in lens_map.items():
            if not isinstance(text, str):
                raise TypeError(f"reply for {finding_id}/{lens_value} must be a string")
            flat[reply_key(finding_id, SkepticLens(lens_value))] = text
    return flat


def _load_replies(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("replies file must be a JSON object keyed by finding_id")
    return _flatten_replies(raw)


def _render_response(response: SkepticResponse) -> list[str]:
    veto = " · **DISPOSITIVE**" if response.dispositive else ""
    rule = f" · rule=`{response.rule_id}`" if response.rule_id else ""
    return [
        f"  - **{response.lens.value}** → `{response.vote.value.upper()}`{veto}{rule}",
        f"    - {response.rationale}",
    ]


def _render_outcome(outcome: VerificationOutcome) -> list[str]:
    finding = outcome.finding
    loc = "—" if finding.line is None else str(finding.line)
    lines = [
        f"### {finding.constraint_violated} — VERDICT: **{finding.verdict.value.upper()}**",
        "",
        f"- `{finding.file}:{loc}` · dimension `{finding.dimension.value}` · severity `{finding.severity.value}`",
        f"- finding_id: `{finding.finding_id}` · layer now `{finding.layer.value}`",
        "- per-lens skeptic verdicts:",
    ]
    for response in outcome.responses:
        lines.extend(_render_response(response))
    return lines


def render_adjudication(outcomes: Sequence[VerificationOutcome]) -> str:
    """Render the full per-lens audit trail + aggregated verdict per finding."""
    summary = summarize(outcomes)
    rate = "n/a" if summary.survival_rate is None else f"{summary.survival_rate:.0%}"
    blocks: list[list[str]] = [
        [
            "# L3 LIVE Adjudication — Inference Substrate",
            "",
            f"- candidates: **{summary.total}** · confirmed: **{summary.confirmed}** · refuted: **{summary.refuted}**",
            f"- survival rate: **{rate}** · dispositive kills: **{summary.dispositive_refutations}**",
            f"- refutes by lens: {', '.join(f'{k}={v}' for k, v in summary.refutes_by_lens.items())}",
            "",
        ]
    ]
    for outcome in outcomes:
        blocks.append(_render_outcome(outcome))
        blocks.append([""])
    return "\n".join(line for block in blocks for line in block) + "\n"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="l3-live-adjudicate", description="Inference-substrate L3 adjudication of a real candidate.")
    parser.add_argument("--candidates", required=True, help="F1 candidate register (JSON array).")
    parser.add_argument("--evidence", required=True, help="Evidence pack (source + precedent + both-sides framing).")
    parser.add_argument("--replies", required=True, help="Recorded skeptic replies: {finding_id: {lens: reply_text}}.")
    parser.add_argument("--out", required=True, help="Verified register output (F1 JSON).")
    parser.add_argument("--report", required=True, help="Adjudication report output (markdown).")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the inference-substrate adjudication over the candidate register."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    rulebook = load_rulebook()
    candidates = findings_from_json(Path(args.candidates).read_text(encoding="utf-8"))
    evidence_pack = Path(args.evidence).read_text(encoding="utf-8")
    transport = RecordedTransport(_load_replies(Path(args.replies)))

    def provide_context(_finding: Finding) -> str:
        return evidence_pack

    verifier = AdversarialVerifier(
        InferenceSkepticDispatcher(transport),
        rulebook,
        context_provider=provide_context,
    )
    outcomes = verifier.verify(candidates)

    Path(args.out).write_text(findings_to_json([outcome.finding for outcome in outcomes]), encoding="utf-8")
    Path(args.report).write_text(render_adjudication(outcomes), encoding="utf-8")

    for outcome in outcomes:
        votes = ", ".join(f"{r.lens.value}={r.vote.value}" for r in outcome.responses)
        print(f"{outcome.finding.constraint_violated}: {outcome.verdict.value.upper()}  [{votes}]")
    print(f"  verified register: {args.out}")
    print(f"  adjudication report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
