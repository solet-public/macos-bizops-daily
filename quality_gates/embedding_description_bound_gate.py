#!/usr/bin/env python3
"""Authoring-time `embedding_description` length-bound gate, with tracked-debt allowlist.

The platform ALREADY constrains this field. `ProcessRegistrationValidator`
(`ananta/src/ananta/core/process_registry/plugin_registration_validator.py::
_validate_discoverable_process`) checks every discoverable process's
`embedding_description` against `[EMBEDDING_DESCRIPTION_MIN_LENGTH,
EMBEDDING_DESCRIPTION_MAX_LENGTH]` at registry load.

But that check is **WARNING-only** — it accumulates out-of-range processes and
logs one aggregate warning per boot. Nothing reds. Nothing runs at authoring or
commit time. The measured consequence (2026-07-30): **77 of 528 process JSONs
are out of range**, including recent additions, i.e. authors are not seeing the
constraint at the moment they write the field. A bound nothing enforces at write
time is a bound in name only. This gate is the enforcement half.

WHY THE FIELD HAS A LENGTH BOUND AT ALL: `embedding_description` is the text
embedded for semantic process retrieval. Too thin and the vector is
under-specified, so the wrong verb wins at rank 1; too fat and the distinctive
terms are diluted by filler. The bound is a proxy for "carries enough
distinguishing vocabulary, and not so much that it blurs."

SINGLE-SOURCED BOUND — this gate does NOT re-declare 200/400. It reads both
constants out of the validator's own source at run time (AST, no import side
effects). If the platform moves the bound, this gate moves with it; if the
constants are renamed or removed, the gate exits 64 rather than silently
enforcing a stale literal. A gate that hardcodes the number it mirrors is a
second source of truth waiting to drift.

SCOPE FIDELITY — the gate mirrors the constraint, it does not extend it:
- Only files that look like process definitions (a `process_key` key) are read.
- `is_discoverable: false` is skipped, exactly as the validator skips it.
- A MISSING or empty `embedding_description` is NOT a finding here. The
  validator accumulates those in a separate `missing` bucket with its own
  warning, and every one of the 528 JSONs currently has the field. Adding a
  missing-field rule would be a new quality rule, which is deliberately out of
  scope for this gate.

DOCUMENTED RESIDUAL — `is_discoverable` declared in CODE, not in the JSON:
the flag can be set on the decorator (`is_discoverable=False`), which a
JSON-only scan cannot see. Measured 2026-07-30: 51 processes declare it in
code; 5 of those also ship a KB JSON (`complete_deploy`, `complete_swap`,
`create_cron_schedule`, `noop`, `register`). All 5 are currently IN range, so
there is no live divergence. If one ever drifts out of range it is an ALLOWLIST
entry citing this residual — not a real finding, and not a reason to loosen the
bound for everyone.

The `--allowlist <path>` file is a tracked-debt register, NOT a skip path: one
`<file_path>::<process_key>` per line (POSIX-suffix-matched path, full process
key). Allowlisted findings are still PRINTED so the gate stays honest; they do
not contribute to the exit-2 verdict. Removing an entry is the unit of
remediation progress; adding entries requires operator ratification.

Exit codes:
  0 — every discoverable process JSON is in range (or is allowlisted)
  2 — one or more non-allowlisted out-of-range values
  64 — usage error (bad arguments, unresolvable roots, or an unreadable bound)
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402

_MIN_CONST = "EMBEDDING_DESCRIPTION_MIN_LENGTH"
_MAX_CONST = "EMBEDDING_DESCRIPTION_MAX_LENGTH"
_VALIDATOR_REL = Path(
    "ananta/src/ananta/core/process_registry/plugin_registration_validator.py",
)
_BUNDLED_VENV_PREFIX = ".venv"
_PROCESS_KEY_FIELD = "process_key"
_EMBEDDING_FIELD = "embedding_description"
_DISCOVERABLE_FIELD = "is_discoverable"


@dataclass(frozen=True)
class _Finding:
    file_path: Path
    process_key: str
    length: int
    reason: str
    allowlisted: bool


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_bound(root: Path) -> tuple[int, int]:
    """Read the platform's own bound constants. Raises ValueError if absent.

    Deliberately AST-parsed rather than imported: importing the validator pulls
    in the ananta package (and its env expectations) for two integers.
    """
    source_path = root / _VALIDATOR_REL
    if not source_path.is_file():
        raise ValueError(f"validator source not found at {source_path}")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, int):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in (_MIN_CONST, _MAX_CONST):
                found[target.id] = value.value
    missing = {_MIN_CONST, _MAX_CONST} - set(found)
    if missing:
        raise ValueError(
            f"{sorted(missing)} not found in {_VALIDATOR_REL} — the gate cannot "
            "mirror a bound it cannot read.",
        )
    return found[_MIN_CONST], found[_MAX_CONST]


def _scan_roots(root: Path) -> list[Path]:
    roots = [root / "plugins", root / "ananta"]
    return [r for r in roots if r.is_dir()]


def _iter_process_json(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for scan_root in roots:
        for path in sorted(scan_root.rglob("processes/*.json")):
            if any(part.startswith(_BUNDLED_VENV_PREFIX) for part in path.parts):
                continue
            files.append(path)
    return files


def _load_allowlist(path: Path) -> frozenset[tuple[str, str]]:
    """Mandatory owner/reason/expires schema (D-3-structural, iss_23fa51b5)."""

    entries: set[tuple[str, str]] = set()
    for key in _load_tagged_allowlist(path):
        if "::" not in key:
            print(f"WARN: malformed allowlist line ignored: {key!r}", file=sys.stderr)
            continue
        file_part, _, key_part = key.partition("::")
        entries.add((file_part.strip(), key_part.strip()))
    return frozenset(entries)


def _matching_entries(
    rel_path: str, process_key: str, allowlist: frozenset[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Every allowlist entry this (path, key) satisfies — for orphan tracking."""
    return {
        (entry_path, entry_key)
        for entry_path, entry_key in allowlist
        if rel_path.endswith(entry_path) and process_key == entry_key
    }


