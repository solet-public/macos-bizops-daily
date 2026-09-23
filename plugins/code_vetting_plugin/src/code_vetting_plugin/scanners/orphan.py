"""Orphan KB-process-JSON scanner.

Every plugin process JSON under ``plugins/<X>/knowledge_base/processes/*.json``
declares a ``process_key`` (``plugin::<X>::<verb>`` or
``service_interface::<svc>::<verb>``). A JSON whose verb is defined nowhere in
the owning plugin's ``src`` (nor in ``ananta/src`` for service-interface verbs)
is an orphan overlay — documentation for a process the code no longer provides.
This is the KB-drift direction the Phase-1 security-scan article calls out; the
code→JSON direction is already covered by the whole-tree integration gate.
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
from ..targets import TargetTree

_SCANNER = "orphan"


def _plugin_dir_of(rel: str) -> str | None:
    parts = Path(rel).parts
    if len(parts) >= 2 and parts[0] == "plugins":
        return f"plugins/{parts[1]}"
    return None


def _concat_src(tree: TargetTree, src_root: str) -> str:
    """Concatenate all Python source text under ``src_root`` (cached by caller)."""
    chunks: list[str] = []
    for rel in tree.python_files():
        if rel.startswith(src_root):
            chunks.append(tree.abspath(rel).read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _verb_defined(verb: str, haystacks: list[str]) -> bool:
    needles = (f"def {verb}", f'"{verb}"', f"'{verb}'", f"name={verb!r}")
    return any(any(needle in text for needle in needles) for text in haystacks)


def _process_key(rel_path: Path) -> str | None:
    parsed: Any = json.loads(rel_path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict):
        key = parsed.get("process_key")
        return key if isinstance(key, str) else None
    return None


def scan(tree: TargetTree, run_id: str) -> ScannerResult:
    """Flag process JSON overlays whose verb is defined nowhere in code."""
    src_cache: dict[str, str] = {}
    ananta_src = _concat_src(tree, "ananta/src")
    findings: list[Finding] = []
    examined = 0
    for rel in tree.process_json():
        examined += 1
        process_key = _process_key(tree.abspath(rel))
        if process_key is None or "::" not in process_key:
            continue
        verb = process_key.rsplit("::", 1)[1]
        plugin_dir = _plugin_dir_of(rel)
        haystacks: list[str] = [ananta_src]
        if plugin_dir is not None:
            src_root = f"{plugin_dir}/src"
            if src_root not in src_cache:
                src_cache[src_root] = _concat_src(tree, src_root)
            haystacks.append(src_cache[src_root])
        if _verb_defined(verb, haystacks):
            continue
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.ORPHAN,
                severity=Severity.MEDIUM,
                file=rel,
                line=None,
                constraint_violated="orphan:kb_process_json",
                evidence=f"process JSON declares '{process_key}' but verb '{verb}' is defined in no source file",
                fix_suggestion="Remove the stale process JSON, or implement/register the process it documents.",
                provenance=Provenance(source=f"gate:{_SCANNER}", rule_id="orphan"),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_SCANNER, ran=True, files_examined=examined),
    )
