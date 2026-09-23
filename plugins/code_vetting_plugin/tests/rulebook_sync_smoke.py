"""rulebook_sync_smoke.py — W3-C C2 (W3C-1b): the rulebook-integrity scanner.

Pins the self-policing lane that keeps the moat honest — two failure modes → ONE ``stale_rulebook``
HIGH ``code_quality`` finding, plus the report integrity banner:

  * CLEAN — a committed artifact freshly assembled from its sources yields ZERO findings (ran=True).
  * STALE (red-first) — editing a source (a guidance article) WITHOUT rebuilding drifts it ahead of the
    committed artifact → exactly one HIGH ``code_quality`` ``stale_rulebook`` finding whose evidence
    names the drifted per-source anchor.
  * CORRUPT (red-first) — a hand-edited committed artifact whose content no longer matches its own
    pinned whole-artifact hash → the same finding via the shared ``verify_artifact`` fail-loud path.
  * BANNER — the report renders the RULEBOOK INTEGRITY WARNING in the title block iff the run carries
    the finding (and is byte-absent on a clean run).
  * ROSTER — ``rulebook_sync`` is the 20th scanner, SELF_ONLY, parse-only (``executes_target_code=False``).
  * GAP — a target with no committed artifact records an honest not_applicable gap, never a clean pass.

The tree's FILE sources (the v1 doc + the two guidance articles) are seeded minimally; the in-code
directive/DNF sources come from the imported modules (as the assembler reads them). Run directly or
via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.models import ContextProfile, Dimension, Finding, Severity
from code_vetting_plugin.report import ReportRenderer
from code_vetting_plugin.rulebook.assembler import (  # noqa: PLC2701 — the assembler source relpaths ARE the fixture contract
    _GUIDANCE_RELPATHS,
    _RULEBOOK_DOC_RELPATH,
    write_artifact,
)
from code_vetting_plugin.run_record import RunTarget
from code_vetting_plugin.runner import SCANNERS, Applicability
from code_vetting_plugin.scanners import rulebook_sync
from code_vetting_plugin.scanners.rulebook_sync import (  # noqa: PLC2701 — pin the scanner's own artifact path
    _ARTIFACT_RELPATH,
    STALE_RULEBOOK_CONSTRAINT,
)
from code_vetting_plugin.targets import TargetTree

_BANNER_MARK = "RULEBOOK INTEGRITY WARNING"
_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _seed_self_tree(root: Path) -> TargetTree:
    """Seed the FILE sources + a FRESH committed artifact, and return a self (non-foreign) tree."""
    (root / _RULEBOOK_DOC_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (root / _RULEBOOK_DOC_RELPATH).write_text("## Universal\nbe skeptical\n\n## Project tier\nRB-FASTFAIL\n", encoding="utf-8")
    for guidance in _GUIDANCE_RELPATHS:
        (root / guidance).parent.mkdir(parents=True, exist_ok=True)
        (root / guidance).write_text(f"# {guidance.name}\nguidance body\n", encoding="utf-8")
    (root / _ARTIFACT_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    write_artifact(root / _ARTIFACT_RELPATH, root=root)
    return TargetTree(root=root, tracked=(), enumeration="git", foreign=False)


def _report(findings: list[Finding]) -> str:
    return ReportRenderer().render(
        run_id="vr",
        target=RunTarget(repo="example", ref="HEAD", scope="self"),
        context_profile=ContextProfile.PRODUCTION,
        generated_at="t",
        findings=findings,
        coverage=[],
    )


def _check_roster() -> None:
    spec = next((s for s in SCANNERS if s.name == "rulebook_sync"), None)
    _check("rulebook_sync is the 20th roster scanner", len(SCANNERS) == 20 and spec is not None, str(len(SCANNERS)))
    assert spec is not None
    _check("rulebook_sync is SELF_ONLY, parse-only", spec.applicability is Applicability.SELF_ONLY and spec.executes_target_code is False, str(spec))


def _check_clean(root: Path) -> None:
    tree = _seed_self_tree(root)
    result = rulebook_sync.scan(tree, "vr")
    _check("CLEAN tree: zero findings, ran=True over 1 artifact", result.findings == [] and result.coverage.ran is True and result.coverage.files_examined == 1, str(result.coverage))
    _check("CLEAN run: report has NO integrity banner", _BANNER_MARK not in _report(result.findings), "")


def _check_stale(root: Path) -> None:
    tree = _seed_self_tree(root)
    # Edit a guidance source WITHOUT rebuilding — a fresh re-assembly now drifts ahead of the committed copy.
    (root / _GUIDANCE_RELPATHS[0]).write_text("# edited\nDRIFTED body\n", encoding="utf-8")
    result = rulebook_sync.scan(tree, "vr")
    _check("STALE: exactly one finding", len(result.findings) == 1, str(len(result.findings)))
    finding = result.findings[0]
    _check(
        "STALE: HIGH code_quality stale_rulebook (above the R8 ceiling — suite integrity)",
        finding.severity is Severity.HIGH and finding.dimension is Dimension.CODE_QUALITY and finding.constraint_violated == STALE_RULEBOOK_CONSTRAINT,
        f"{finding.severity}/{finding.dimension}/{finding.constraint_violated}",
    )
    _check("STALE: evidence localizes the drifted per-source anchor", "STALE" in finding.evidence and "guidance::" in finding.evidence, finding.evidence)
    _check("STALE run: report RENDERS the integrity banner in the title block", _BANNER_MARK in _report(result.findings), "")


def _check_corrupt(root: Path) -> None:
    tree = _seed_self_tree(root)
    # Hand-edit the committed artifact's content without fixing its pinned hash → verify_artifact fails loud.
    artifact_path = root / _ARTIFACT_RELPATH
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["content"]["version"] = "TAMPERED"
    artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    result = rulebook_sync.scan(tree, "vr")
    _check("CORRUPT: exactly one finding", len(result.findings) == 1, str(len(result.findings)))
    finding = result.findings[0]
    _check(
        "CORRUPT: HIGH code_quality stale_rulebook via the shared verify_artifact fail-loud",
        finding.severity is Severity.HIGH and finding.dimension is Dimension.CODE_QUALITY and finding.constraint_violated == STALE_RULEBOOK_CONSTRAINT and "CORRUPT" in finding.evidence,
        finding.evidence,
    )
    _check("CORRUPT run: report RENDERS the integrity banner", _BANNER_MARK in _report(result.findings), "")


def _check_missing_gap(root: Path) -> None:
    tree = _seed_self_tree(root)
    (root / _ARTIFACT_RELPATH).unlink()
    result = rulebook_sync.scan(tree, "vr")
    _check(
        "MISSING artifact: honest not_applicable gap (ran=False), never a clean pass",
        result.findings == [] and result.coverage.ran is False and (result.coverage.gap_reason or "").startswith("not_applicable:"),
        str(result.coverage),
    )


def main() -> int:
    try:
        _check_roster()
        with tempfile.TemporaryDirectory() as tmp:
            _check_clean(Path(tmp) / "clean")
        with tempfile.TemporaryDirectory() as tmp:
            _check_stale(Path(tmp) / "stale")
        with tempfile.TemporaryDirectory() as tmp:
            _check_corrupt(Path(tmp) / "corrupt")
        with tempfile.TemporaryDirectory() as tmp:
            _check_missing_gap(Path(tmp) / "missing")
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"rulebook_sync_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
