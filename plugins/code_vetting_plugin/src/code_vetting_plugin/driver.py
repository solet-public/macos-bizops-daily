"""driver.py — the vetting orchestrator skeleton: scope → L1 → L2 → L3 → report.

The driver wires the four layers into one run and hands the result to the report
renderer and the metrics writer. The layers are injected behind narrow Protocols
(:class:`L1Scanner`, :class:`L2Critic`, :class:`L3Verifier`) so this skeleton is
complete now and the concrete scanners/critics/verifier land in Wave 2 without
touching the orchestration. The flow honors the F1 layer contract: L1 emits
deterministic candidates, the L2 critics fan out (run concurrently), L3 re-stamps
the L2 candidates confirmed/refuted, and the report renders confirmed + zero-FP
L1 while the metrics row counts all three layers.

Timestamps are supplied by an injected ``clock`` (F1 §3: the caller stamps
``started``/``finished``), which also keeps a run deterministic under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .metrics import MetricsWriter
from .models import ContextProfile, Finding, Layer
from .report import ReportRenderer, is_zero_fp_promoted
from .run_record import AllowlistDelta, CoverageRecord, RunMetrics, RunTarget, build_run_metrics
from .runner import L1ReportData

# Returns an ISO-8601 timestamp string; injected so runs are deterministic.
Clock = Callable[[], str]


@dataclass(frozen=True, slots=True)
class L1Output:
    """What the deterministic layer hands the driver: findings + coverage + report-supplementary
    payloads (structural metrics + candidate dead symbols), bundled in ``L1ReportData``."""

    findings: list[Finding]
    coverage: list[CoverageRecord]
    report_data: L1ReportData = field(default_factory=L1ReportData)


class L1Scanner(Protocol):
    """Deterministic layer — wraps external tools + platform gates into F1 findings."""

    async def scan(self, run_id: str, target: RunTarget) -> L1Output:
        """Scan the target, returning candidate L1 findings (run-scoped) plus coverage."""
        ...


class L2Critic(Protocol):
    """One single-lens critic — emits F1 candidates naming the rule each breaks."""

    async def review(self, run_id: str, target: RunTarget) -> list[Finding]:
        """Review the target through one lens, returning run-scoped L2 candidates."""
        ...


class L3Verifier(Protocol):
    """Adversarial verifier — re-stamps L2 candidates confirmed/refuted (creates none)."""

    async def verify(self, candidates: Sequence[Finding]) -> list[Finding]:
        """Return the candidates re-stamped with an L3 verdict (id preserved)."""
        ...


@dataclass(frozen=True, slots=True)
class VettingResult:
    """One run's rendered report plus its metrics row and full finding trail."""

    report: str
    metrics: RunMetrics
    findings: list[Finding]


@dataclass(frozen=True, slots=True)
class VettingDriver:
    """Runs one vetting pass end to end (scope → L1 → L2 → L3 → report + metrics)."""

    l1: L1Scanner
    l2_critics: Sequence[L2Critic]
    l3: L3Verifier
    renderer: ReportRenderer
    metrics_writer: MetricsWriter
    clock: Clock
    context_profile: ContextProfile
    # Which inference engine reviewed/refuted this run — recorded on the metrics
    # row (F1 §3). Defaults to the no-inference heuristic path; B3/W3-C sets the
    # real substrate (local_inference | subscription) from the selector.
    substrate: str = "heuristic"

    async def run(
        self,
        *,
        run_id: str,
        target: RunTarget,
        allowlist_delta: AllowlistDelta,
        preamble: str | None = None,
    ) -> VettingResult:
        """Execute the full pipeline for one target and persist its metrics row."""
        started = self.clock()
        l1_output = await self.l1.scan(run_id, target)
        l2_candidates = await self._run_critics(run_id, target)
        promoted, to_verify = self._route([*l1_output.findings, *l2_candidates])
        verified = await self.l3.verify(to_verify)
        findings = [*promoted, *verified]
        finished = self.clock()

        structural = l1_output.report_data.structural_metrics
        dead = l1_output.report_data.dead_symbols
        # Render the report BEFORE the metrics row so the row can carry it (C3a: the get_vetting_run
        # read-verb serves the report by run_id — the joseki's L2/L3 steps read it without re-running).
        report = self.renderer.render(
            run_id=run_id,
            target=target,
            context_profile=self.context_profile,
            generated_at=finished,
            findings=findings,
            coverage=l1_output.coverage,
            preamble=preamble,
            structural_metrics=structural,
            dead_symbols=dead,
        )
        metrics = build_run_metrics(
            run_id=run_id,
            target=target,
            started=started,
            finished=finished,
            substrate=self.substrate,
            layers_run=self._layers_run(),
            findings=findings,
            coverage=l1_output.coverage,
            allowlist_delta=allowlist_delta,
            structural_metrics=structural.to_dict() if structural is not None else None,
            dead_symbols=dead.to_dict() if dead is not None else None,
            report=report,
        )
        await self.metrics_writer.persist(metrics)
        return VettingResult(report=report, metrics=metrics, findings=findings)

    async def _run_critics(self, run_id: str, target: RunTarget) -> list[Finding]:
        """Fan the L2 critics out concurrently and flatten their candidates."""
        reviews = await asyncio.gather(*(critic.review(run_id, target) for critic in self.l2_critics))
        return [finding for review in reviews for finding in review]

    def _route(self, candidates: Sequence[Finding]) -> tuple[list[Finding], list[Finding]]:
        """Split candidates into zero-FP-promoted (shown as-is) and to-verify (→ L3).

        Zero-FP deterministic facts (F1 §1) bypass L3; everything else — the L1
        judgment dimensions (dup/orphan/identity_leak/network_bind) and every L2
        candidate — is routed to the verifier. Uses the renderer's promotion
        policy so routing and rendering share one definition of "bypasses L3".
        """
        dimensions = self.renderer.zero_fp_dimensions
        promoted = [finding for finding in candidates if is_zero_fp_promoted(finding, dimensions)]
        to_verify = [finding for finding in candidates if not is_zero_fp_promoted(finding, dimensions)]
        return promoted, to_verify

    def _layers_run(self) -> list[Layer]:
        """Which layers actually participated (L2 only when critics are wired)."""
        layers = [Layer.L1_DETERMINISTIC]
        if self.l2_critics:
            layers.append(Layer.L2_CRITIC)
        layers.append(Layer.L3_VERIFIED)
        return layers
