"""Deterministic duplicate detection — disjoint by language (R8-2, ruling §A.4 revised).

ONE UNIVERSAL scanner, two internal lanes over DISJOINT language domains so no physical
duplicate is ever reported twice:

  * PYTHON exact-block (``dup:exact_block``) — the original normalized-10-line-window detector,
    BYTE-UNCHANGED: a self-vet's Python duplicate findings are identical to before R8-2. Normalize
    each file's lines (drop blanks, comments, imports, trivia), hash sliding windows of
    ``_MIN_BLOCK_LINES`` normalized lines, flag any window whose text was already seen elsewhere.
    Catches structural / boilerplate line repetition. Semantic near-duplication stays L2's.
  * NON-PYTHON token-clone (``dup:token_clone``) — lizard's unified-token cross-file duplicate
    detection at the DEFAULT 70-token threshold, over every lizard-supported non-Python file. Closes
    the gap the original scanner left: foreign TS/JS had ZERO deterministic duplication coverage.

The parity probe (2026-07-21) proved lizard-dup is NOT a superset of the exact-block detector at any
usable threshold — they are different duplicate classes — so they run on DISJOINT domains rather than
one superseding the other (§A.4 revision; option B co-location rejected). Python renamed-variable
token-clones therefore stay a NAMED, deterministically-uncovered gap. Each lane keeps its own findings
cap + dropped-count honesty; the coverage row discloses the per-language mechanism split + threshold.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import lizard
import lizard_languages

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

_MIN_BLOCK_LINES = 10
_FINDINGS_CAP = 80
# lizard-dup default (ruling §A.4 revision): the 158x fragment flood at 15 is the empirical proof
# the default 70-token band is the sane one. Recorded in the coverage disclosure + provenance.
_MIN_DUPLICATE_TOKENS = 70
_SCANNER = "duplication"
_EXACT_BLOCK_ID = "dup:exact_block"
_TOKEN_CLONE_ID = "dup:token_clone"

_SKIP_PREFIXES = ("#", "import ", "from ", '"""', "'''")
_SKIP_EXACT = frozenset({"pass", "return", "...", "){", "}", "):"})


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


# --- Python exact-block lane (BYTE-UNCHANGED from the pre-R8-2 detector) ---


