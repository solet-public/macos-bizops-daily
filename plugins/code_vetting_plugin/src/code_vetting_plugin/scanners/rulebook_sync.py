"""rulebook_sync.py — W3-C C2 (W3C-1b): the self-policing rulebook-integrity scanner.

SELF_ONLY. Two failure modes collapse to ONE ``stale_rulebook`` HIGH finding in ``code_quality``:

  * CORRUPT — the committed ``assembled_rulebook.json``'s content no longer matches its own pinned
    whole-artifact hash (hand-edited, bit-rot, a merge that touched the JSON directly). Caught by the
    shared ``verify_artifact`` raising — the exact fail-loud check every runtime load runs.
  * STALE — the committed artifact verifies internally, but a fresh re-assembly from the CURRENT
    sources produces different content: a source (a directive clause, a DNF rule, the v1 rulebook doc,
    a guidance article) was edited without regenerating + committing the artifact. Caught by comparing
    the fresh whole-artifact hash to the committed one; the per-source hashes localize WHICH anchor
    drifted for the repair step.

Severity HIGH — deliberately above the R8 metric ceiling (Q2): a stale/corrupt rulebook means every
AI verdict in the run was computed against a moat that no longer matches canon, so the run's own trust
chain is broken. Report-not-gate stands (the commit gates never consumed the rulebook); the blocking
signal is the report's integrity banner (``report._integrity_banner``) + the C3 joseki's
``blocks_continuation`` gate.

R6 (verb-import-closure): the assembler pulls the pure-DATA ``verify.lenses`` / ``verify.rulebook``
source modules, so it is imported FUNCTION-LOCALLY inside :func:`scan` — module-import of this scanner
(at runner load, inside the L1 verb's import closure) stays ``verify``-free, exactly like plugin.py's
inference seam. ``manifest`` (hashing only) carries no such closure and is a top-level import.
"""

from __future__ import annotations

import json
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
from ..rulebook.manifest import canonical_hash, verify_artifact
from ..targets import TargetTree

_SCANNER = "rulebook_sync"

# The shared constraint id — imported by ``report`` to render the integrity banner (one definition,
# no magic string across the scanner/report seam).
STALE_RULEBOOK_CONSTRAINT = "stale_rulebook"

# The committed artifact's path RELATIVE to the target tree root. self_only → tree.root is always the
# platform repo, so this is where the shipped assembled rulebook lives (mirrors assembler.ARTIFACT_PATH).
_ARTIFACT_RELPATH = Path("plugins/code_vetting_plugin/src/code_vetting_plugin/rulebook/assembled_rulebook.json")

_ASSEMBLER_CMD = "python -m code_vetting_plugin.rulebook.assembler"
_FIX = f"Re-run `{_ASSEMBLER_CMD}` and commit the regenerated artifact via Git-Controller, then re-run the vet."


def _finding(run_id: str, evidence: str) -> Finding:
    """The single ``stale_rulebook`` finding — HIGH ``code_quality`` (zero-FP-promoted, renders as fact)."""
    return Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.CODE_QUALITY,
        severity=Severity.HIGH,
        file=str(_ARTIFACT_RELPATH),
        line=None,
        constraint_violated=STALE_RULEBOOK_CONSTRAINT,
        evidence=evidence,
        fix_suggestion=_FIX,
        provenance=Provenance(source="gate:rulebook_sync", rule_id=STALE_RULEBOOK_CONSTRAINT),
        context_profile=ContextProfile.PRODUCTION,
    )


def _drifted_anchors(committed_sources: list[dict[str, str]], fresh_sources: list[dict[str, str]]) -> str:
    """The per-source anchors whose hash differs between the committed artifact and a fresh re-assembly
    (the Q1 per-source drift probe) — names WHICH source rot the rulebook. Falls back to a whole-artifact
    note when no single source isolates the diff (e.g. an added/removed section shifts the set)."""
    committed_by_anchor = {source["anchor"]: source["hash"] for source in committed_sources}
    fresh_by_anchor = {source["anchor"]: source["hash"] for source in fresh_sources}
    drifted = sorted(
        anchor
        for anchor in set(committed_by_anchor) | set(fresh_by_anchor)
        if committed_by_anchor.get(anchor) != fresh_by_anchor.get(anchor)
    )
    return ", ".join(drifted) if drifted else "(whole-artifact hash differs; no single source isolated)"


def scan(tree: TargetTree, run_id: str) -> ScannerResult:
    artifact_path = tree.root / _ARTIFACT_RELPATH
    if not artifact_path.is_file():
        # No committed artifact in the target tree — nothing to police (honest gap, never a clean pass).
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_SCANNER,
                ran=False,
                files_examined=0,
                gap_reason=f"not_applicable: no committed {_ARTIFACT_RELPATH.name} in target",
            ),
        )
    committed = json.loads(artifact_path.read_text(encoding="utf-8"))

    # MODE 1 (CORRUPT): the committed content no longer matches its own pinned whole-artifact hash.
    try:
        verify_artifact(committed)
    except ValueError as exc:
        evidence = f"CORRUPT — the committed assembled rulebook fails its own integrity hash: {exc}"
        return ScannerResult(findings=[_finding(run_id, evidence)], coverage=_ran())

    # MODE 2 (STALE): a fresh re-assembly from the CURRENT sources differs from the committed artifact.
    # Function-local import — keeps the verify-pulling assembler out of the L1 verb's import closure (R6).
    from ..rulebook.assembler import assemble  # noqa: PLC0415 — R6 closure guard, see module docstring

    fresh = assemble(tree.root)
    if canonical_hash(fresh["content"]) != committed["manifest"]["artifact_hash"]:
        drifted = _drifted_anchors(committed["manifest"]["sources"], fresh["manifest"]["sources"])
        evidence = (
            "STALE — a rulebook source drifted ahead of the committed artifact without a rebuild "
            f"(the shipped moat no longer matches canon). Drifted source(s): {drifted}."
        )
        return ScannerResult(findings=[_finding(run_id, evidence)], coverage=_ran())

    return ScannerResult(findings=[], coverage=_ran())


def _ran() -> CoverageRecord:
    return CoverageRecord(scanner=_SCANNER, ran=True, files_examined=1)
