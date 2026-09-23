#!/usr/bin/env python3
"""Radon maintainability-index gate, coherence-aware edition.

Wraps `radon mi` with the same coherence-aware framing as the reformed
`quality_gates/god_class_check.py`: before measuring, strips `@platform_process`-
decorated method bodies + decorator extents + the contract-mandated
`get_edge_process_definitions` body from the AST, so the MI gate measures
the **non-process structural surface** rather than penalizing coherent
plugin growth.

Per the plugin-god-class-remediation design record §5–§6 (dev-checkout
workbench — not part of the shipped tree): the platform's chosen
architecture is "larger plugins organized
via submodules, bounded by coherence not method count." Textbook MI
penalizes raw file size (radon's MI formula has a `-16.2·ln(L)` term);
plugins that concretize use into `@platform_process` methods accumulate
that LOC by design. The reform parallels the god-class gate.

Default behavior:
  - Replace every `@platform_process`-decorated method's body with `pass`
    and drop its decorators (AST manipulation via `ast.unparse`).
  - Replace `get_edge_process_definitions`'s body with `pass`.
  - Compute MI on the rewritten source.

The `--raw` flag preserves the textbook counting (no stripping) for
diagnostic A/B comparisons.

Exit codes:
  0 — every file ranks A or B
  2 — one or more files rank C (or worse)
  64 — usage error (bad arguments / non-existent paths)
  70 — GATE CRASH: the analyser raised, so no verdict exists for the tree.
       A crash is NOT a violation count; see `gate_scope.GateCrashError`.

Output: one line per file `<path> - <rank> (<value>)`. Failing files are
also summarized to stderr.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from gate_scope import (
    GATE_CRASH_EXIT,
    GateCrashError,
    repo_python_files,
)
from radon.metrics import mi_rank, mi_visit

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402


def _has_platform_process_decorator(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """True if the method carries an `@platform_process` decorator (any form).

    Matches the recognition logic in `god_class_check.py` so both gates
    treat the same surface as platform-process veneer.
    """
    for dec in method.decorator_list:
        target: ast.expr = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id == "platform_process":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "platform_process":
            return True
    return False


def _strip_platform_process_surface(source: str) -> str:
    """Return `source` with every @platform_process method + get_edge_process_definitions
    body replaced by `pass` and their decorators dropped.

    The rewrite preserves the class structure so radon can still measure
    the non-process structural surface. Comments and module docstrings are
    not preserved (`ast.unparse` rewrites from the tree), but radon's MI
    formula does not differentiate comments from code in a way that affects
    the result for this gate's purpose.
    """
    tree = ast.parse(source)
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _has_platform_process_decorator(stmt) or stmt.name == "get_edge_process_definitions":
                stmt.decorator_list = []
                stmt.body = [ast.Pass()]
    return ast.unparse(tree)


def _measure(path: Path, raw: bool) -> tuple[float, str] | None:
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"WARN: cannot read {path}: {exc}", file=sys.stderr)
        return None
    if raw:
        source = original
    else:
        try:
            source = _strip_platform_process_surface(original)
        except SyntaxError as exc:
            print(f"WARN: syntax error in {path}: {exc}", file=sys.stderr)
            return None
        except RecursionError:
            # Vendored libraries (sympy, Cython, etc.) sometimes have deeply
            # nested AST expressions that overflow `ast.unparse`'s recursive
            # writer. Fall back to raw counting for those files — the
            # @platform_process strip is irrelevant for vendored code anyway.
            print(
                f"WARN: AST unparse recursion exceeded on {path}; "
                f"measuring raw source",
                file=sys.stderr,
            )
            source = original
    try:
        mi = mi_visit(source, multi=True)
    except SyntaxError as exc:
        print(f"WARN: post-strip syntax error in {path}: {exc}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001 — any analyser failure is a non-verdict
        raise GateCrashError(path, f"{type(exc).__name__} in radon mi_visit: {exc}") from exc
    return mi, mi_rank(mi)


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


def _load_allowlist(path: Path) -> frozenset[str]:
    """Read a file-path allowlist file under the mandatory owner/reason/expires
    schema (D-3-structural, iss_23fa51b5).

    Paths are matched as POSIX-string suffixes against the candidate
    file's resolved path — entries can be repo-relative
    (`plugins/foo/src/foo/plugin.py`) or absolute. The allowlist is a
    tracked-debt register documenting files whose maintainability fix
    is deferred (per the plugin-god-class-remediation design record
    §9.D–§9.Z + Task #74, dev-checkout workbench — not part of the
    shipped tree). Allowlisted findings are still printed in
    the report but do NOT contribute to the exit-2 verdict.
    """
    return frozenset(_load_tagged_allowlist(path))


def _path_is_allowlisted(path: Path, allowlist: frozenset[str]) -> bool:
    """True if `path` ends with any allowlist entry's POSIX suffix."""
    candidate = path.as_posix()
    return any(candidate.endswith(entry) for entry in allowlist)


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
            "Restore the textbook counting model: skip the @platform_process "
            "stripping pass and measure MI on the unmodified source. Use for "
            "diagnostic A/B comparison; the SKILL gate uses the default "
            "coherence-aware counting."
        ),
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "Path to a tracked-debt register file listing file paths whose "
            "MI rank C is acknowledged + deferred (one path per line; blank "
            "lines and `#` comments ignored). Entries are POSIX-suffix-matched "
            "against each candidate's path. Allowlisted findings are still "
            "printed so the gate stays honest; they just do not contribute "
            "to the exit-2 verdict. Removing an entry from the allowlist is "
            "the unit of remediation progress. See the "
            "plugin-god-class-remediation design record "
            "§9.D–§9.Z + Task #74."
        ),
    )
    return parser


