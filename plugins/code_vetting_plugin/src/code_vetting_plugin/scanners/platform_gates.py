"""Wrapper over the platform's own quality gates, normalized into F1 records.

Runs the gates *exactly the way Git-Controller runs them* to avoid the
well-documented divergence footgun (per-file ruff/pyright config, allowlist
scope): it invokes the canonical aggregate ``code_quality_check.py`` (ruff +
pyright --strict + the coherence trio + whole-tree-integration + service-interface
AST + return-shape + wint2 checks), plus ``sql_access_gate.py`` (which the
aggregate deliberately excludes). A gate that reports non-allowlisted findings
becomes one F1 record naming that gate. (Dead code moved to its own in-process
``scanners/dead_code.py`` scanner in R9-A — this module no longer wraps vulture.)

Dimension: aggregate gate findings emit ``dimension=code_quality`` — the F1 L1
bucket for deterministic gate/lint facts, which the report promotes as zero-FP.
The specific gate that fired is preserved in ``constraint_violated``. (The SQL
gate's raw-SQL findings keep their precise dimension.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..coverage import CoverageRecord, ScannerResult
from ..models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from ..targets import TargetTree
from ..toolrun import run


@dataclass(frozen=True, slots=True)
class _GatePaths:
    """The canonical gate-script + venv paths, anchored on the scanned tree root."""

    aggregate: Path
    sql_gate: Path
    sql_allowlist: Path
    venv_python: Path


def _gate_paths(root: Path) -> _GatePaths:
    """Resolve the gate-script + venv paths under the SCANNED tree ``root`` (deploy-invariant).

    Anchored on ``root`` (the git worktree the plugin located via ``APP_HOME``), NOT
    ``__file__``. A materialized blue-green release copy ships ``ananta/`` + ``plugins/``
    but NOT top-level ``quality_gates/``, so a ``__file__`` anchor resolves the release
    copy and fails to find the gate scripts (observed live 2026-07-20: code_quality +
    sql_access gapped out on the deployed color while only vulture, a bare-PATH tool, ran).
    ``root`` is the SAME anchor as the scanned target (``tree.root``); the worktree HAS
    ``quality_gates/`` + ``.venv``, so this resolves correctly from both the direct-launch
    and the release-copy contexts. A seeded / foreign solet with no ``quality_gates/``
    gaps out HONESTLY — these wrappers are platform-self-vet-specific (Phase-2 foreign targets
    use language-native gates).
    """
    gates_dir = root / "quality_gates"
    return _GatePaths(
        aggregate=gates_dir / "code_quality_check.py",
        sql_gate=gates_dir / "sql_access_gate.py",
        sql_allowlist=gates_dir / "sql_access_allowlist.txt",
        venv_python=root / ".venv" / "bin" / "python3",
    )


_USAGE_ERROR_EXIT = 64
_SOLET_ENV = {"SOLET_NAME": "self"}
_BLOCKING_MARKER = "❌ BLOCKING:"


def _blocking_blocks(output: str) -> list[tuple[str, list[str]]]:
    """Split aggregate output into (blocking-message, evidence-lines) blocks."""
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in output.splitlines():
        if _BLOCKING_MARKER in line:
            if current is not None:
                blocks.append(current)
            current = (line.split(_BLOCKING_MARKER, 1)[1].strip(), [])
        elif current is not None:
            if line.startswith("💡") or line.startswith("✅"):
                blocks.append(current)
                current = None
            elif line.strip():
                current[1].append(line.strip())
    if current is not None:
        blocks.append(current)
    return blocks


def scan_code_quality(tree: TargetTree, run_id: str) -> ScannerResult:
    """Run the platform quality aggregate and normalize failures to F1 records."""
    paths = _gate_paths(tree.root)
    if not paths.aggregate.exists():
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner="code_quality_check", ran=False, files_examined=0, gap_reason="aggregate gate script not found"
            ),
        )
    outcome = run(
        [str(paths.venv_python), str(paths.aggregate)],
        cwd=str(tree.root),
        env_overrides=_SOLET_ENV,
        timeout_s=600,
    )
    if outcome.returncode == _USAGE_ERROR_EXIT:
        raise RuntimeError(f"code_quality_check usage error: {outcome.stdout[-500:]}")
    examined = len(tree.quality_surface_python())
    if outcome.returncode == 0:
        return ScannerResult(
            findings=[], coverage=CoverageRecord(scanner="code_quality_check", ran=True, files_examined=examined)
        )
    blocks = _blocking_blocks(outcome.stdout)
    findings: list[Finding] = []
    for message, evidence_lines in blocks:
        evidence = "; ".join(evidence_lines[:8]) or message
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.CODE_QUALITY,
                severity=Severity.HIGH,
                file="quality_gates/code_quality_check.py",
                line=None,
                constraint_violated=f"gate:{message.split(' gate')[0].strip() or 'code_quality'}",
                evidence=f"{message} — {evidence}",
                fix_suggestion="Fix the non-allowlisted gate finding; allowlisting is not a fix.",
                provenance=Provenance(source="gate:code_quality_check"),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    if not findings:
        # Non-zero exit with no parseable blocking block (e.g. ruff/pyright count-only path).
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.CODE_QUALITY,
                severity=Severity.HIGH,
                file="quality_gates/code_quality_check.py",
                line=None,
                constraint_violated="gate:code_quality_check",
                evidence=f"aggregate exited {outcome.returncode}: {outcome.stdout[-400:].strip()}",
                fix_suggestion="Run code_quality_check.py locally and clear the reported findings.",
                provenance=Provenance(source="gate:code_quality_check"),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return ScannerResult(
        findings=findings, coverage=CoverageRecord(scanner="code_quality_check", ran=True, files_examined=examined)
    )


def scan_sql_access(tree: TargetTree, run_id: str) -> ScannerResult:
    """Run the SQL-access lockdown gate (excluded from the aggregate)."""
    paths = _gate_paths(tree.root)
    if not paths.sql_gate.exists():
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(scanner="sql_access", ran=False, files_examined=0, gap_reason="sql gate not found"),
        )
    outcome = run(
        [str(paths.venv_python), str(paths.sql_gate), "--allowlist", str(paths.sql_allowlist)],
        cwd=str(tree.root),
        env_overrides=_SOLET_ENV,
    )
    examined = len(tree.quality_surface_python())
    if outcome.returncode == 0:
        return ScannerResult(
            findings=[], coverage=CoverageRecord(scanner="sql_access", ran=True, files_examined=examined)
        )
    # Keep the non-allowlisted findings + summary; drop the allowlisted-line noise.
    relevant = "\n".join(line for line in outcome.stdout.splitlines() if "[allowlisted]" not in line).strip()
    evidence = relevant[-800:] or outcome.stderr.strip()[-400:]
    finding = Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.SECURITY,
        severity=Severity.HIGH,
        file="quality_gates/sql_access_gate.py",
        line=None,
        constraint_violated="RB-STATE:raw_sql",
        evidence=f"sql_access gate reported non-allowlisted findings: {evidence}",
        fix_suggestion="Route DB access through the state interface; no raw SQL / hand-DDL / execute_sql.",
        provenance=Provenance(source="gate:sql_access", rule_id="RB-STATE"),
        context_profile=ContextProfile.PRODUCTION,
    )
    return ScannerResult(
        findings=[finding], coverage=CoverageRecord(scanner="sql_access", ran=True, files_examined=examined)
    )
