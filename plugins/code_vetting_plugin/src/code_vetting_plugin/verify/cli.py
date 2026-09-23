"""L3 verifier CLI — the dogfood driver.

Reads a candidate finding register (F1 JSON, as L1/L2 emit), runs the
adversarial refute-harness over it with the deterministic heuristic dispatcher,
and writes two artifacts: the verified register (F1 JSON, every finding stamped
confirmed/refuted, ``layer=L3_verified``) and a human-readable run report.

Wave-2 swaps the dispatcher for the inference substrate (the platform's
peer-messaging bridge); nothing else here changes.

    python -m code_vetting_plugin.verify.cli \\
        --candidates workbench/<candidates>.json \\
        --out        workbench/<candidates>_verified.json \\
        --report     workbench/<candidates>_report.md

Exit codes: 0 — verification ran; 64 — usage error. A confirmed finding is not
an error here; it is the output. Blocking-vs-advisory is the report consumer's
call (context profile, F2 §3).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ..models import Finding, Severity, findings_from_json, findings_to_json
from .dispatch import HeuristicSkepticDispatcher, SkepticVote
from .rulebook import load_rulebook
from .verifier import (
    AdversarialVerifier,
    VerificationOutcome,
    VerificationSummary,
    confirmed_findings,
    summarize,
)

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.BLOCKER: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.ADVISORY: 4,
}


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="l3-verifier", description="Adversarial L3 verification of candidate findings.")
    parser.add_argument("--candidates", required=True, help="F1 candidate finding register (JSON array).")
    parser.add_argument("--out", default=None, help="Verified register output (default: <candidates>_verified.json).")
    parser.add_argument("--report", default=None, help="Run report output (default: <candidates>_report.md).")
    return parser.parse_args(argv)


def _default_sibling(candidates: Path, suffix: str) -> Path:
    return candidates.with_name(f"{candidates.stem}{suffix}")


def _severity_line(finding: Finding) -> str:
    line = "—" if finding.line is None else str(finding.line)
    return f"- **[{finding.severity.value}]** `{finding.file}:{line}` — {finding.constraint_violated}"


def _render_confirmed(findings: Sequence[Finding]) -> list[str]:
    if not findings:
        return ["## Confirmed findings", "", "_None — every candidate was refuted._"]
    ranked = sorted(findings, key=lambda f: (_SEVERITY_RANK[f.severity], f.file, f.line or 0))
    lines = ["## Confirmed findings", "", f"{len(ranked)} finding(s) survived adversarial verification.", ""]
    for finding in ranked:
        lines.append(_severity_line(finding))
        lines.append(f"  - dimension: `{finding.dimension.value}` · provenance: `{finding.provenance.source}`")
        lines.append(f"  - evidence: {finding.evidence}")
        if finding.fix_suggestion:
            lines.append(f"  - fix: {finding.fix_suggestion}")
    return lines


def _refutation_reason(outcome: VerificationOutcome) -> str:
    refutes = [r for r in outcome.responses if r.vote is SkepticVote.REFUTE]
    dispositive = next((r for r in refutes if r.dispositive), None)
    if dispositive is not None:
        return f"dispositive {dispositive.rule_id or 'DO-NOT-FLAG'} ({dispositive.lens.value}): {dispositive.rationale}"
    lenses = ", ".join(r.lens.value for r in refutes) or "majority"
    return f"majority-refute [{lenses}]"


def _render_refuted(outcomes: Sequence[VerificationOutcome]) -> list[str]:
    refuted = [o for o in outcomes if o.verdict.value == "refuted"]
    if not refuted:
        return ["## Refuted (dropped) findings", "", "_None._"]
    lines = ["## Refuted (dropped) findings", "", f"{len(refuted)} candidate(s) killed as false positives:", ""]
    for outcome in refuted:
        finding = outcome.finding
        loc = "—" if finding.line is None else str(finding.line)
        lines.append(f"- `{finding.file}:{loc}` ({finding.dimension.value}) — {finding.constraint_violated}")
        lines.append(f"  - reason: {_refutation_reason(outcome)}")
    return lines


def _render_summary(summary: VerificationSummary, rulebook_source: str) -> list[str]:
    rate = "n/a" if summary.survival_rate is None else f"{summary.survival_rate:.1%}"
    lens_line = ", ".join(f"{lens}={count}" for lens, count in summary.refutes_by_lens.items())
    dims = ", ".join(f"{dim}={count}" for dim, count in summary.confirmed_by_dimension.items()) or "—"
    return [
        "# L3 Adversarial Verification — Run Report",
        "",
        f"- rulebook (F2): `{rulebook_source}`",
        f"- candidates verified: **{summary.total}**",
        f"- confirmed: **{summary.confirmed}** · refuted: **{summary.refuted}** · dispositive kills: **{summary.dispositive_refutations}**",
        f"- survival rate (L2→L3 precision proxy): **{rate}**",
        f"- refutes by lens: {lens_line}",
        f"- confirmed by dimension: {dims}",
        "",
    ]


def render_report(outcomes: Sequence[VerificationOutcome], summary: VerificationSummary, rulebook_source: str) -> str:
    """Render the human-facing run report (markdown)."""
    blocks = [
        _render_summary(summary, rulebook_source),
        _render_confirmed(confirmed_findings(outcomes)),
        [""],
        _render_refuted(outcomes),
        [""],
    ]
    return "\n".join(line for block in blocks for line in block) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: verify a candidate register, write the verified register + report."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    candidates_path = Path(args.candidates)
    out_path = Path(args.out) if args.out else _default_sibling(candidates_path, "_verified.json")
    report_path = Path(args.report) if args.report else _default_sibling(candidates_path, "_report.md")

    rulebook = load_rulebook()
    candidates = findings_from_json(candidates_path.read_text(encoding="utf-8"))
    verifier = AdversarialVerifier(HeuristicSkepticDispatcher(rulebook), rulebook)
    outcomes = verifier.verify(candidates)
    summary = summarize(outcomes)

    out_path.write_text(findings_to_json([outcome.finding for outcome in outcomes]), encoding="utf-8")
    report_path.write_text(render_report(outcomes, summary, rulebook.source_path), encoding="utf-8")

    rate = "n/a" if summary.survival_rate is None else f"{summary.survival_rate:.1%}"
    print(f"L3: {summary.total} verified → {summary.confirmed} confirmed, {summary.refuted} refuted (survival {rate})")
    print(f"  verified register: {out_path}")
    print(f"  run report:        {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