def _measure_targets(
    targets: list[Path], raw: bool, allowlist: frozenset[str],
) -> list[tuple[Path, float, str, bool]]:
    """Measure each target and emit per-file output; return the C+ rows."""
    failures: list[tuple[Path, float, str, bool]] = []
    for target in targets:
        result = _measure(target, raw)
        if result is None:
            continue
        mi, rank = result
        is_allowlisted = _path_is_allowlisted(target, allowlist)
        marker = " [allowlisted]" if is_allowlisted else ""
        print(f"{target} - {rank} ({mi:.2f}){marker}")
        if rank not in ("A", "B"):
            failures.append((target, mi, rank, is_allowlisted))
    return failures


def _report_failures(
    failures: list[tuple[Path, float, str, bool]],
    target_count: int,
    allowlist_active: bool,
) -> int:
    """Print the failure summary + per-file lines; return the exit code."""
    total = len(failures)
    blocking = [f for f in failures if not f[3]]
    if allowlist_active:
        allowlisted_count = total - len(blocking)
        print(
            f"\n{total} file(s) rank C or worse "
            f"({allowlisted_count} allowlisted; {len(blocking)} still failing).",
            file=sys.stderr,
        )
        for path, mi, rank, is_allowlisted in failures:
            marker = " [allowlisted]" if is_allowlisted else ""
            print(f"  {path} - {rank} ({mi:.2f}){marker}", file=sys.stderr)
        return 2 if blocking else 0
    print(
        f"\n{total} file(s) rank C or worse across {target_count} scanned:",
        file=sys.stderr,
    )
    for path, mi, rank, _is_allowlisted in failures:
        print(f"  {path} - {rank} ({mi:.2f})", file=sys.stderr)
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


    allowlist: frozenset[str] = frozenset()
    allowlist_active = args.allowlist is not None
    if allowlist_active:
        try:
            allowlist = _load_allowlist(args.allowlist)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 64

    try:
        failures = _measure_targets(targets, args.raw, allowlist)
    except GateCrashError as crash:
        print(f"GATE-CRASH: {crash}", file=sys.stderr)
        print(
            "No verdict was produced. This is NOT a violation count: the "
            "analyser aborted, so this run measured nothing about the tree.",
            file=sys.stderr,
        )
        return GATE_CRASH_EXIT
    if not failures:
        print(f"\nOK: {len(targets)} file(s) scanned, 0 maintainability violations.")
        return 0
    return _report_failures(failures, len(targets), allowlist_active)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
