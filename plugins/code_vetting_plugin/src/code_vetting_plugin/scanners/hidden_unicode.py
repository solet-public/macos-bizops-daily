"""Permanent hidden-Unicode scanner over all model-facing text.

Invisible and direction-controlling characters embedded in prose an LLM reads
are a prompt-injection / smuggling vector (bidi overrides, zero-width joiners,
private-use glyphs, Unicode tag characters). This scan is deterministic and
always runs — it needs no external tool. It sweeps every model-facing text file
(``md``/``json``/``yaml``/``toml``/``py``/…) and emits one finding per
(file, line, category).
"""

from __future__ import annotations

from dataclasses import dataclass

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

_SCANNER = "hidden_unicode"


@dataclass(frozen=True, slots=True)
class _Category:
    key: str
    severity: Severity
    description: str


_BIDI = _Category("bidi_control", Severity.HIGH, "bidirectional-control character (text-direction override)")
_ZERO_WIDTH = _Category("zero_width", Severity.HIGH, "zero-width / invisible formatting character")
_PRIVATE_USE = _Category("private_use", Severity.MEDIUM, "private-use-area codepoint (non-standard glyph)")
_TAG = _Category("tag_chars", Severity.HIGH, "Unicode tag character (ASCII-smuggling block)")
_BOM = _Category("bom", Severity.LOW, "byte-order-mark / zero-width no-break space")


def _classify(codepoint: int) -> _Category | None:
    """Return the hidden-Unicode category for a codepoint, or None if benign."""
    if codepoint == 0xFEFF:
        return _BOM
    if codepoint in (0x200E, 0x200F, 0x061C) or 0x202A <= codepoint <= 0x202E or 0x2066 <= codepoint <= 0x2069:
        return _BIDI
    if codepoint in (0x200B, 0x200C, 0x200D, 0x2060, 0x00AD, 0x180E):
        return _ZERO_WIDTH
    if 0xE0000 <= codepoint <= 0xE007F:
        return _TAG
    if 0xE000 <= codepoint <= 0xF8FF or 0xF0000 <= codepoint <= 0xFFFFD or 0x100000 <= codepoint <= 0x10FFFD:
        return _PRIVATE_USE
    return None


def _scan_line(
    *,
    run_id: str,
    rel_path: str,
    line_no: int,
    text: str,
) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()
    for column, char in enumerate(text, start=1):
        category = _classify(ord(char))
        if category is None or category.key in seen:
            continue
        seen.add(category.key)
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.HIDDEN_UNICODE,
                severity=category.severity,
                file=rel_path,
                line=line_no,
                constraint_violated=f"hidden_unicode:{category.key}",
                evidence=f"U+{ord(char):04X} ({category.description}) at column {column}",
                fix_suggestion="Remove the invisible/control character or replace with its visible equivalent.",
                provenance=Provenance(source=_SCANNER),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return findings


def scan(tree: TargetTree, run_id: str) -> ScannerResult:
    """Scan every model-facing text file for hidden Unicode."""
    findings: list[Finding] = []
    examined = 0
    for rel_path in tree.model_facing():
        raw = tree.abspath(rel_path).read_bytes()
        examined += 1
        # A leading BOM is decoded away by utf-8-sig; flag it explicitly first.
        if raw.startswith(b"\xef\xbb\xbf"):
            findings.append(
                Finding.build(
                    run_id=run_id,
                    layer=Layer.L1_DETERMINISTIC,
                    dimension=Dimension.HIDDEN_UNICODE,
                    severity=_BOM.severity,
                    file=rel_path,
                    line=1,
                    constraint_violated="hidden_unicode:bom",
                    evidence="U+FEFF byte-order-mark at file start",
                    fix_suggestion="Strip the leading BOM; save the file as UTF-8 without BOM.",
                    provenance=Provenance(source=_SCANNER),
                    context_profile=ContextProfile.PRODUCTION,
                )
            )
        text = raw.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            findings.extend(_scan_line(run_id=run_id, rel_path=rel_path, line_no=line_no, text=line))
    coverage = CoverageRecord(scanner=_SCANNER, ran=True, files_examined=examined)
    return ScannerResult(findings=findings, coverage=coverage)
