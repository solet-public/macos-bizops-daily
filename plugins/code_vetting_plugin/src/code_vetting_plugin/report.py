"""report.py — the single severity-ranked vetting report renderer (Stream O).

Emits one markdown report per run: findings ranked most-severe first, each
anchored to ``file:line`` with its provenance, the exact constraint it violated,
its evidence, and a concrete fix. Per the F1 layer contract the report renders
**confirmed** findings (L3-verified) plus **zero-false-positive L1** findings
(deterministic dimensions promoted without L3). Everything else — unverified L2
candidates, refuted findings, non-promoted L1 candidates — is not rendered as a
finding but is tallied in the honesty footer, so the report never hides what it
filtered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import ContextProfile, Dimension, Finding, Layer, Provenance, Severity, Verdict
from .run_record import CoverageRecord, RunTarget
from .scanners.dead_code import DeadSymbolsReport, render_candidate_dead_symbols_section
from .scanners.rulebook_sync import STALE_RULEBOOK_CONSTRAINT
from .scanners.structural_metrics import StructuralMetricsReport, render_structural_metrics_section
from .source_roles import render_source_role_section
from .technology_fingerprint import render_technology_fingerprint_section
from .test_coverage import TestCoverageReport, render_test_coverage_section

# Most-severe first. The report ranks findings by this order.
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.BLOCKER,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.ADVISORY,
)
_SEVERITY_RANK: dict[Severity, int] = {severity: rank for rank, severity in enumerate(SEVERITY_ORDER)}

# Deterministic L1 dimensions promoted into the report without an L3 pass —
# effectively zero-false-positive facts (F1 §1 "zero-FP L1"). Two groups
# (Coordinator-Dusk ruling, 2026-07-19): tool-verified findings
# (secrets/hidden_unicode/license/deps) and the gate-backed deterministic dims
# (complexity/dead_code/type_coverage) — the latter are allowlist-filtered gate
# facts, so L1 only surfaces NEW, non-allowlisted violations and L3 adjudication
# would be wasted inference. dup/orphan/identity_leak/network_bind are
# deliberately EXCLUDED — they genuinely false-positive and get an L1→L3 path in
# Wave 2 (advisory/footer until then). A report-stage promotion policy, overridable
# per run; kept here rather than on the record so the finding stays data-only.
DEFAULT_ZERO_FP_DIMENSIONS: frozenset[Dimension] = frozenset(
    {
        Dimension.SECRETS,
        Dimension.HIDDEN_UNICODE,
        Dimension.LICENSE,
        Dimension.DEPS,
        Dimension.COMPLEXITY,
        Dimension.TYPE_COVERAGE,
        Dimension.CODE_QUALITY,
    }
)
# DEAD_CODE is deliberately NOT here (R9-A ruling §A.2 — advisory posture as TEST_REACH: the
# dimension carries judgment-adjacent content, the 60% candidate family). Its two provable finding
# classes (unreachable/unused-import) instead render as facts by emitting ``verdict=CONFIRMED`` from
# the deterministic scanner (§A.1), so they do not rely on dimension promotion.

# W3C-1b: the rulebook-integrity banner. ``rulebook_sync`` emits a HIGH ``stale_rulebook`` finding when
# the shipped moat is corrupt or has drifted behind its sources; the report then renders this WARNING in
# the title block so a stale run is unmissable — A0 R4-b's "blocking" mechanized honestly (report-not-gate
# stands; the C3 joseki's ``blocks_continuation`` gate is the pipeline enforcement).
_INTEGRITY_BANNER = (
    "> ⚠️ **RULEBOOK INTEGRITY WARNING — this run's trust chain is compromised.**\n"
    ">\n"
    "> The assembled rulebook is stale or corrupt (`stale_rulebook`), so every AI verdict in this run "
    "was computed against a moat that may no longer match canon. Re-run the assembler, commit the "
    "regenerated artifact, and re-run the vet. See the HIGH `code_quality` finding below."
)


def _has_stale_rulebook(findings: Sequence[Finding]) -> bool:
    """True iff the run carries the ``rulebook_sync`` integrity finding (drives the title-block banner)."""
    return any(finding.constraint_violated == STALE_RULEBOOK_CONSTRAINT for finding in findings)


def _is_downgraded_secret(finding: Finding) -> bool:
    """A low-confidence secret-rule hit downgraded on a known-safe path (suite v1.1).

    The only ADVISORY-severity secrets finding is a ``scanners/secrets.py``
    downgrade (a ``generic-api-key`` catch-all landing on a test/archive/artifact
    path). It is not a zero-FP fact: tally it in the footer, never promote it.
    """
    return finding.dimension is Dimension.SECRETS and finding.severity is Severity.ADVISORY


def is_zero_fp_promoted(finding: Finding, zero_fp_dimensions: frozenset[Dimension]) -> bool:
    """A deterministic L1 finding in a zero-FP dimension — shown without an L3 pass.

    Shared by the driver (which routes everything *else* through L3) and the
    renderer (which promotes these into the report), so both agree on exactly one
    definition of "bypasses verification" (F1 §1). A downgraded low-confidence
    secret (advisory) is excluded — it is not a zero-FP fact (suite v1.1).
    """
    if finding.layer is not Layer.L1_DETERMINISTIC or finding.dimension not in zero_fp_dimensions:
        return False
    return not _is_downgraded_secret(finding)


def _anchor(finding: Finding) -> str:
    """The clickable ``file:line`` anchor (``file`` alone for file-level findings)."""
    return finding.file if finding.line is None else f"{finding.file}:{finding.line}"


def _sort_key(finding: Finding) -> tuple[int, str, int, int]:
    """Severity-first ordering; file then line break ties; null line sorts last."""
    line_present = 0 if finding.line is not None else 1
    return (_SEVERITY_RANK[finding.severity], finding.file, line_present, finding.line or 0)


def _render_provenance(provenance: Provenance) -> str:
    """One-line provenance: source plus any tool version / critic lens / rule id."""
    parts = [provenance.source]
    if provenance.tool_version is not None:
        parts.append(f"v{provenance.tool_version}")
    if provenance.critic_lens is not None:
        parts.append(f"lens={provenance.critic_lens}")
    if provenance.rule_id is not None:
        parts.append(f"rule={provenance.rule_id}")
    return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class ReportRenderer:
    """Renders the run's report markdown from its findings + coverage."""

    zero_fp_dimensions: frozenset[Dimension] = DEFAULT_ZERO_FP_DIMENSIONS

    def _is_promoted_l1(self, finding: Finding) -> bool:
        return is_zero_fp_promoted(finding, self.zero_fp_dimensions)

    def _select(self, findings: Sequence[Finding]) -> list[Finding]:
        """Confirmed + zero-FP-L1 findings, deduped by id, most-severe first."""
        by_id: dict[str, Finding] = {}
        for finding in findings:
            if finding.verdict is Verdict.CONFIRMED or self._is_promoted_l1(finding):
                by_id[finding.finding_id] = finding
        return sorted(by_id.values(), key=_sort_key)

    def render(
        self,
        *,
        run_id: str,
        target: RunTarget,
        context_profile: ContextProfile,
        generated_at: str,
        findings: Sequence[Finding],
        coverage: Sequence[CoverageRecord],
        test_coverage: TestCoverageReport | None = None,
        preamble: str | None = None,
        enumeration: str | None = None,
        file_count: int | None = None,
        stacks: str | None = None,
        structural_metrics: StructuralMetricsReport | None = None,
        dead_symbols: DeadSymbolsReport | None = None,
    ) -> str:
        """Build the full markdown report for one run.

        ``enumeration`` (``git``|``walk``) + ``file_count`` record HOW a foreign
        target was inventoried (FT-1); omitted (None) for the byte-compatible
        self-vet default. ``test_coverage`` carries the opt-in coverage run-profile's
        per-owner rollup; None (the fast-verb path) renders a stable placeholder.
        ``stacks`` (R7-1) is the detected language-stack label (e.g. ``python`` /
        ``typescript, javascript``) — the engine's provenance for what it believed the
        target was; None for callers that do not detect stacks.
        """
        selected = self._select(findings)
        banner = _INTEGRITY_BANNER if _has_stale_rulebook(findings) else ""
        blocks = [
            self._header(run_id, target, context_profile, generated_at, preamble, enumeration, file_count, stacks, banner),
        ]
        if target.technology_fingerprint is not None:
            # Keep the routing evidence before Summary/Findings so the bounded
            # inline report preserves it when later detail is truncated.
            blocks.append(
                render_technology_fingerprint_section(target.technology_fingerprint)
            )
        blocks.extend(
            (
                self._summary(selected),
                self._findings_section(selected),
                self._coverage_section(coverage),
                render_structural_metrics_section(structural_metrics),
                render_candidate_dead_symbols_section(dead_symbols),
                render_test_coverage_section(test_coverage),
            )
        )
        if target.source_role_partition is not None:
            blocks.append(render_source_role_section(target.source_role_partition))
        blocks.append(self._footer(findings, selected))
        return "\n\n".join(blocks) + "\n"

    def _header(
        self,
        run_id: str,
        target: RunTarget,
        context_profile: ContextProfile,
        generated_at: str,
        preamble: str | None,
        enumeration: str | None = None,
        file_count: int | None = None,
        stacks: str | None = None,
        integrity_banner: str = "",
    ) -> str:
        ref_display = target.ref if target.ref else "(no ref)"
        lines = [f"# Vetting report — `{target.repo}` @ `{ref_display}`"]
        if integrity_banner:
            lines.extend(("", integrity_banner))
        lines.extend((
            "",
            f"- **Run:** `{run_id}`",
            f"- **Scope:** {target.scope}",
        ))
        if enumeration is not None:
            count = "" if file_count is None else f" ({file_count} files)"
            lines.append(f"- **Enumeration:** `{enumeration}`{count}")
        if stacks is not None:
            lines.append(f"- **Stacks:** {stacks}")
        lines.extend((
            f"- **Context profile:** `{context_profile.value}` (drives blocking-vs-advisory)",
            f"- **Generated:** {generated_at}",
        ))
        if preamble is not None:
            lines.extend(("", preamble))
        return "\n".join(lines)

    def _summary(self, selected: Sequence[Finding]) -> str:
        counts = {severity.value: 0 for severity in Severity}
        for finding in selected:
            counts[finding.severity.value] += 1
        cells = " · ".join(f"{severity.value}: {counts[severity.value]}" for severity in SEVERITY_ORDER)
        headline = "no findings to report" if not selected else f"{len(selected)} finding(s) to report"
        return f"## Summary\n\n**{headline}** — {cells}"

    def _findings_section(self, selected: Sequence[Finding]) -> str:
        if not selected:
            return "## Findings\n\n_None — nothing confirmed and no zero-FP deterministic findings._"
        blocks = ["## Findings"]
        blocks.extend(self._finding_block(index, finding) for index, finding in enumerate(selected, start=1))
        return "\n\n".join(blocks)

    def _finding_block(self, index: int, finding: Finding) -> str:
        verdict_note = "zero-FP L1 (promoted)" if finding.verdict is Verdict.CANDIDATE else finding.verdict.value
        lines = [
            f"### {index}. [{finding.severity.value.upper()}] {finding.dimension.value} — `{_anchor(finding)}`",
            f"- **Constraint:** `{finding.constraint_violated}`",
            f"- **Provenance:** {_render_provenance(finding.provenance)}",
            f"- **Verdict:** {verdict_note} ({finding.layer.value})",
            "",
            "**Evidence**",
            "",
            "```text",
            finding.evidence,
            "```",
            "",
            f"**Fix:** {finding.fix_suggestion if finding.fix_suggestion is not None else '—'}",
        ]
        return "\n".join(lines)

    def _coverage_section(self, coverage: Sequence[CoverageRecord]) -> str:
        if not coverage:
            return "## Scanner Coverage\n\n_No scanner-coverage evidence recorded._"
        lines = ["## Scanner Coverage", "", "| scanner | ran | files examined | gap |", "| --- | --- | --- | --- |"]
        for record in coverage:
            gap = record.gap_reason if record.gap_reason is not None else "—"
            lines.append(f"| {record.scanner} | {'yes' if record.ran else 'NO'} | {record.files_examined} | {gap} |")
        return "\n".join(lines)

    def _partition_counts(self, findings: Sequence[Finding]) -> dict[str, int]:
        """Assign every emitted finding to exactly ONE render/filter bucket (FT-1.1.3).

        The buckets are mutually exclusive and follow ``_select``'s own priority
        (confirmed, else zero-FP-promoted, else refuted, else downgraded-secret, else an
        unverified non-promoted candidate), so the footer reconciles exactly: emitted ==
        the five buckets summed. The previous footer counted ``confirmed`` and
        ``promoted`` on overlapping predicates over the raw list, so the shown numbers
        did not add up to ``emitted`` and could not be reconciled against the deduped
        ``rendered`` count — the count/render mismatch. Counted on the RAW (pre-dedup)
        list; the dedup delta is reported separately so nothing reads as an under-count.
        """
        counts = {"confirmed": 0, "promoted": 0, "refuted": 0, "downgraded_secret": 0, "unverified": 0}
        for finding in findings:
            if finding.verdict is Verdict.CONFIRMED:
                counts["confirmed"] += 1
            elif self._is_promoted_l1(finding):
                counts["promoted"] += 1
            elif finding.verdict is Verdict.REFUTED:
                counts["refuted"] += 1
            elif _is_downgraded_secret(finding):
                counts["downgraded_secret"] += 1
            else:
                counts["unverified"] += 1
        return counts

    def _footer(self, findings: Sequence[Finding], selected: Sequence[Finding]) -> str:
        counts = self._partition_counts(findings)
        rendered_raw = counts["confirmed"] + counts["promoted"]
        filtered = counts["refuted"] + counts["downgraded_secret"] + counts["unverified"]
        collapsed = rendered_raw - len(selected)
        lines = [
            "## What was filtered",
            "",
            f"- Emitted across all layers: **{len(findings)}**; rendered above (deduped by "
            f"finding-id): **{len(selected)}**. Every emitted finding is accounted below — "
            f"rendered + filtered equals emitted.",
            f"- Rendered (pre-dedup **{rendered_raw}**): L3-confirmed **{counts['confirmed']}**, "
            f"zero-FP deterministic L1 promoted without an L3 pass **{counts['promoted']}**, "
            f"duplicate finding-ids collapsed **{collapsed}**.",
            f"- Filtered / not rendered (**{filtered}**): refuted by L3 **{counts['refuted']}**, "
            f"unverified non-promoted candidates **{counts['unverified']}**, low-confidence secrets "
            f"downgraded on safe fixture/archive/artifact paths **{counts['downgraded_secret']}**.",
        ]
        return "\n".join(lines)
