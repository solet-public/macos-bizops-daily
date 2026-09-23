"""L1 orchestration — run every deterministic scanner over a target tree.

Aggregates each scanner's :class:`ScannerResult` into one findings list plus the
per-scanner coverage evidence. No defensive ``try/except`` wraps the scanners:
expected conditions (a tool absent, an offline registry) are already converted to
coverage gaps inside each scanner, so a raised exception here means a real defect
that must fail loud (RB-FASTFAIL), not be swallowed into an empty result.

This is the L1 half of the pipeline; Stream O's orchestrator drives L1→L2→L3 and
owns the ``vetting_runs`` metrics row. Here we just produce the L1 records.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .coverage import CoverageRecord, ScannerResult
from .models import Finding
from .scanners import (
    dead_code,
    deps,
    duplication,
    hidden_unicode,
    orphan,
    patterns,
    platform_gates,
    prior_pass,
    python_type_check,
    rulebook_sync,
    sast,
    secrets,
    structural_metrics,
    ts_toolchain,
)
from .scanners.dead_code import DeadSymbolsReport
from .scanners.structural_metrics import StructuralMetricsReport
from .stacks import Stack, detect_stacks
from .targets import TargetTree


@dataclass(frozen=True, slots=True)
class L1ReportData:
    """Report-supplementary L1 payloads — each scanner's optional report-section + trend-persistence
    data, collected from the ScannerResults so ``run_all``'s return arity stays fixed as new payloads
    land (R9-A). None fields = that scanner did not run / produced no payload."""

    structural_metrics: StructuralMetricsReport | None = None
    dead_symbols: DeadSymbolsReport | None = None

# Heterogeneous roster: most scanners take ``(tree, run_id)``; the two target-toolchain
# scanners (tsc/eslint) also take the ``execute_target_toolchain`` opt-in keyword (R7-4).
# ``Callable[..., ScannerResult]`` is the one explicit type that admits both call shapes —
# ``run_all`` selects the shape per ``ScannerSpec.executes_target_code``.
Scanner = Callable[..., ScannerResult]


class Applicability(StrEnum):
    """When a scanner runs on a given target (R7-1, generalizing FT-1's self_only set)."""

    UNIVERSAL = "universal"
    """Runs on every target — honestly examines whatever tree it is handed (0 files is honest)."""
    SELF_ONLY = "self_only"
    """platform-canon / gate-wrapper scanners — on a FOREIGN target: skip execution, ledger a not_applicable (ruling C)."""
    STACK = "stack"
    """Runs iff one of its declared language stacks is present in the target (R7-1 stack gate)."""


@dataclass(frozen=True, slots=True)
class ScannerSpec:
    """One registered scanner + its applicability gate. Replaces the (name, fn) tuple + SELF_ONLY_SCANNERS."""

    name: str
    run: Scanner
    applicability: Applicability
    stacks: frozenset[Stack] = field(default_factory=frozenset)
    executes_target_code: bool = False
    """R7-4: this scanner runs the TARGET's OWN toolchain (tsc/eslint) on the scan host — so
    ``run_all`` passes it the ``execute_target_toolchain`` opt-in, and it appears in the §8
    read-only checklist. False for every scanner that only reads the target's source text."""

    def __post_init__(self) -> None:
        # R2: `stacks` is non-empty IFF applicability is STACK — a STACK scanner must declare which
        # stacks trigger it, and a non-STACK scanner must not carry stray stacks that never gate.
        if (self.applicability is Applicability.STACK) != bool(self.stacks):
            raise ValueError(
                f"ScannerSpec {self.name!r}: `stacks` must be non-empty iff applicability is STACK (R2)"
            )


# Ordered registry — every deterministic scanner the L1 lane wraps, with its applicability gate.
# UNIVERSAL scanners self-scope internally (a Python-only tool on a TS tree honestly examines 0
# files); SELF_ONLY are the platform-canon gate wrappers (skip+ledger on foreign, ruling C). The TS
# STACK scanners (tsc/eslint) are added by R7-4 — the SCANNERS tuple is the single extension point.
SCANNERS: tuple[ScannerSpec, ...] = (
    ScannerSpec("gitleaks", secrets.scan_gitleaks, Applicability.UNIVERSAL),
    ScannerSpec("trufflehog", secrets.scan_trufflehog, Applicability.UNIVERSAL),
    ScannerSpec("bandit", sast.scan_bandit, Applicability.UNIVERSAL),
    ScannerSpec("semgrep", sast.scan_semgrep, Applicability.UNIVERSAL),
    ScannerSpec("tsc", ts_toolchain.scan_tsc, Applicability.STACK, frozenset({Stack.TYPESCRIPT}), executes_target_code=True),
    ScannerSpec("eslint", ts_toolchain.scan_eslint, Applicability.STACK, frozenset({Stack.TYPESCRIPT, Stack.JAVASCRIPT}), executes_target_code=True),
    ScannerSpec("python_type_check", python_type_check.scan, Applicability.STACK, frozenset({Stack.PYTHON}), executes_target_code=True),
    ScannerSpec("pip_audit", deps.scan_pip_audit, Applicability.UNIVERSAL),
    ScannerSpec("osv_scanner", deps.scan_osv, Applicability.UNIVERSAL),
    ScannerSpec("license_sweep", deps.scan_licenses, Applicability.UNIVERSAL),
    ScannerSpec("code_quality", platform_gates.scan_code_quality, Applicability.SELF_ONLY),
    ScannerSpec("sql_access", platform_gates.scan_sql_access, Applicability.SELF_ONLY),
    ScannerSpec("vulture", dead_code.scan, Applicability.UNIVERSAL),
    ScannerSpec("structural_metrics", structural_metrics.scan, Applicability.UNIVERSAL),
    ScannerSpec("rg_battery", patterns.scan, Applicability.UNIVERSAL),
    ScannerSpec("hidden_unicode", hidden_unicode.scan, Applicability.UNIVERSAL),
    ScannerSpec("duplication", duplication.scan, Applicability.UNIVERSAL),
    ScannerSpec("orphan_kb", orphan.scan, Applicability.SELF_ONLY),
    ScannerSpec("prior_pass", prior_pass.scan, Applicability.SELF_ONLY),
    ScannerSpec("rulebook_sync", rulebook_sync.scan, Applicability.SELF_ONLY),
)

# The honest-coverage reason strings (ruling C). `not_applicable:` is the single discriminator that
# separates a by-design skip from a real tool-missing gap in the Scanner Coverage table.
_NOT_APPLICABLE_SELF = "not_applicable: platform-self scanner on foreign target"
_NOT_APPLICABLE_STACK = "not_applicable: no {stacks} sources in target"


def _not_applicable(name: str, reason: str) -> CoverageRecord:
    return CoverageRecord(scanner=name, ran=False, files_examined=0, gap_reason=reason)


def run_all(
    tree: TargetTree, run_id: str, *, execute_target_toolchain: bool = False
) -> tuple[list[Finding], list[CoverageRecord], L1ReportData]:
    """Run every registered scanner per its applicability gate; return findings + coverage +
    the report-supplementary payloads (``L1ReportData``: structural metrics + candidate dead symbols).

    Roster order, per spec: a SELF_ONLY scanner on a FOREIGN target skips execution and
    ledgers a ``not_applicable`` record (ruling C); a STACK scanner whose declared stacks
    do not intersect the target's detected stacks skips + ledgers likewise (R2 intersection
    semantics); everything else executes. The roster NEVER forks — Y (``scanners_total``)
    stays the full roster whether a scanner ran or was skipped.

    ``execute_target_toolchain`` (R7-4, default-off) is the caller-owned opt-in threaded to the
    ``executes_target_code`` scanners (tsc/eslint) only — it gates ON-HOST execution of
    target-controlled code; unset, those scanners record an opt-in coverage gap instead of running.

    The third element bundles the scanners' report-supplementary payloads (the structural-metrics
    distribution + the candidate-dead-symbols list) for the report sections + ``vetting_runs`` trend
    persistence; a payload is None if its scanner did not run or produced nothing (R8-1/R9-A).
    """
    stacks = detect_stacks(tree)
    findings: list[Finding] = []
    coverage: list[CoverageRecord] = []
    structural: StructuralMetricsReport | None = None
    dead_symbols: DeadSymbolsReport | None = None
    for spec in SCANNERS:
        if spec.applicability is Applicability.SELF_ONLY and tree.foreign:
            coverage.append(_not_applicable(spec.name, _NOT_APPLICABLE_SELF))
            continue
        if spec.applicability is Applicability.STACK and not (spec.stacks & stacks):
            label = "/".join(sorted(stack.value for stack in spec.stacks))
            coverage.append(_not_applicable(spec.name, _NOT_APPLICABLE_STACK.format(stacks=label)))
            continue
        result = (
            spec.run(tree, run_id, execute_target_toolchain=execute_target_toolchain)
            if spec.executes_target_code
            else spec.run(tree, run_id)
        )
        findings.extend(result.findings)
        coverage.append(result.coverage)
        if result.structural_metrics is not None:
            structural = result.structural_metrics
        if result.dead_symbols is not None:
            dead_symbols = result.dead_symbols
    return findings, coverage, L1ReportData(structural_metrics=structural, dead_symbols=dead_symbols)
