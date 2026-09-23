"""samples.py — Wave-1 synthetic layers + in-memory state, for the dogfood run.

Until the real L1 scanners (Stream L1), L2 critics (Stream L2), and L3 verifier
(Stream L3) wire in at Wave 2, these stand-ins let the Stream-O orchestrator run
end to end and render a representative report. The findings are SYNTHETIC and
illustrative — they name plausible platform-shaped violations against real rulebook
ids (F2) so the report + metrics *shapes* are exercised; they are not real scan
results, and the ``example_*`` paths are placeholders.

``InMemoryStateWriter`` is the Wave-1 binding of the ``StateWriter`` seam: the
same own-namespace verbs (upsert / query_ordered / delete) backed by a dict, so
the metrics writer runs unchanged against it. Wave 2 swaps it for the live
``service_interface::state_service`` adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .driver import L1Output
from .models import ContextProfile, Dimension, Finding, Layer, Provenance, Severity, Verdict
from .run_record import CoverageRecord, RunTarget

_PROFILE = ContextProfile.PRODUCTION


@dataclass(slots=True)
class InMemoryStateWriter:
    """Wave-1 in-memory binding of the ``StateWriter`` seam (own-namespace only)."""

    rows: dict[str, dict[str, dict[str, object]]] = field(default_factory=dict)

    async def upsert_state(
        self,
        *,
        namespace: str,
        data: Mapping[str, object],
        conflict_columns: Sequence[str],
    ) -> None:
        key = "|".join(str(data[column]) for column in conflict_columns)
        self.rows.setdefault(namespace, {})[key] = dict(data)

    async def query_ordered(
        self,
        *,
        namespace: str,
        order_by: Sequence[tuple[str, str]],
        limit: int,
    ) -> list[Mapping[str, object]]:
        stored = list(self.rows.get(namespace, {}).values())
        direction = order_by[0][1] if order_by else "asc"
        ordered = sorted(
            stored,
            key=lambda row: tuple(str(row[column]) for column, _ in order_by),
            reverse=direction == "desc",
        )
        return [dict(row) for row in ordered[:limit]]

    async def delete_records(
        self,
        *,
        namespace: str,
        filters: Mapping[str, object],
    ) -> int:
        bucket = self.rows.get(namespace, {})
        doomed = [key for key, row in bucket.items() if all(row.get(column) == value for column, value in filters.items())]
        for key in doomed:
            del bucket[key]
        return len(doomed)

    def count(self, namespace: str) -> int:
        """Row count in a namespace — used by the retention demo assertion."""
        return len(self.rows.get(namespace, {}))


def _l1(
    run_id: str,
    dimension: Dimension,
    severity: Severity,
    file: str,
    line: int | None,
    constraint: str,
    evidence: str,
    fix: str,
    source: str,
    tool_version: str,
) -> Finding:
    return Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=dimension,
        severity=severity,
        file=file,
        line=line,
        constraint_violated=constraint,
        evidence=evidence,
        provenance=Provenance(source=source, tool_version=tool_version),
        context_profile=_PROFILE,
        fix_suggestion=fix,
    )


def _l2(
    run_id: str,
    dimension: Dimension,
    severity: Severity,
    file: str,
    line: int | None,
    constraint: str,
    evidence: str,
    fix: str,
    lens: str,
    rule_id: str | None = None,
) -> Finding:
    return Finding.build(
        run_id=run_id,
        layer=Layer.L2_CRITIC,
        dimension=dimension,
        severity=severity,
        file=file,
        line=line,
        constraint_violated=constraint,
        evidence=evidence,
        provenance=Provenance(source=f"critic:{lens}", critic_lens=lens, rule_id=rule_id),
        context_profile=_PROFILE,
        fix_suggestion=fix,
    )


class SampleL1Scanner:
    """Synthetic deterministic layer: a mix of zero-FP-promoted + not-promoted findings."""

    async def scan(self, run_id: str, target: RunTarget) -> L1Output:
        _ = target  # stub: the real scanners draw their file view from the target inventory
        findings = [
            _l1(
                run_id, Dimension.HIDDEN_UNICODE, Severity.HIGH,
                "knowledge_bases/ananta_platform/example_article.md", 12, "RB-GUIDELINES",
                "U+200B ZERO WIDTH SPACE embedded mid-sentence in a shipping KB article",
                "Strip the zero-width codepoint; re-run the hidden-unicode battery to confirm.",
                "gate:hidden_unicode", "1.0",
            ),
            _l1(
                run_id, Dimension.LICENSE, Severity.MEDIUM,
                "plugins/example_widget_plugin/pyproject.toml", 8, "RB-LICENSE",
                "license = { file = \"LICENSE\" } — a license-files table, not a bare SPDX string",
                "Replace with the bare SPDX string `license = \"Apache-2.0\"` (RB-LICENSE).",
                "gate:license", "1.0",
            ),
            _l1(
                run_id, Dimension.COMPLEXITY, Severity.MEDIUM,
                "plugins/example_widget_plugin/src/example_widget_plugin/plugin.py", 214, "radon_cc",
                "_dispatch_widget ranked C (16) — not in quality_gates/radon_cc_allowlist.txt",
                "Extract the branch ladder into small helpers to land at A/B, or triage as tracked debt.",
                "gate:radon_cc", "6.0.1",
            ),
            _l1(
                run_id, Dimension.ORPHAN, Severity.LOW,
                "knowledge_bases/ananta_platform/example_unreferenced.md", None, "RB-GUIDELINES",
                "KB article with no registry/process reference — possibly dead, possibly loaded dynamically",
                "Confirm whether anything ingests it; if truly orphaned, remove it.",
                "gate:orphan", "1.0",
            ),
        ]
        coverage = [
            CoverageRecord("gitleaks", ran=True, files_examined=1423),
            CoverageRecord("semgrep", ran=True, files_examined=512),
            CoverageRecord("bandit", ran=True, files_examined=512),
            CoverageRecord("pip-audit", ran=True, files_examined=6),
            CoverageRecord("gates(ruff/pyright/radon/god-class)", ran=True, files_examined=512),
            CoverageRecord("trufflehog", ran=False, files_examined=0, gap_reason="tool not installed on this host"),
        ]
        return L1Output(findings=findings, coverage=coverage)


@dataclass(frozen=True, slots=True)
class SampleL2Critic:
    """Synthetic single-lens critic — emits one candidate for its lens."""

    finding_builder: SampleCritic

    async def review(self, run_id: str, target: RunTarget) -> list[Finding]:
        _ = target  # stub: the real critic reads the target inventory + rulebook
        return [self.finding_builder.build(run_id)]


@dataclass(frozen=True, slots=True)
class SampleCritic:
    """A single synthetic L2 finding recipe (keeps SampleL2Critic uniform)."""

    dimension: Dimension
    severity: Severity
    file: str
    line: int | None
    constraint: str
    evidence: str
    fix: str
    lens: str
    rule_id: str | None = None

    def build(self, run_id: str) -> Finding:
        return _l2(
            run_id, self.dimension, self.severity, self.file, self.line,
            self.constraint, self.evidence, self.fix, self.lens, self.rule_id,
        )


_SAMPLE_CRITICS: tuple[SampleCritic, ...] = (
    SampleCritic(
        Dimension.ARCHITECTURE, Severity.BLOCKER,
        "plugins/example_widget_plugin/src/example_widget_plugin/store.py", 42, "RB-STATE",
        "execute_sql(\"SELECT * FROM widgets WHERE owner = %s\", [owner]) — a NEW raw-SQL surface",
        "Route through the state interface (query_state on the owned namespace); no execute_sql.",
        "architecture", "RB-STATE",
    ),
    SampleCritic(
        Dimension.CORRECTNESS, Severity.HIGH,
        "plugins/example_widget_plugin/src/example_widget_plugin/parser.py", 88, "universal:correctness/off-by-one",
        "for i in range(len(items) + 1): items[i] — indexes one past the end on the final iteration",
        "Iterate range(len(items)) (or `for item in items`); the +1 walks off the end.",
        "correctness",
    ),
    SampleCritic(
        Dimension.SECURITY, Severity.HIGH,
        "plugins/example_widget_plugin/src/example_widget_plugin/exec.py", 31, "universal:security/CWE-78",
        "subprocess.run(f\"convert {user_path} out.png\", shell=True) — shell=True on an interpolated path",
        "Pass an argv list with shell=False; never interpolate user input into a shell string.",
        "security",
    ),
    SampleCritic(
        Dimension.AI_SLOP, Severity.LOW,
        "plugins/example_widget_plugin/src/example_widget_plugin/plugin.py", 5, "universal:ai_slop/stale-comment",
        "# NOTE: falls back to the legacy renderer when unset — but the fallback branch was deleted",
        "Delete the stale comment (or restore the behavior it describes).",
        "ai_slop",
    ),
)


def sample_critics() -> list[SampleL2Critic]:
    """The synthetic L2 critic panel (architecture / correctness / security / ai_slop)."""
    return [SampleL2Critic(critic) for critic in _SAMPLE_CRITICS]


class SampleL3Verifier:
    """Synthetic verifier: confirms every candidate except the ai_slop stale-comment.

    Stands in for Stream L3's real adversarial, perspective-diverse refutation. It
    refutes the stale-comment candidate (the comment is in fact accurate — a
    DO-NOT-FLAG-adjacent false positive), yielding a survival_rate below 1.0 so the
    metrics trail shows a non-trivial precision proxy.
    """

    async def verify(self, candidates: Sequence[Finding]) -> list[Finding]:
        verified: list[Finding] = []
        for candidate in candidates:
            verdict = Verdict.REFUTED if candidate.dimension is Dimension.AI_SLOP else Verdict.CONFIRMED
            verified.append(candidate.with_verdict(verdict))
        return verified
