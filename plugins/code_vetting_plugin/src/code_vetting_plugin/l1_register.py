"""Render the L1 deterministic findings register (markdown).

This is the L1 *candidate* register — every deterministic finding the scanners
produced, grouped by severity. It is distinct from Stream O's ``report.py``,
which renders only confirmed + zero-FP-promoted findings for the final report.
The register is the full L1 workbench artifact; the machine-complete view is the
F1 JSON sidecar (``models.findings_to_json``), linked in the header.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import Finding, Severity
from .run_record import CoverageRecord

_SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.BLOCKER,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.ADVISORY,
)


def _cell(text: str, limit: int = 160) -> str:
    """Markdown-table-safe cell: collapse whitespace, escape pipes, bound length."""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return flat.replace("|", "\\|")


def _counts_by_severity(findings: Sequence[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {sev.value: 0 for sev in Severity}
    for finding in findings:
        counts[finding.severity.value] += 1
    return counts


def _counts_by_dimension(findings: Sequence[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.dimension.value] = counts.get(finding.dimension.value, 0) + 1
    return counts


def _severity_section(severity: Severity, findings: Sequence[Finding]) -> list[str]:
    group = [f for f in findings if f.severity == severity]
    if not group:
        return []
    lines = [
        f"### {severity.value.upper()} ({len(group)})",
        "",
        "| dimension | file:line | constraint | source | evidence |",
        "|---|---|---|---|---|",
    ]
    for finding in sorted(group, key=lambda f: (f.dimension.value, f.file, f.line or 0)):
        location = finding.file if finding.line is None else f"{finding.file}:{finding.line}"
        lines.append(
            f"| {finding.dimension.value} | {_cell(location, 80)} | {_cell(finding.constraint_violated, 60)} "
            f"| {_cell(finding.provenance.source, 40)} | {_cell(finding.evidence)} |"
        )
    lines.append("")
    return lines


def _coverage_section(coverage: Sequence[CoverageRecord]) -> list[str]:
    lines = ["## Coverage", "", "| scanner | ran | files examined | gap |", "|---|---|---|---|"]
    for record in coverage:
        lines.append(
            f"| {record.scanner} | {'yes' if record.ran else '**NO**'} | {record.files_examined} "
            f"| {_cell(record.gap_reason or '', 120)} |"
        )
    gaps = [f"- **{r.scanner}**: {r.gap_reason}" for r in coverage if not r.ran and r.gap_reason is not None]
    if gaps:
        lines.extend(["", "### Coverage gaps (not examined — surfaced, not swallowed)", "", *gaps])
    lines.append("")
    return lines


def _header(run_id: str, repo: str, ref: str, scope: str, started: str, finished: str, json_sidecar: str) -> list[str]:
    return [
        "# Vetting Suite — L1 Deterministic Findings Register",
        "",
        f"**Run:** `{run_id}` · **Target:** `{repo}` @ `{ref}` · **Scope:** {scope}",
        f"**Started:** {started} · **Finished:** {finished}",
        "**Layer:** `L1_deterministic` · **Verdict:** `candidate` (unverified) · **Context profile:** `production`",
        f"**Machine-complete records:** `{json_sidecar}` (F1 schema; this register is the human view)",
        "",
        "All records emit the F1 shape (`workbench/2026-07-19_vetting_finding_schema_v1.md`) via the shared "
        "`code_vetting_plugin/models.py` binding. L1 findings are *candidates* — deterministic facts pending "
        "L2/L3 verification and report-stage promotion of the zero-FP dimensions.",
        "",
    ]


def _schema_notes() -> list[str]:
    return [
        "## Schema-fit notes",
        "",
        "- **SAST dimension:** bandit/semgrep and the SQL-access gate emit `dimension=security` at "
        "`layer=L1_deterministic`. These are candidates that await L3 verification (SAST has real false positives), "
        "so `security` is intentionally NOT in the report's zero-FP promotion set — they surface via L3, not "
        "auto-promotion. The layer field disambiguates the deterministic producer from an L2 critic.",
        "- **Platform-gate dimension:** aggregate gate findings emit `dimension=code_quality` (the promoted L1 "
        "bucket for deterministic gate facts); the specific gate is preserved in `constraint_violated`.",
        "",
    ]


def render_register(
    *,
    run_id: str,
    repo: str,
    ref: str,
    scope: str,
    started: str,
    finished: str,
    json_sidecar: str,
    findings: Sequence[Finding],
    coverage: Sequence[CoverageRecord],
) -> str:
    """Render the full L1 findings register markdown."""
    sev_counts = _counts_by_severity(findings)
    dim_counts = _counts_by_dimension(findings)
    lines = _header(run_id, repo, ref, scope, started, finished, json_sidecar)
    lines.extend(
        [
            "## Summary",
            "",
            f"- **Total L1 findings:** {len(findings)}",
            "- **By severity:** " + ", ".join(f"{sev.value}={sev_counts[sev.value]}" for sev in _SEVERITY_ORDER),
            "- **By dimension:** " + (", ".join(f"{k}={v}" for k, v in sorted(dim_counts.items())) or "none"),
            "",
        ]
    )
    lines.extend(_coverage_section(coverage))
    lines.extend(["## Findings", "", "Grouped by severity (blocker → advisory).", ""])
    any_findings = False
    for severity in _SEVERITY_ORDER:
        section = _severity_section(severity, findings)
        if section:
            any_findings = True
            lines.extend(section)
    if not any_findings:
        lines.extend(["_No findings — every scanner that ran reported clean._", ""])
    lines.extend(_schema_notes())
    return "\n".join(lines)
