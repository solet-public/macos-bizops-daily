"""On-disk reference scan for process retirement (Phase 6 §4.2).

Before a process is retired (decorator + JSON removed together), the retire
procedure must know which artifacts still NAME it, so removal does not leave a
dangling reference. This module is the guard: a deterministic, exact-string
scan of the on-disk knowledge-base source for a given ``process_key``.

Scope is DELIBERATELY PARTIAL and the partiality is machine-readable, not a
comment (operator ruling, Phase 6 Tier 2): the return carries a
``scanned_corpora`` list of exactly what was walked and an ``unscanned`` list
naming what was NOT — specifically the LIVE plan / WBS instances that live in
the database (``thinking_plans`` / ``thinking_wbs``), which an on-disk scan
cannot see. Retirement is rare and operator-witnessed, so an honest partial
scan now beats cross-lane sprawl; live-instance coverage belongs to Tier 3+
where the plan template/instance split (§4.5) gives it real structure.

Delimiter-aware exact match on the full ``process_key`` (never semantic) — a
joseki / WBS / plan / capability article names a process as e.g.
``(service_interface::thinking_service::foo)``. The key must NOT be immediately
preceded or followed by an identifier character (``[A-Za-z0-9_]``), so a longer
key that merely contains this one as a prefix/suffix — ``…::foo_extended`` or
``x…::foo`` — is not counted. A retire GUARD must never conservative-block a
removal on a non-reference, so a raw substring count (which would treat
``foo_extended`` as a reference to ``foo``) is wrong here.

A verb wrapper (natural home: ``discovery_service``) is a documented follow-on,
out of the Tier-2 lane; this module is the pure function the retire procedure
and its smoke consume directly.
"""

from __future__ import annotations

import string
from pathlib import Path
from typing import Any

# Identifier characters: a key flanked by one of these on either side is part
# of a LONGER key, not a reference to this one.
_IDENT_CHARS = frozenset(string.ascii_letters + string.digits + "_")

# On-disk knowledge-base source roots, repo-relative. Deduped by resolved path
# so the `knowledge_bases/` symlink-aggregation dir does not double-count the
# plugin / platform sources it points at.
_KB_ROOT_GLOBS: tuple[str, ...] = (
    "knowledge_bases",
    "ananta/knowledge_bases",
    "plugins/*/knowledge_base",
    "plugins/*/knowledge_base_joseki",
)

# The corpus this scan CANNOT see — surfaced verbatim in every result so a
# "no references found" reading is never mistaken for "safe to retire".
_UNSCANNED_NOTE = (
    "Live plan/WBS INSTANCES in the database (thinking_plans / thinking_wbs) "
    "are NOT scanned by this on-disk scan. Before retiring, check live "
    "instances separately (e.g. query the thinking_service plan/WBS state for "
    "steps naming this process key). On-disk joseki/WBS/plan/article SOURCE is "
    "covered below."
)


def default_kb_roots(repo_root: Path) -> list[Path]:
    """Resolve the standard on-disk KB source roots under *repo_root*.

    Returns existing directories only; a glob that matches nothing (e.g. a
    plugin without a knowledge base) simply contributes no root.
    """
    roots: list[Path] = []
    for pattern in _KB_ROOT_GLOBS:
        if "*" in pattern:
            roots.extend(p for p in repo_root.glob(pattern) if p.is_dir())
        else:
            candidate = repo_root / pattern
            if candidate.is_dir():
                roots.append(candidate)
    return roots


def scan_process_references(
    process_key: str,
    kb_roots: list[Path],
) -> dict[str, Any]:
    """Find on-disk KB source files that name *process_key* (exact substring).

    Returns a machine-readable report: the referencing files (with per-file
    match counts), the corpora actually scanned, and the explicit unscanned
    note. ``reference_count == 0`` means no ON-DISK reference — NOT "safe to
    retire" on its own; read ``unscanned`` first.
    """
    references: list[dict[str, Any]] = []
    scanned_corpora: list[dict[str, Any]] = []
    seen: set[Path] = set()
    total_files = 0

    for root in kb_roots:
        corpus_files = 0
        for md_path in sorted(root.rglob("*.md")):
            resolved = md_path.resolve()
            if resolved in seen or not md_path.is_file():
                continue
            seen.add(resolved)
            corpus_files += 1
            total_files += 1
            count = _count_occurrences(md_path, process_key)
            if count:
                references.append(
                    {"path": _display_path(md_path), "match_count": count},
                )
        scanned_corpora.append(
            {"root": _display_path(root), "file_count": corpus_files},
        )

    references.sort(key=lambda r: r["path"])
    return {
        "process_key": process_key,
        "references": references,
        "reference_count": len(references),
        "scanned_corpora": scanned_corpora,
        "scanned_file_count": total_files,
        "unscanned": [_UNSCANNED_NOTE],
    }


def _count_occurrences(path: Path, needle: str) -> int:
    """Count delimiter-aware occurrences of *needle* in *path*'s text.

    An occurrence counts only when the key is not part of a longer identifier
    (see the module docstring): the character before it and the character after
    it must each be absent or a non-identifier character.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return _count_delimited(text, needle)


def _count_delimited(text: str, needle: str) -> int:
    """Count non-overlapping delimiter-bounded occurrences of *needle*."""
    count = 0
    start = 0
    width = len(needle)
    while (idx := text.find(needle, start)) != -1:
        before_ok = idx == 0 or text[idx - 1] not in _IDENT_CHARS
        after = idx + width
        after_ok = after >= len(text) or text[after] not in _IDENT_CHARS
        if before_ok and after_ok:
            count += 1
        start = after
    return count


def _display_path(path: Path) -> str:
    """Best-effort repo-relative display path (falls back to the name)."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return path.name
