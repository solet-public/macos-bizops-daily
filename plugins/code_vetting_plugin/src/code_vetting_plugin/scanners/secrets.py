"""Secret-detection scanners: gitleaks (+ trufflehog when present).

Both tools walk a *directory*, so pointing them at the live working tree would
scan gitignored ``.venv`` / blob stores / scratchpad — slow and a false-positive
source. Instead the scan runs over a materialized snapshot of the **tracked**
tree (``git archive HEAD`` extracted to a temp dir), so it sees exactly what
ships. gitleaks runs in ``--redact`` mode so a real secret is never written into
the findings register (F1 §3: the trail must not become its own leak). trufflehog
is optional; when absent the scanner records a coverage gap rather than a silent
pass.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
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
from ..targets import TargetTree
from ..toolrun import run, tool_available, tool_unavailable_reason, tool_version

_GITLEAKS = "gitleaks"
_TRUFFLEHOG = "trufflehog"

# gitleaks' low-confidence CATCH-ALL rules — high-entropy substrings that *look*
# like a key. On structurally-not-a-secret paths (test fixtures, archived docs,
# generated composition artifacts) these are pure false positives. Specific
# provider rules (github-pat, aws-access-token, stripe, private-key, …) are
# deliberately NOT in this set: a specific-rule match is worth surfacing even on a
# safe path, so a real leaked key in a fixture STILL fires HIGH (suite v1.1).
_LOW_CONFIDENCE_RULES: frozenset[str] = frozenset({"generic-api-key"})


class _SafePathClass(StrEnum):
    """A path class where a low-confidence secret-rule hit is not a real secret."""

    TEST = "test"  # test dirs / fixtures / smokes — synthetic credentials, not live
    ARCHIVE = "archive"  # frozen archived docs (KB .archive/, workbench/archive/)
    COMPOSITION_ARTIFACT = "composition-artifact"  # generated composition prompt/render dumps


# Ordered path predicates for the low-confidence-rule downgrade. The TEST pattern
# mirrors the F2 rulebook's test-path convention (tests?/, test_*.py, *_test.py);
# ARCHIVE matches an ``archive/`` or ``.archive/`` segment anywhere; the
# composition class is the generated-artifact content KB. Scoped to the whole
# tracked tree because secrets is a safety dimension (targets.py sweeps all paths).
_SAFE_PATH_PATTERNS: tuple[tuple[_SafePathClass, re.Pattern[str]], ...] = (
    (_SafePathClass.TEST, re.compile(r"(?:^|/)tests?/|(?:^|/)test_[^/]*\.py$|_test\.py$")),
    (_SafePathClass.ARCHIVE, re.compile(r"(?:^|/)\.?archive/")),
    (_SafePathClass.COMPOSITION_ARTIFACT, re.compile(r"^knowledge_bases/compositions/")),
)


def _known_safe_class(path: str) -> _SafePathClass | None:
    """The known-safe path class ``path`` belongs to, or ``None`` for real source."""
    for label, pattern in _SAFE_PATH_PATTERNS:
        if pattern.search(path):
            return label
    return None


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


def _extract_skipping_unsafe_links(archive: tarfile.TarFile, dest: Path) -> None:
    """``extractall(dest, filter="data")``, but a rejected symlink is SKIPPED
    rather than fatal.

    ``filter="data"`` (PEP 706) rejects any absolute or destination-escaping
    link target and raises, which kills the whole scan on one bad member
    (measured live: a tracked absolute symlink dies the entire secrets pass).
    A skipped symlink loses nothing for this scanner: gitleaks/trufflehog scan
    file CONTENT, and a symlink carries none of its own — the file it points
    at, if tracked, is a separate archive member scanned on its own merits.
    """
    data_filter = tarfile.data_filter
    for member in archive.getmembers():
        try:
            filtered = data_filter(member, str(dest))
        except tarfile.FilterError:
            continue
        archive.extract(filtered, dest)


@contextmanager
def _tracked_snapshot(tree: TargetTree) -> Generator[Path]:
    """Materialize the ENUMERATED tree into a temp dir for a directory scanner.

    A git target (``tree.root/.git`` present) uses read-only ``git archive HEAD``.
    A NON-git (walk-mode / foreign) target instead gets a read-only COPY of the
    tree's already-enumerated files (``tree.all_files()`` — which the curated walk
    exclude set already pruned of node_modules/.venv/… junk), so a foreign target
    with no ``.git`` is scanned without needing git and without dragging in junk
    (FT-1). Either way the snapshot mirrors the repo-relative layout so a directory
    scanner's paths relativize back correctly. Read-only w.r.t. the target; the
    temp dir is auto-removed on exit.
    """
    with tempfile.TemporaryDirectory(prefix="vetting_snap_") as tmp:
        dest = Path(tmp)
        if (tree.root / ".git").exists():
            completed = subprocess.run(
                ["git", "-C", str(tree.root), "archive", "--format=tar", "HEAD"],
                capture_output=True,
                timeout=180,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"git archive failed at {tree.root}: {completed.stderr.decode('utf-8', 'replace')[:200]}")
            with tarfile.open(fileobj=io.BytesIO(completed.stdout)) as archive:
                _extract_skipping_unsafe_links(archive, dest)
        else:
            for rel in tree.all_files():
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tree.root / rel, out)
        yield dest


def _gitleaks_findings(snapshot: Path, run_id: str, version: str | None) -> list[Finding]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as report:
        outcome = run(
            [
                _GITLEAKS,
                "detect",
                "--no-git",
                "--source",
                str(snapshot),
                "--report-format",
                "json",
                "--report-path",
                report.name,
                "--redact",
                "--exit-code",
                "0",
                "--no-banner",
            ]
        )
        # ``--exit-code 0`` forces the leaks-found exit to 0, so ANY non-zero here
        # is a genuine gitleaks error (bad config / unreadable source). Fail loud
        # rather than trust the resulting empty report as a clean scan — a silent
        # false-clean on the highest-stakes secrets scanner is the failure mode.
        if outcome.returncode != 0:
            raise RuntimeError(f"gitleaks errored (rc={outcome.returncode}): {outcome.stderr.strip()[-300:]}")
        raw = Path(report.name).read_text(encoding="utf-8") or "[]"
    parsed: Any = json.loads(raw)
    if not isinstance(parsed, list):
        raise RuntimeError("gitleaks report was not a JSON array")
    return [
        finding
        for entry in parsed
        if (finding := _build_gitleaks_finding(entry, snapshot, run_id, version)) is not None
    ]


_FIX_REMOVE = "Remove the secret from source; rotate it; store via the vault, never in-repo."
_FIX_DOWNGRADED = (
    "Advisory only — a low-confidence heuristic match on a known-safe fixture/archive/artifact path. "
    "If it is genuinely a live credential, remove + rotate + vault it; otherwise no action."
)


def _build_gitleaks_finding(entry: Any, snapshot: Path, run_id: str, version: str | None) -> Finding | None:  # noqa: ANN401 — narrows untyped tool JSON
    """One F1 finding from a gitleaks report entry, applying the v1.1 downgrade.

    A low-confidence catch-all rule (``_LOW_CONFIDENCE_RULES``) that lands on a
    known-safe path class is stamped ADVISORY (not HIGH) with the reason recorded
    in ``evidence`` — the report then tallies but does not promote it, while
    real-source hits and every specific-provider rule stay HIGH.
    """
    if not isinstance(entry, dict):
        return None
    rule_id = _as_str(entry.get("RuleID")) or "unknown"
    description = _as_str(entry.get("Description"))
    match = _as_str(entry.get("Match"))
    rel = _relativize(snapshot, _as_str(entry.get("File")))
    evidence = f"{description} (redacted match: {match})".strip()
    severity = Severity.HIGH
    fix = _FIX_REMOVE
    safe_class = _known_safe_class(rel) if rule_id in _LOW_CONFIDENCE_RULES else None
    if safe_class is not None:
        severity = Severity.ADVISORY
        fix = _FIX_DOWNGRADED
        evidence = (
            f"{evidence} [downgraded: low-confidence {rule_id} rule on a known-safe "
            f"{safe_class.value} path — heuristic match, not a zero-FP secret fact]"
        )
    return Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.SECRETS,
        severity=severity,
        file=rel,
        line=_as_int(entry.get("StartLine")),
        constraint_violated=f"gitleaks:{rule_id}",
        evidence=evidence,
        fix_suggestion=fix,
        provenance=Provenance(source=_GITLEAKS, tool_version=version, rule_id=rule_id),
        context_profile=ContextProfile.PRODUCTION,
    )


def scan_gitleaks(tree: TargetTree, run_id: str) -> ScannerResult:
    if not tool_available(_GITLEAKS):
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_GITLEAKS, ran=False, files_examined=0, gap_reason=tool_unavailable_reason(_GITLEAKS)
            ),
        )
    version = tool_version(_GITLEAKS)
    with _tracked_snapshot(tree) as snapshot:
        findings = _gitleaks_findings(snapshot, run_id, version)
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_GITLEAKS, ran=True, files_examined=len(tree.all_files())),
    )


def scan_trufflehog(tree: TargetTree, run_id: str) -> ScannerResult:
    """trufflehog is optional; record a coverage gap when it is not installed."""
    if not tool_available(_TRUFFLEHOG):
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_TRUFFLEHOG,
                ran=False,
                files_examined=0,
                gap_reason=f"{tool_unavailable_reason(_TRUFFLEHOG)} — verified-secret cross-check not run",
            ),
        )
    version = tool_version(_TRUFFLEHOG)
    with _tracked_snapshot(tree) as snapshot:
        outcome = run([_TRUFFLEHOG, "filesystem", str(snapshot), "--json", "--no-update"])
        findings = _trufflehog_findings(outcome.stdout, snapshot, run_id, version)
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_TRUFFLEHOG, ran=True, files_examined=len(tree.all_files())),
    )


def _trufflehog_findings(stdout: str, snapshot: Path, run_id: str, version: str | None) -> list[Finding]:
    findings: list[Finding] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parsed: Any = json.loads(line)
        if not isinstance(parsed, dict):
            continue
        detector = _as_str(parsed.get("DetectorName")) or "unknown"
        rel = _trufflehog_path(parsed.get("SourceMetadata"), snapshot)
        verified = bool(parsed.get("Verified"))
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.SECRETS,
                severity=Severity.BLOCKER if verified else Severity.HIGH,
                file=rel,
                line=None,
                constraint_violated=f"trufflehog:{detector}",
                evidence=f"{detector} secret detected (verified={verified})",
                fix_suggestion="Remove the secret from source; rotate it; store via the vault, never in-repo.",
                provenance=Provenance(source=_TRUFFLEHOG, tool_version=version, rule_id=detector),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return findings


def _trufflehog_path(metadata: object, snapshot: Path) -> str:
    """Repo-relative path from a trufflehog filesystem SourceMetadata object."""
    if not isinstance(metadata, dict):
        return ""
    data = metadata.get("Data")
    if not isinstance(data, dict):
        return ""
    fs_meta = data.get("Filesystem")
    if not isinstance(fs_meta, dict):
        return ""
    return _relativize(snapshot, _as_str(fs_meta.get("file")))
