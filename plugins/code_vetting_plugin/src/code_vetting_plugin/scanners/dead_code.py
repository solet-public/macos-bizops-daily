"""dead_code.py — R9-A: in-process vulture dead-code scanner (two-layer output).

Rebuilds the old bare-PATH `scan_vulture` stub (which mapped every line to LOW) as a pinned
in-process `vulture==2.16` scanner. vulture is a plugin dependency, so it is structurally present —
never a tool-absent coverage row — and the version is asserted at scan time (an upgrade shifting
the confidence semantics that ARE this scanner's contract must be a deliberate re-baseline).

TWO-LAYER output (Architect ruling §A.1, made mandatory by the probe: vulture assigns unused
functions/classes/methods/variables all 60% — the headline dead-code class lives below any clean
threshold, so a confidence cutoff can't separate it):

  * FINDING rows — only the provable classes, ceiling MEDIUM, confidence Confirmed (facts):
      - unreachable_code (100%) → MEDIUM (`dead_code:unreachable`) — code after return/raise.
      - unused import (90%) → LOW (`dead_code:unused_import`), with a HARD `__init__.py` carve-out
        (re-export API surface — the classic FP class a foreign repo owes us no `__all__` for).
  * The entire 60% family (function/class/method/variable/attribute/property) → a capped
    "candidate dead symbols" report section + persisted L2-targeting evidence (the test_reach
    pattern), NEVER findings: a library's exported public API is legitimately unused in-repo, and
    60%-confidence Finding rows on a foreign tree would be the hit-job calibrate-not-verdict forbids.

Dimension is the existing `DEAD_CODE` (kept OUT of the zero-FP registry). Per-class self emission
(ruling §A.3, tier-as-config by target class): unused imports on self are SUPPRESSED (ruff F401 is
gate authority — no double-vocabulary), unreachable on self is EMITTED (no gate covers it), and the
candidate table renders on self with vulture's `--ignore-decorators` set to the platform registry
decorators (registry-dispatched methods are structurally "unused" to static analysis). File scope:
self = `quality_surface_python()`; foreign = all `*.py`. TS/JS dead-exports (knip/ts-prune) are a
named Phase-2 gap (report methodology), NOT a phantom roster row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vulture

from ..coverage import CoverageRecord, ScannerResult
from ..models import ContextProfile, Dimension, Finding, Layer, Provenance, Severity, Verdict
from ..targets import TargetTree

VULTURE_PINNED_VERSION = "2.16"

_SCANNER = "vulture"
# Self-vet project_local-tier config (ruling §A.3): the 828 registry-dispatched methods are
# structurally "unused" to static analysis and would drown the candidate table. Matches bare AND
# parametrized decorator forms (probed). Foreign targets get no ignore set (we owe them no config).
_IGNORE_DECORATORS: tuple[str, ...] = ("@platform_process", "@service_interface_process")

# vulture `.typ` values that are provable Finding classes vs the 60% candidate family.
_TYP_IMPORT = "import"
_TYP_UNREACHABLE = "unreachable_code"
_CANDIDATE_TYPES: frozenset[str] = frozenset({"function", "class", "method", "variable", "attribute", "property"})

_RENDER_CAP = 20  # worst-N rows rendered in the report table
_PERSIST_CAP = 200  # candidates persisted to the run record as L2 targeting evidence (bounded — F1 §3)


def _relativize(root: Path, path_field: str) -> str:
    if not path_field:
        return path_field
    candidate = Path(path_field)
    if not candidate.is_absolute():
        return path_field
    try:
        return str(candidate.relative_to(root))
    except ValueError:
        return path_field


@dataclass(frozen=True, slots=True)
class DeadSymbol:
    """One 60%-confidence candidate dead symbol (targeting evidence, never a finding)."""

    file: str
    line: int
    name: str
    kind: str
    confidence: int
    dead_lines: int

    def to_dict(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line, "name": self.name, "kind": self.kind, "confidence": self.confidence, "dead_lines": self.dead_lines}


@dataclass(frozen=True, slots=True)
class DeadSymbolsReport:
    """The candidate-dead-symbols payload: counts + worst-by-dead-lines list (bounded)."""

    tool: str
    tool_version: str
    total: int
    by_kind: dict[str, int]
    candidates: tuple[DeadSymbol, ...]  # sorted by dead_lines desc, capped at _PERSIST_CAP

    def to_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "tool_version": self.tool_version,
            "total": self.total,
            "by_kind": dict(self.by_kind),
            "candidates": [symbol.to_dict() for symbol in self.candidates],
        }


def _target_files(tree: TargetTree) -> list[str]:
    """Python file scope (ruling §A.3): self = quality-surface, foreign = all *.py."""
    return list(tree.python_files() if tree.foreign else tree.quality_surface_python())


def _scavenge(tree: TargetTree, files: list[str]) -> list[Any]:
    """Run vulture in-process over the target files (ignore-decorators on self only)."""
    scanner: Any = vulture.Vulture(ignore_decorators=[] if tree.foreign else list(_IGNORE_DECORATORS))
    scanner.scavenge([str(tree.abspath(rel)) for rel in files])
    return list(scanner.get_unused_code())


def _import_finding(tree: TargetTree, rel: str, item: Any, run_id: str) -> Finding | None:
    """An unused-import Finding, or None when carved out (ruling §A.1/§A.3):
    ALWAYS skip __init__.py imports (re-export surface); on SELF skip entirely (ruff F401 owns)."""
    if Path(rel).name == "__init__.py" or not tree.foreign:
        return None
    return Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.DEAD_CODE,
        severity=Severity.LOW,
        file=rel,
        line=int(item.first_lineno),
        constraint_violated="dead_code:unused_import",
        evidence=f"unused import '{item.name}' (vulture {item.confidence}% confidence)",
        fix_suggestion="Remove the unused import (or, for a re-export, add it to __all__).",
        provenance=Provenance(source="vulture", tool_version=VULTURE_PINNED_VERSION, rule_id="unused_import"),
        context_profile=ContextProfile.PRODUCTION,
        verdict=Verdict.CONFIRMED,  # a deterministic fact (§A.1) — renders without dimension promotion
    )


def _unreachable_finding(rel: str, item: Any, run_id: str) -> Finding:
    """An unreachable-code Finding (100% — emitted on self AND foreign; no gate covers it)."""
    return Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.DEAD_CODE,
        severity=Severity.MEDIUM,
        file=rel,
        line=int(item.first_lineno),
        constraint_violated="dead_code:unreachable",
        evidence=f"statically unreachable after return/raise (vulture {item.confidence}% confidence)",
        fix_suggestion="Remove the unreachable code, or fix the control flow that skips it.",
        provenance=Provenance(source="vulture", tool_version=VULTURE_PINNED_VERSION, rule_id="unreachable"),
        context_profile=ContextProfile.PRODUCTION,
        verdict=Verdict.CONFIRMED,  # a control-flow-provable fact (§A.1) — renders without dimension promotion
    )


def _build_report(candidates: list[DeadSymbol]) -> DeadSymbolsReport:
    ordered = sorted(candidates, key=lambda symbol: (-symbol.dead_lines, symbol.file, symbol.line))
    by_kind: dict[str, int] = {}
    for symbol in candidates:
        by_kind[symbol.kind] = by_kind.get(symbol.kind, 0) + 1
    return DeadSymbolsReport(
        tool="vulture",
        tool_version=VULTURE_PINNED_VERSION,
        total=len(candidates),
        by_kind=by_kind,
        candidates=tuple(ordered[:_PERSIST_CAP]),
    )


def scan(tree: TargetTree, run_id: str) -> ScannerResult:
    """Two-layer dead-code scan: provable Finding rows + a candidate-dead-symbols payload."""
    if vulture.__version__ != VULTURE_PINNED_VERSION:
        raise RuntimeError(
            f"{_SCANNER}: vulture {vulture.__version__} != pinned {VULTURE_PINNED_VERSION} — the "
            "per-class confidence map is this scanner's contract; re-pin deliberately with a re-baseline note."
        )
    files = _target_files(tree)
    if not files:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_SCANNER, ran=False, files_examined=0,
                gap_reason="not_applicable: no python source files in scope",
            ),
        )
    findings: list[Finding] = []
    candidates: list[DeadSymbol] = []
    for item in _scavenge(tree, files):
        rel = _relativize(tree.root, str(item.filename))
        if item.typ == _TYP_UNREACHABLE:
            findings.append(_unreachable_finding(rel, item, run_id))
        elif item.typ == _TYP_IMPORT:
            finding = _import_finding(tree, rel, item, run_id)
            if finding is not None:
                findings.append(finding)
        elif item.typ in _CANDIDATE_TYPES:
            candidates.append(DeadSymbol(file=rel, line=int(item.first_lineno), name=str(item.name), kind=str(item.typ), confidence=int(item.confidence), dead_lines=int(item.size)))
    report = _build_report(candidates)
    disclosure = f"{report.total} candidate dead symbols (60% class → section + L2 evidence, not findings)" if report.total else None
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_SCANNER, ran=True, files_examined=len(files), gap_reason=disclosure),
        dead_symbols=report,
    )


# --- report section (owned here, like structural_metrics owns its section) ---


def render_candidate_dead_symbols_section(report: DeadSymbolsReport | None) -> str:
    """The ``## Candidate Dead Symbols`` block — evidence for the AI-critic layer, NOT findings."""
    if report is None:
        return "## Candidate Dead Symbols\n\n_No dead-code candidate evidence recorded._"
    if report.total == 0:
        return "## Candidate Dead Symbols\n\n_None — no unused functions/classes/methods/variables detected._"
    kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(report.by_kind.items()))
    lines = [
        "## Candidate Dead Symbols",
        "",
        f"_{report.total} candidate dead symbol(s) ({kinds}) at 60% confidence (vulture v{report.tool_version}) — "
        "evidence for the AI-critic layer, NOT findings: an exported public API is legitimately unused in-repo._",
        "",
        "| symbol | kind | location | dead lines |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(f"| `{symbol.name}` | {symbol.kind} | `{symbol.file}:{symbol.line}` | {symbol.dead_lines} |" for symbol in report.candidates[:_RENDER_CAP])
    if report.total > _RENDER_CAP:
        lines.append(f"\n_+{report.total - _RENDER_CAP} more (top {_RENDER_CAP} by dead-line volume shown; up to {_PERSIST_CAP} persisted to the run record)._")
    return "\n".join(lines)
