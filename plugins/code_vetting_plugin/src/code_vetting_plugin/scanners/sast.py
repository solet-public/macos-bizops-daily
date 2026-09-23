"""Static application security testing: bandit + semgrep.

Scoped to the platform quality surface (RB-SCOPE): these run over shipping
source, not operator tooling. bandit is offline and deterministic; semgrep's
``--config auto`` needs the rule registry, so a fetch failure is recorded as a
coverage gap rather than a masked pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..coverage import CoverageRecord, ScannerResult
from ..models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from ..stacks import Stack, detect_stacks
from ..targets import WALK_EXCLUDE_DIRS, TargetTree
from ..toolrun import run, tool_available, tool_unavailable_reason, tool_version

_BANDIT = "bandit"
_SEMGREP = "semgrep"

# R7-2: the semgrep registry pack per detected stack — additive (packs union across the
# detected stacks). A named pack (not `--config auto`, which requires metrics-on/phone-home).
# The mapping is the extension point: framework packs (p/react, …) are a later curation call.
_SEMGREP_PACKS: dict[Stack, tuple[str, ...]] = {
    Stack.PYTHON: ("p/python",),
    Stack.TYPESCRIPT: ("p/typescript",),
    Stack.JAVASCRIPT: ("p/javascript",),
}
# The self-vet quality-surface top-level dirs semgrep scans (unchanged from FT-1.1).
_SELF_SCAN_DIRS: frozenset[str] = frozenset({"ananta", "plugins", "quality_gates"})

_BANDIT_SEVERITY: dict[str, Severity] = {
    "HIGH": Severity.HIGH,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
}
_SEMGREP_SEVERITY: dict[str, Severity] = {
    "ERROR": Severity.HIGH,
    "WARNING": Severity.MEDIUM,
    "INFO": Severity.LOW,
}


def _as_str(value: Any) -> str:  # noqa: ANN401 — narrows untyped tool JSON
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int | None:  # noqa: ANN401 — narrows untyped tool JSON
    return value if isinstance(value, int) else None


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


def _bandit_findings(payload: dict[str, Any], root: Path, run_id: str, version: str | None) -> list[Finding]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError("bandit JSON missing a results array")
    findings: list[Finding] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        # Drop bandit's LOW/informational tier (B101 assert-used, B404/B603/B607
        # subprocess-usage, etc.) — it floods with policy-fine patterns. Real SAST
        # signal is MEDIUM+ (injection, deserialization); secrets are covered by
        # gitleaks + the rg secret-shape battery, not bandit's low-confidence B105.
        if _as_str(entry.get("issue_severity")) == "LOW":
            continue
        test_id = _as_str(entry.get("test_id")) or "unknown"
        severity = _BANDIT_SEVERITY.get(_as_str(entry.get("issue_severity")), Severity.LOW)
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.SECURITY,
                severity=severity,
                file=_relativize(root, _as_str(entry.get("filename"))),
                line=_as_int(entry.get("line_number")),
                constraint_violated=f"bandit:{test_id}",
                evidence=_as_str(entry.get("issue_text")),
                fix_suggestion=None,
                provenance=Provenance(source=_BANDIT, tool_version=version, rule_id=test_id),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return findings


def scan_bandit(tree: TargetTree, run_id: str) -> ScannerResult:
    # RIDER-1 (R9-D shape): a FOREIGN target runs over all *.py (foreign python trees previously
    # gapped as 'no quality-surface python' while semgrep ran); a self-vet stays quality-surface-scoped.
    # Test files are excluded either way — asserts + fixtures there are policy-fine, not security signal.
    scoped = tree.python_files() if tree.foreign else tree.quality_surface_python()
    targets = tuple(p for p in scoped if "/tests/" not in p)
    if not tool_available(_BANDIT):
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_BANDIT, ran=False, files_examined=0, gap_reason=tool_unavailable_reason(_BANDIT)
            ),
        )
    if not targets:
        # FT-1.1 defect 2: no python source in the target quality-surface means bandit
        # examined nothing — an honest coverage GAP, not a clean pass. ran=True/0 reads
        # as "ran clean". Never triggers on a self-vet (our tree always carries python).
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_BANDIT,
                ran=False,
                files_examined=0,
                gap_reason="not_applicable: no python source files in target quality-surface — bandit examined 0 files",
            ),
        )
    version = tool_version(_BANDIT)
    abs_targets = [str(tree.abspath(rel)) for rel in targets]
    outcome = run([_BANDIT, "-f", "json", "-q", *abs_targets], cwd=str(tree.root))
    parsed: Any = json.loads(outcome.stdout or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("bandit did not emit a JSON object")
    findings = _bandit_findings(parsed, tree.root, run_id, version)
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_BANDIT, ran=True, files_examined=len(targets)),
    )


def _semgrep_findings(payload: dict[str, Any], root: Path, run_id: str, version: str | None) -> list[Finding]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError("semgrep JSON missing a results array")
    findings: list[Finding] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        check_id = _as_str(entry.get("check_id")) or "unknown"
        extra = entry.get("extra")
        message = ""
        severity = Severity.MEDIUM
        if isinstance(extra, dict):
            message = _as_str(extra.get("message"))
            severity = _SEMGREP_SEVERITY.get(_as_str(extra.get("severity")), Severity.MEDIUM)
        start = entry.get("start")
        line = _as_int(start.get("line")) if isinstance(start, dict) else None
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.SECURITY,
                severity=severity,
                file=_relativize(root, _as_str(entry.get("path"))),
                line=line,
                constraint_violated=f"semgrep:{check_id}",
                evidence=message,
                fix_suggestion=None,
                provenance=Provenance(source=_SEMGREP, tool_version=version, rule_id=check_id),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return findings


def _semgrep_packs(stacks: frozenset[Stack]) -> tuple[str, ...]:
    """Union of registry packs for the detected stacks (additive, sorted-deterministic)."""
    packs: set[str] = set()
    for stack in stacks:
        packs.update(_SEMGREP_PACKS.get(stack, ()))
    return tuple(sorted(packs))


def _semgrep_scan_target_args(tree: TargetTree) -> list[str]:
    """The scan roots + excludes. Self-vet → the quality-surface top-level dirs (unchanged).

    A FOREIGN target → the whole tree (``.``, cwd-relative) with the curated walk-excludes:
    semgrep cannot rely on the target's gitignore on a tarball, so materialized junk
    (``node_modules`` on disk) is excluded explicitly — closing the walk-mode residual the
    same way the osv per-lockfile enumeration does (R7-2/R7-3).
    """
    if not tree.foreign:
        return sorted({rel.split("/", 1)[0] for rel in tree.quality_surface()} & _SELF_SCAN_DIRS)
    args: list[str] = []
    for exclude in sorted(WALK_EXCLUDE_DIRS):
        args.extend(("--exclude", exclude))
    args.append(".")
    return args


def _paths_scanned_count(parsed: dict[str, Any]) -> int:
    """Files semgrep ACTUALLY scanned (``paths.scanned``) — the ground truth for files_examined,
    not what we pointed it at. This is what makes ``ran=True`` mean something (hardens FT-D2)."""
    paths = parsed.get("paths")
    scanned = paths.get("scanned") if isinstance(paths, dict) else None
    return len(scanned) if isinstance(scanned, list) else 0


def scan_semgrep(tree: TargetTree, run_id: str) -> ScannerResult:
    if not tool_available(_SEMGREP):
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_SEMGREP, ran=False, files_examined=0, gap_reason=tool_unavailable_reason(_SEMGREP)
            ),
        )
    stacks = detect_stacks(tree)
    packs = _semgrep_packs(stacks)
    if not packs:
        # R7-2: no detected stack maps to a semgrep pack (e.g. a pure-Go tree). An honest gap,
        # not a silent clean — generalizes the FT-1.1 no-python gap (which was p/python-specific
        # and mis-read a TS tree as uncovered; semgrep now selects p/typescript there instead).
        label = "/".join(sorted(stack.value for stack in stacks)) or "none"
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_SEMGREP,
                ran=False,
                files_examined=0,
                gap_reason=f"not_applicable: no semgrep ruleset mapped for detected stacks [{label}]",
            ),
        )
    version = tool_version(_SEMGREP)
    config_flags = [flag for pack in packs for flag in ("--config", pack)]
    # Named registry packs (not `--config auto`, which requires metrics-on/phone-home); an
    # offline registry fetch failure → coverage gap (never a masked pass).
    outcome = run(
        [_SEMGREP, "scan", *config_flags, "--json", "--quiet", "--metrics", "off", *_semgrep_scan_target_args(tree)],
        cwd=str(tree.root),
        timeout_s=300,
        raise_on_timeout=False,
    )
    if outcome.timed_out:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_SEMGREP, ran=False, files_examined=0, gap_reason="semgrep timed out (offline registry / slow scan)"
            ),
        )
    try:
        parsed: Any = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_SEMGREP,
                ran=False,
                files_examined=0,
                gap_reason=f"semgrep produced no JSON (likely offline registry fetch): {outcome.stderr.strip()[:200]}",
            ),
        )
    if not isinstance(parsed, dict):
        raise RuntimeError("semgrep did not emit a JSON object")
    findings = _semgrep_findings(parsed, tree.root, run_id, version)
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_SEMGREP, ran=True, files_examined=_paths_scanned_count(parsed)),
    )