def _checkable_embedding(path: Path) -> tuple[str, str] | None:
    """(process_key, embedding_description) for a checkable process JSON, else None.

    Returns None for: unreadable/non-JSON files, files that are not process
    definitions, non-discoverable processes (mirroring the validator's own
    skip), and a missing/empty field (the validator's separate `missing`
    bucket, deliberately not this gate's finding class).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"WARN: cannot read {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or _PROCESS_KEY_FIELD not in data:
        return None
    if data.get(_DISCOVERABLE_FIELD, True) is not True:
        return None
    embedding = data.get(_EMBEDDING_FIELD)
    if not isinstance(embedding, str) or not embedding:
        return None
    return str(data[_PROCESS_KEY_FIELD]), embedding


def _scan(
    files: list[Path],
    root: Path,
    low: int,
    high: int,
    allowlist: frozenset[tuple[str, str]],
    raw_mode: bool,
) -> tuple[int, list[_Finding], set[tuple[str, str]]]:
    """Return (processes_checked, findings, allowlist_entries_that_matched)."""
    checked = 0
    findings: list[_Finding] = []
    used: set[tuple[str, str]] = set()
    for path in files:
        entry = _checkable_embedding(path)
        if entry is None:
            continue
        process_key, embedding = entry
        checked += 1
        length = len(embedding)
        if low <= length <= high:
            continue
        rel_path = path.relative_to(root).as_posix()
        matched = _matching_entries(rel_path, process_key, allowlist)
        used |= matched
        side = "below the floor" if length < low else "above the ceiling"
        findings.append(
            _Finding(
                file_path=path.relative_to(root),
                process_key=process_key,
                length=length,
                reason=f"{length} chars, {side} [{low}, {high}]",
                allowlisted=not raw_mode and bool(matched),
            ),
        )
    return checked, findings, used


def _print_findings(findings: list[_Finding]) -> None:
    if not findings:
        return
    print("\nembedding_description length-bound findings:")
    for finding in sorted(findings, key=lambda f: (f.allowlisted, f.length)):
        marker = "ALLOWLISTED" if finding.allowlisted else "FINDING    "
        print(f"  {marker}  {finding.process_key} — {finding.reason}")
        print(f"               {finding.file_path.as_posix()}")


def _print_stale(stale: set[tuple[str, str]]) -> None:
    """Report allowlist entries that no longer guard anything.

    A stale entry is NOT dead weight — it is a latent OVER-ALLOW: an exception
    nobody remembers, guarding nothing, that silently permits the condition if
    it ever returns. Reported, never fatal: exiting non-zero here would red the
    gate for someone who just IMPROVED a description, punishing the exact
    behaviour the register exists to encourage.

    Covers both causes — the value came back into range, and the file or
    process key was renamed or deleted out from under the entry.
    """
    if not stale:
        return
    print(f"\n[stale-allowlist] {len(stale)} entry(ies) no longer match any finding:")
    for entry_path, entry_key in sorted(stale):
        print(f"  [stale-allowlist]  {entry_key}")
        print(f"                     {entry_path}")
    print("  → back in range, renamed, or deleted. Remove these entries.")


def _report(
    checked: int, file_count: int, findings: list[_Finding], allowlist_active: bool,
) -> int:
    live = [f for f in findings if not f.allowlisted]
    allowlisted = len(findings) - len(live)
    print(
        f"\nembedding_description bound gate: {checked} discoverable process(es) "
        f"with an embedding_description, across {file_count} JSON file(s) scanned.",
    )
    if allowlist_active:
        print(f"  tracked debt (allowlisted, still reported): {allowlisted}")
    if live:
        print(f"  ❌ {len(live)} non-allowlisted out-of-range value(s)")
        return 2
    print("  ✅ no non-allowlisted out-of-range values")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "Tracked-debt register: one `<file_path>::<process_key>` per line "
            "(POSIX-suffix-matched path, full process key). Allowlisted "
            "findings still print; they do not contribute to the exit-2 "
            "verdict. Removing an entry is the unit of remediation progress."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help=(
            "Diagnostic flag: same measurement, allowlist IGNORED — every "
            "out-of-range value contributes to the exit-2 verdict."
        ),
    )
    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    root = _repo_root()

    try:
        low, high = _read_bound(root)
    except (ValueError, OSError, SyntaxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 64
    print(f"Bound read from {_VALIDATOR_REL.as_posix()}: [{low}, {high}]")

    roots = _scan_roots(root)
    if not roots:
        print("ERROR: no scan roots found (plugins, ananta).", file=sys.stderr)
        return 64

    allowlist: frozenset[tuple[str, str]] = frozenset()
    allowlist_active = args.allowlist is not None and not args.raw
    if args.allowlist is not None:
        try:
            allowlist = _load_allowlist(args.allowlist)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 64

    files = _iter_process_json(roots)
    checked, findings, used = _scan(files, root, low, high, allowlist, args.raw)
    _print_findings(findings)
    if allowlist_active:
        _print_stale(set(allowlist) - used)
    return _report(checked, len(files), findings, allowlist_active)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