def _normalized_lines(text: str) -> list[tuple[int, str]]:
    """Return (1-indexed original line, normalized text) for significant lines."""
    out: list[tuple[int, str]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped in _SKIP_EXACT or stripped.startswith(_SKIP_PREFIXES):
            continue
        out.append((line_no, " ".join(stripped.split())))
    return out


def _window_hash(window: list[tuple[int, str]]) -> str:
    joined = "\n".join(text for _, text in window)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _exact_block_findings(tree: TargetTree, run_id: str) -> tuple[list[Finding], int, int]:
    """Flag exact duplicated code blocks across the Python surface. Returns (findings,
    files_examined, dropped) — block-detection logic identical to the pre-R8-2 scanner.

    Scope (R9-D widen): a FOREIGN target runs over all ``*.py`` (foreign Python trees previously
    had zero exact-dup coverage — the named gap §A.4 left); a self-vet stays on
    ``quality_surface_python()`` (the gates' scope), so self findings are byte-identical.
    Disjointness holds — the token-clone lane stays non-Python.
    """
    seen: dict[str, tuple[str, int]] = {}
    findings: list[Finding] = []
    dropped = 0
    examined = 0
    for rel in (tree.python_files() if tree.foreign else tree.quality_surface_python()):
        examined += 1
        lines = _normalized_lines(tree.abspath(rel).read_text(encoding="utf-8"))
        index = 0
        while index + _MIN_BLOCK_LINES <= len(lines):
            window = lines[index : index + _MIN_BLOCK_LINES]
            key = _window_hash(window)
            first = seen.get(key)
            if first is not None and first[0] != rel:
                if len(findings) >= _FINDINGS_CAP:
                    dropped += 1
                else:
                    findings.append(
                        Finding.build(
                            run_id=run_id,
                            layer=Layer.L1_DETERMINISTIC,
                            dimension=Dimension.DUP,
                            severity=Severity.LOW,
                            file=rel,
                            line=window[0][0],
                            constraint_violated=_EXACT_BLOCK_ID,
                            evidence=f"{_MIN_BLOCK_LINES}-line block duplicates {first[0]}:{first[1]}",
                            fix_suggestion="Extract the shared block into a reused helper (DRY).",
                            provenance=Provenance(source=f"gate:{_SCANNER}", rule_id="dup"),
                            context_profile=ContextProfile.PRODUCTION,
                        )
                    )
                index += _MIN_BLOCK_LINES
                continue
            if first is None:
                seen[key] = (rel, window[0][0])
            index += 1
    return findings, examined, dropped


# --- Non-Python token-clone lane (lizard unified-token duplicate detection) ---


def _nonpython_lizard_files(tree: TargetTree) -> list[str]:
    """Every lizard-analyzable NON-Python file in the tree (disjoint from the Python lane).

    Uses ``all_files`` (not the quality-surface) because a FOREIGN target is not in the the platform
    quality-surface regex — the same reason R8-1's structural_metrics scans the whole tree.
    """
    return [
        rel
        for rel in tree.all_files()
        if not rel.endswith(".py") and lizard_languages.get_reader_for(rel) is not None
    ]


def _token_clone_findings(tree: TargetTree, run_id: str, files: list[str]) -> tuple[list[Finding], int, int]:
    """lizard token-clone duplicates across the non-Python files. Returns (findings, examined,
    dropped). Each duplicate group's 2nd..Nth fragments are reported as clones of the 1st."""
    if not files:
        return [], 0, 0
    version = lizard.version
    extensions = lizard.get_extensions(["duplicate"])
    # lizard is untyped and get_extensions returns mixed function/object extensions; the duplicate
    # extension is the object exposing cross_file_process + get_duplicates.
    dup_ext: Any = next(ext for ext in extensions if type(ext).__name__ == "LizardExtension")
    analyzer = lizard.FileAnalyzer(extensions)
    infos = [analyzer(str(tree.abspath(rel))) for rel in files]
    list(dup_ext.cross_file_process(iter(infos)))  # consume — builds the cross-file token index
    findings: list[Finding] = []
    dropped = 0
    for group in dup_ext.get_duplicates(min_duplicate_tokens=_MIN_DUPLICATE_TOKENS):
        fragments = [
            (_relativize(tree.root, str(snippet.file_name)), int(snippet.start_line), int(snippet.end_line))
            for snippet in group
        ]
        origin_file, origin_line, _ = fragments[0]
        for rel, start_line, _end in fragments[1:]:
            if len(findings) >= _FINDINGS_CAP:
                dropped += 1
                continue
            findings.append(
                Finding.build(
                    run_id=run_id,
                    layer=Layer.L1_DETERMINISTIC,
                    dimension=Dimension.DUP,
                    severity=Severity.LOW,
                    file=rel,
                    line=start_line,
                    constraint_violated=_TOKEN_CLONE_ID,
                    evidence=f"token-clone (≥{_MIN_DUPLICATE_TOKENS} tokens) of {origin_file}:{origin_line}",
                    fix_suggestion="Extract the shared token sequence into a reused helper (DRY).",
                    provenance=Provenance(source="lizard", tool_version=version, rule_id="token_clone"),
                    context_profile=ContextProfile.PRODUCTION,
                )
            )
    return findings, len(files), dropped


def _coverage_gap(py_examined: int, py_dropped: int, tc_examined: int, tc_dropped: int) -> str:
    """Always disclose the per-language mechanism split (rider 3): mechanism, file counts,
    threshold, and per-mechanism dropped-count. Rendered on the ran=True coverage row (a
    disclosure, not a gap — ``coverage_gaps`` keys on ``not ran``, so it is never a gap row)."""
    exact = f"exact-block: {py_examined} python file(s)" + (f", {py_dropped} dropped (cap {_FINDINGS_CAP})" if py_dropped else "")
    token = (
        f"token-clone: {tc_examined} non-python file(s) via lizard (min_duplicate_tokens={_MIN_DUPLICATE_TOKENS})"
        + (f", {tc_dropped} dropped (cap {_FINDINGS_CAP})" if tc_dropped else "")
    )
    return f"{exact}; {token}"


def scan(tree: TargetTree, run_id: str) -> ScannerResult:
    """Disjoint-by-language duplicate detection: Python exact-block + non-Python token-clone."""
    py_findings, py_examined, py_dropped = _exact_block_findings(tree, run_id)
    tc_findings, tc_examined, tc_dropped = _token_clone_findings(tree, run_id, _nonpython_lizard_files(tree))
    return ScannerResult(
        findings=py_findings + tc_findings,
        coverage=CoverageRecord(
            scanner=_SCANNER,
            ran=True,
            files_examined=py_examined + tc_examined,
            gap_reason=_coverage_gap(py_examined, py_dropped, tc_examined, tc_dropped),
        ),
    )
