"""report_accounting_smoke.py — FT-1.1 defect 3 (count/render footer reconciliation).

The first live foreign vet produced a report whose ``## What was filtered`` footer did
not add up: emitted findings could not be reconciled against the rendered count because
``confirmed`` and ``promoted`` were counted on overlapping predicates and the dedup delta
was invisible. This pins the fix: every emitted finding lands in exactly one partition
bucket, so ``rendered + filtered == emitted``, and the collapsed-duplicate count makes
``rendered-unique == rendered-raw - collapsed`` explicit. The report never under-states.

Run directly: ``.venv/bin/python3 plugins/code_vetting_plugin/tests/report_accounting_smoke.py``.
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
    Verdict,
)
from code_vetting_plugin.report import ReportRenderer
from code_vetting_plugin.run_record import RunTarget

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _finding(
    *,
    dimension: Dimension,
    layer: Layer,
    severity: Severity,
    file: str,
    line: int,
    constraint: str,
    verdict: Verdict = Verdict.CANDIDATE,
) -> Finding:
    built = Finding.build(
        run_id="vr-ft11-3",
        layer=layer,
        dimension=dimension,
        severity=severity,
        file=file,
        line=line,
        constraint_violated=constraint,
        evidence=f"evidence for {constraint}",
        provenance=Provenance(source="smoke"),
        context_profile=ContextProfile.PRODUCTION,
    )
    return built if verdict is Verdict.CANDIDATE else built.with_verdict(verdict)


def _scenario() -> list[Finding]:
    """One finding per bucket, plus a duplicate finding-id in the promoted bucket."""
    promoted = _finding(
        dimension=Dimension.SECRETS, layer=Layer.L1_DETERMINISTIC, severity=Severity.HIGH,
        file="b.py", line=2, constraint="gitleaks:aws-access-token",
    )
    return [
        _finding(  # confirmed (rendered)
            dimension=Dimension.SECURITY, layer=Layer.L3_VERIFIED, severity=Severity.HIGH,
            file="a.py", line=1, constraint="bandit:B602", verdict=Verdict.CONFIRMED,
        ),
        promoted,        # zero-FP promoted (rendered)
        promoted,        # SAME finding_id — collapses on dedup
        _finding(  # refuted (filtered)
            dimension=Dimension.CORRECTNESS, layer=Layer.L3_VERIFIED, severity=Severity.MEDIUM,
            file="c.py", line=3, constraint="critic:logic", verdict=Verdict.REFUTED,
        ),
        _finding(  # downgraded secret (filtered, tallied)
            dimension=Dimension.SECRETS, layer=Layer.L1_DETERMINISTIC, severity=Severity.ADVISORY,
            file="d.py", line=4, constraint="gitleaks:generic-api-key",
        ),
        _finding(  # unverified non-promoted candidate (filtered) — network_bind is NOT zero-FP
            dimension=Dimension.NETWORK_BIND, layer=Layer.L1_DETERMINISTIC, severity=Severity.LOW,
            file="e.py", line=5, constraint="rg:bind-all-interfaces",
        ),
    ]


def _render(findings: list[Finding]) -> str:
    return ReportRenderer().render(
        run_id="vr-ft11-3",
        target=RunTarget(repo="example", ref="deadbeef", scope="s"),
        context_profile=ContextProfile.PRODUCTION,
        generated_at="t",
        findings=findings,
        coverage=[],
    )


def _check_partition_reconciles() -> None:
    findings = _scenario()
    renderer = ReportRenderer()
    counts = renderer._partition_counts(findings)  # noqa: SLF001 — pin the reconciliation invariant
    _check("partition covers every emitted finding exactly once", sum(counts.values()) == len(findings), str(counts))
    rendered_raw = counts["confirmed"] + counts["promoted"]
    filtered = counts["refuted"] + counts["downgraded_secret"] + counts["unverified"]
    _check("rendered_raw + filtered == emitted (no leakage)", rendered_raw + filtered == len(findings), f"{rendered_raw}+{filtered}!={len(findings)}")
    _check("each bucket has its one expected member", counts == {"confirmed": 1, "promoted": 2, "refuted": 1, "downgraded_secret": 1, "unverified": 1}, str(counts))


def _check_footer_text_reconciles() -> None:
    report = _render(_scenario())
    # 6 emitted; 2 rendered-unique (confirmed + promoted, the dup collapsed); 3 filtered.
    _check("footer states emitted 6", "Emitted across all layers: **6**" in report, report)
    _check("footer states rendered-unique 2", "rendered above (deduped by finding-id): **2**" in report, report)
    _check("footer states rendered-raw 3 with 1 collapsed", "Rendered (pre-dedup **3**)" in report and "duplicate finding-ids collapsed **1**" in report, report)
    _check("footer states filtered 3, itemized", "Filtered / not rendered (**3**)" in report, report)
    _check("footer names the unverified non-promoted candidate (the low network_bind class)", "unverified non-promoted candidates **1**" in report, report)
    _check("footer states rendered+filtered==emitted invariant in prose", "rendered + filtered equals emitted" in report, report)
    # The summary headline must agree with the deduped rendered count (never the raw 3).
    _check("summary headline agrees with rendered-unique (2)", "2 finding(s) to report" in report, report)


def _check_cap_overflow_reconciles() -> None:
    """R7-4 (design §6): the tsc/eslint flood cap drops overflow at the SCANNER, so the report only
    ever sees the emitted (≤200) set. The footer must still reconcile with a full cap present — the
    200 promoted findings are exactly the set reasoned over; rendered == emitted, filtered == 0."""
    capped = 200  # _MAX_TOOL_FINDINGS worth of promoted TYPE_COVERAGE findings (what a capped tsc run emits)
    findings = [
        _finding(
            dimension=Dimension.TYPE_COVERAGE, layer=Layer.L1_DETERMINISTIC, severity=Severity.MEDIUM,
            file=f"src/f{index:04d}.ts", line=index, constraint=f"tsc:TS{2000 + index}",
        )
        for index in range(capped)
    ]
    renderer = ReportRenderer()
    counts = renderer._partition_counts(findings)  # noqa: SLF001 — pin the reconciliation invariant under a cap
    _check("cap: every capped finding partitions exactly once", sum(counts.values()) == capped, str(counts))
    _check("cap: all 200 are promoted (zero-FP TYPE_COVERAGE), none filtered", counts["promoted"] == capped and counts["refuted"] + counts["unverified"] + counts["downgraded_secret"] == 0, str(counts))
    report = _render(findings)
    _check("cap: footer reconciles emitted 200", "Emitted across all layers: **200**" in report, report[-600:])
    _check("cap: summary headline agrees (200 to report)", "200 finding(s) to report" in report, report[:400])


def main() -> int:
    try:
        _check_partition_reconciles()
        _check_footer_text_reconciles()
        _check_cap_overflow_reconciles()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"report_accounting_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
