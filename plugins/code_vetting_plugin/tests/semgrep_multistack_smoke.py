"""semgrep_multistack_smoke.py — R7-2: semgrep is multi-stack + counts honestly (FT-D2 proper).

Two cultivar bugs made semgrep examine 0 files: the ruleset was hardcoded ``p/python`` and the
scan roots were the platform's quality-surface dirs (empty on any foreign tree). R7-2 selects the pack
by DETECTED STACK, scans a foreign target's whole tree with the curated walk-excludes, and reports
``files_examined`` from semgrep's own ``paths.scanned`` (the ground truth, not what we pointed it at).

Pins (all HERMETIC — no live registry hit; the design keeps the live TS positive-control to
build-verify only, so registry flake stays a runtime gap, never a gate red):
  * ``_semgrep_packs``: pack-per-stack, additive union, sorted-deterministic; no mapped stack → ().
  * ``_semgrep_scan_target_args``: self-vet → the quality-surface top-level dirs; a FOREIGN target →
    ``.`` plus a ``--exclude`` per curated walk-exclude dir (closes the materialized-junk residual).
  * ``_paths_scanned_count``: reads ``paths.scanned`` length; missing/malformed → 0.
  * ``scan_semgrep`` no-pack GAP: a no-mapped-stack tree (pure-Go/text) records an honest
    ``not_applicable: no semgrep ruleset mapped …`` (this path returns BEFORE any subprocess, so it
    is hermetic) — it never reads as a clean run.
  * ``_semgrep_findings``: a canned semgrep JSON entry maps to one SECURITY finding.

Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.models import Dimension
from code_vetting_plugin.scanners.sast import (  # noqa: PLC2701 — pin the internal pack/target/count logic
    _paths_scanned_count,
    _semgrep_findings,
    _semgrep_packs,
    _semgrep_scan_target_args,
    scan_semgrep,
)
from code_vetting_plugin.stacks import Stack
from code_vetting_plugin.targets import WALK_EXCLUDE_DIRS, TargetTree
from code_vetting_plugin.toolrun import tool_available

_CHECKS_RUN: list[str] = []

# The automake/Meson/CTest SKIP_RETURN_CODE convention, matching
# run_smokes.py's own _SKIP_EXIT_CODE. Undeclared-dependency audit:
# workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
_SKIP_EXIT_CODE = 77
_semgrep_check_skipped = False


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _check_pack_selection() -> None:
    _check("PYTHON -> p/python", _semgrep_packs(frozenset({Stack.PYTHON})) == ("p/python",), "")
    _check("TYPESCRIPT -> p/typescript", _semgrep_packs(frozenset({Stack.TYPESCRIPT})) == ("p/typescript",), "")
    _check("JAVASCRIPT -> p/javascript", _semgrep_packs(frozenset({Stack.JAVASCRIPT})) == ("p/javascript",), "")
    all_three = _semgrep_packs(frozenset({Stack.PYTHON, Stack.TYPESCRIPT, Stack.JAVASCRIPT}))
    _check("all stacks -> additive sorted union", all_three == ("p/javascript", "p/python", "p/typescript"), str(all_three))
    _check("no detected stack -> no packs", _semgrep_packs(frozenset()) == (), "")


def _check_scan_target_args(tmp: Path) -> None:
    self_tree = TargetTree(
        root=Path("/example"),
        tracked=("ananta/src/x.py", "plugins/foo/src/foo/a.py", "README.md"),
        enumeration="git",
        foreign=False,
    )
    self_args = _semgrep_scan_target_args(self_tree)
    _check("self-vet scans the quality-surface top-level dirs only", self_args == ["ananta", "plugins"], str(self_args))
    _check("self-vet passes no --exclude flags", "--exclude" not in self_args, str(self_args))

    ts_root = tmp / "foreign"
    ts_root.mkdir(parents=True)
    (ts_root / "src").mkdir()
    (ts_root / "src" / "app.ts").write_text("export const x=1\n", encoding="utf-8")
    foreign_tree = TargetTree.from_walk(ts_root)
    foreign_args = _semgrep_scan_target_args(foreign_tree)
    _check("foreign scans the whole tree ('.')", foreign_args[-1] == ".", str(foreign_args))
    _check("foreign excludes materialized junk (node_modules)", "--exclude" in foreign_args and "node_modules" in foreign_args, str(foreign_args))
    excluded = {foreign_args[i + 1] for i, tok in enumerate(foreign_args) if tok == "--exclude"}
    _check("foreign excludes EVERY curated walk-exclude dir", excluded == set(WALK_EXCLUDE_DIRS), str(sorted(excluded)))


def _check_paths_scanned_count() -> None:
    _check("paths.scanned length is the file count", _paths_scanned_count({"paths": {"scanned": ["a.ts", "b.ts", "c.ts"]}}) == 3, "")
    _check("no paths key -> 0", _paths_scanned_count({"results": []}) == 0, "")
    _check("malformed paths -> 0 (no crash)", _paths_scanned_count({"paths": "nope"}) == 0, "")
    _check("empty scanned -> 0 (the honest 'ran clean over nothing' signal)", _paths_scanned_count({"paths": {"scanned": []}}) == 0, "")


def _check_no_pack_gap(tmp: Path) -> None:
    go_root = tmp / "go"
    go_root.mkdir(parents=True)
    (go_root / "main.go").write_text("package main\n", encoding="utf-8")
    (go_root / "notes.txt").write_text("x\n", encoding="utf-8")
    tree = TargetTree.from_walk(go_root)
    cov = scan_semgrep(tree, "vr-r72").coverage
    # ran is False whether semgrep is installed (no-pack gap, returns before any subprocess) or
    # absent (not-installed gap) — never a clean run over an unmapped stack.
    _check("semgrep on a no-mapped-stack tree: ran=False", cov.ran is False, str(cov))
    global _semgrep_check_skipped
    if tool_available("semgrep"):
        _check(
            "semgrep no-pack gap names the unmapped stacks (hermetic — no subprocess)",
            (cov.gap_reason or "").startswith("not_applicable:") and "no semgrep ruleset mapped" in (cov.gap_reason or ""),
            str(cov.gap_reason),
        )
    else:
        print("  SKIP  semgrep no-pack gap-reason assertion: semgrep not on PATH")
        _semgrep_check_skipped = True


def _check_findings_parse() -> None:
    payload = {
        "results": [
            {
                "check_id": "typescript.react.security.audit.react-no-refs.react-no-refs",
                "path": "/x/src/app.tsx",
                "start": {"line": 12},
                "extra": {"message": "avoid direct ref access", "severity": "WARNING"},
            }
        ]
    }
    findings = _semgrep_findings(payload, Path("/x"), "vr-r72", "1.0")
    _check("one semgrep result -> one finding", len(findings) == 1, str(findings))
    _check("finding is a SECURITY dimension", findings[0].dimension is Dimension.SECURITY, str(findings[0].dimension))
    _check("finding pins the semgrep rule id", findings[0].constraint_violated.startswith("semgrep:typescript.react"), findings[0].constraint_violated)
    _check("finding path is relativized to the root", findings[0].file == "src/app.tsx", findings[0].file)


def main() -> int:
    try:
        _check_pack_selection()
        with tempfile.TemporaryDirectory() as tmp:
            _check_scan_target_args(Path(tmp))
        _check_paths_scanned_count()
        with tempfile.TemporaryDirectory() as tmp:
            _check_no_pack_gap(Path(tmp))
        _check_findings_parse()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"semgrep_multistack_smoke OK: {len(_CHECKS_RUN)} checks passed")
    if _semgrep_check_skipped:
        print('BORN_CLONE_SKIP_WITNESS={"reason":"ambient_executable_missing","executable":"semgrep"}')
        print(
            "SKIP: semgrep not on PATH -- the semgrep-specific gap-reason "
            "assertion disclosed a gap rather than running; every other "
            "check ran and passed."
        )
        return _SKIP_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
