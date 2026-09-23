#!/usr/bin/env python3
"""Radon cyclomatic-complexity gate with tracked-debt allowlist.

Wraps `radon cc` per-file, surfaces every function ranked C or worse,
and consults a tracked-debt allowlist before deciding the exit code.
Unlike the maintainability-index gate, there's no `@platform_process`
veneer to strip — cyclomatic complexity is per-function, not per-file,
and the platform-process methods themselves can perfectly well be A/B.
The reform here is solely the allowlist mechanism that mirrors
`god_class_check.py` and `radon_mi_check.py`.

Per the KB "Critical Development Guidelines v2", cyclomatic complexity is A
or B only — any function ranked C, D, E, or F that isn't in the allowlist
fails the gate.

The `--allowlist <path>` file is a tracked-debt register: one
`<file_path>::<function_name>` per line. Operator decision 2026-05-25:
the currently-listed entries are deferred to the post-AWS-solet
cycle (Task #74). Removing an entry from the allowlist is the unit of
remediation progress; the allowlist is NOT a skip path.

Function name matching is by `(file_path_suffix, bare_function_name)`:
the file-path entry is POSIX-suffix-matched against the candidate file's
resolved path (so repo-relative entries work cleanly), and the function
name is the bare name (no `ClassName.` prefix). This matches the
brief's entry format.

Exit codes:
  0 — no C+ functions, or every C+ function is allowlisted
  2 — one or more non-allowlisted C+ functions
  64 — usage error (bad arguments / non-existent paths)
  70 — GATE CRASH: the analyser raised, so no verdict exists for the tree.
       A crash is NOT a violation count, and callers must not render it as
       one — see `gate_scope.GateCrashError` and `code_quality_check.py`'s
       `_GateOutcome.CRASH`. Observed 2026-08-16: `radon`'s `cc_visit`
       raises `RecursionError` on some deeply-nested third-party sources,
       which previously escaped as a bare traceback and exit 1, and the
       aggregate reported it as "Blocking gate violations (radon_cc)".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gate_scope import (
    GATE_CRASH_EXIT,
    GateCrashError,
    repo_python_files,
)
from radon.complexity import cc_rank, cc_visit


def _expand_targets(raw_paths: list[str]) -> list[Path]:
    """Resolve CLI path arguments to concrete `.py` files.

    A DIRECTORY expands to its in-repo `.py` files only (tracked, or
    untracked and not ignored). Measured
    2026-08-16: a bare run over `plugins/cosyvoice2_tts_plugin` reached
    18,321 files in `src/.venv_cosyvoice`, reported sympy's C-grade
    functions as this repo's findings, and then crashed on one of them.

    A file named EXPLICITLY is always scanned, in-repo or not — the caller
    asked for that path by name, and a brand-new file is the normal case.
    """
    out: list[Path] = []
    for raw in raw_paths:
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_dir():
            out.extend(repo_python_files(p))
        elif p.suffix == ".py":
            out.append(p)
    return out


def _load_allowlist(path: Path) -> frozenset[tuple[str, str]]:
    """Read a `<file_path>::<function_name>` allowlist file.

    Per-line format: `path/to/file.py::function_name`. Blank lines and
    `#` comments are ignored. Returns a set of (file_suffix, func_name)
    tuples; both fields are exact-match (file_path is POSIX-suffix-matched
    by `_finding_is_allowlisted`).
    """
    if not path.exists():
        raise FileNotFoundError(f"allowlist file not found: {path}")
    entries: set[tuple[str, str]] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if "::" not in line:
            print(
                f"WARN: malformed allowlist line (missing '::'): {line!r}",
                file=sys.stderr,
            )
            continue
        file_part, func_part = line.split("::", 1)
        entries.add((file_part.strip(), func_part.strip()))
    return frozenset(entries)


def _finding_is_allowlisted(
    file_path: Path, func_name: str, allowlist: frozenset[tuple[str, str]],
) -> bool:
    """True if (file_path, func_name) matches any allowlist entry.

    File path is POSIX-suffix-matched (so repo-relative entries work);
    function name is exact-match against the bare name.
    """
    candidate = file_path.as_posix()
    return any(
        candidate.endswith(entry_file) and func_name == entry_func
        for entry_file, entry_func in allowlist
    )


def _scan_file(path: Path) -> list[tuple[int, str, str, int]]:
    """Return [(lineno, fullname, rank, complexity)] for every C+ function.

    Uses radon's library API (`cc_visit`) rather than the CLI to avoid
    fragile stdout parsing. `fullname` carries the optional `ClassName.`
    qualifier for methods; the bare name is what allowlist entries match.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"WARN: cannot read {path}: {exc}", file=sys.stderr)
        return []
    try:
        visited = cc_visit(source)
    except SyntaxError as exc:
        print(f"WARN: syntax error in {path}: {exc}", file=sys.stderr)
        return []
    except Exception as exc:  # noqa: BLE001 — any analyser failure is a non-verdict
        raise GateCrashError(path, f"{type(exc).__name__} in radon cc_visit: {exc}") from exc
    findings: list[tuple[int, str, str, int]] = []
    for item in visited:
        rank = cc_rank(item.complexity)
        if rank in ("A", "B"):
            continue
        findings.append((item.lineno, item.fullname, rank, item.complexity))
    return findings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="+",
        help="Python files or directories to scan (recursive for dirs).",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help=(
            "Diagnostic flag: same measurement as the default, but the "
            "allowlist is IGNORED so every C+ function contributes to the "
            "exit-2 verdict. Use for honest auditing of deferred-debt "
            "remediation progress."
        ),
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "Path to a tracked-debt register listing C+ functions whose "
            "cyclomatic complexity is acknowledged + deferred. Format: one "
            "`<file_path>::<function_name>` per line; blank lines and "
            "`#` comments ignored. File path is POSIX-suffix-matched; "
            "function name is the bare name (no ClassName. prefix). "
            "Allowlisted findings are still printed so the gate stays "
            "honest; they just do not contribute to the exit-2 verdict. "
            "Removing an entry is the unit of remediation progress. See "
            "the plugin-god-class-remediation design record "
            "§9.D–§9.Z + Task #74."
        ),
    )
    return parser


def _scan_all(
    targets: list[Path], allowlist: frozenset[tuple[str, str]], raw: bool,
) -> list[tuple[Path, int, str, str, int, bool]]:
    """Run cc_visit over every target; emit per-finding lines.

    Returns the list of C+ findings as
    (path, lineno, fullname, rank, complexity, is_allowlisted).
    """
    findings: list[tuple[Path, int, str, str, int, bool]] = []
    for target in targets:
        for lineno, fullname, rank, complexity in _scan_file(target):
            bare_name = fullname.rsplit(".", 1)[-1]
            is_allowlisted = (
                False if raw else _finding_is_allowlisted(target, bare_name, allowlist)
            )
            marker = " [allowlisted]" if is_allowlisted else ""
            print(
                f"CC {rank} ({complexity}): {target}:{lineno} {fullname}{marker}"
            )
            findings.append((target, lineno, fullname, rank, complexity, is_allowlisted))
    return findings


def _report(
    findings: list[tuple[Path, int, str, str, int, bool]],
    target_count: int,
    allowlist_active: bool,
) -> int:
    if not findings:
        print(f"OK: {target_count} file(s) scanned, 0 cyclomatic-complexity violations.")
        return 0
    total = len(findings)
    blocking = [f for f in findings if not f[5]]
    if allowlist_active:
        allowlisted_count = total - len(blocking)
        print(
            f"\n{total} cyclomatic-complexity violation(s) "
            f"({allowlisted_count} allowlisted; {len(blocking)} still failing).",
            file=sys.stderr,
        )
        return 2 if blocking else 0
    print(
        f"\n{total} cyclomatic-complexity violation(s) across {target_count} file(s).",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        targets = _expand_targets(args.paths)
    except GateCrashError as crash:
        print(f"GATE-CRASH: {crash}", file=sys.stderr)
        return GATE_CRASH_EXIT
    if not targets:
        print("ERROR: no Python files to scan from given paths.", file=sys.stderr)
        return 64

    allowlist: frozenset[tuple[str, str]] = frozenset()
    allowlist_active = args.allowlist is not None and not args.raw
    if args.allowlist is not None:
        try:
            allowlist = _load_allowlist(args.allowlist)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 64

    try:
        findings = _scan_all(targets, allowlist, args.raw)
    except GateCrashError as crash:
        print(f"GATE-CRASH: {crash}", file=sys.stderr)
        print(
            "No verdict was produced. This is NOT a violation count: the "
            "analyser aborted, so this run measured nothing about the tree.",
            file=sys.stderr,
        )
        return GATE_CRASH_EXIT
    return _report(findings, len(targets), allowlist_active)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
