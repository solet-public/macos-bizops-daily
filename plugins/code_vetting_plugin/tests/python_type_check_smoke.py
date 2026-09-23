"""python_type_check_smoke.py — R9-E: foreign-Python type-check scanner (pyright + mypy, opt-in tier).

Pins the R7-4-mirror contract. HERMETIC (canned checker output; crafted trees; the three gap classes
return BEFORE any tool touch) + absence-tolerant for the one live end-to-end (uses the host pyright/mypy
only when present):

  * Roster + spec: ONE 'python_type_check' slot (STACK/{PYTHON}, executes_target_code=True); roster is 19.
  * THREE DISTINCT gap classes, red-first (all pre-tool-touch, hermetic): no config → no-contract
    not_applicable; config + flag unset → opt-in gap; config + flag set + no {.venv,venv} → materialization gap.
  * Parse: canned ``pyright --outputjson`` (0-indexed lines +1, error→MEDIUM/warning→LOW, constraint
    ``pyright:<rule>``) and canned mypy text (``mypy:<code>``, error→MEDIUM/note→LOW). Diagnostics anchored
    inside the env/site-packages are DROPPED with a count. The checker version rides the provenance.
  * Flood cap: ``_MAX_TOOL_FINDINGS`` bounds the per-checker findings.
  * Self-suppression: a self-vet emits ZERO finding rows (the pyright --strict gate is the sole self
    authority, R8 §C); a foreign target emits (live end-to-end, absence-tolerant on the host checkers).

Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin import toolrun
from code_vetting_plugin.models import Dimension, Severity
from code_vetting_plugin.report import DEFAULT_ZERO_FP_DIMENSIONS
from code_vetting_plugin.runner import SCANNERS, Applicability
from code_vetting_plugin.scanners.python_type_check import (  # noqa: PLC2701 — pin the internal parse/gate logic
    _MAX_TOOL_FINDINGS,
    _in_venv_dir,
    _mypy_findings,
    _mypy_scope_args,
    _pyright_findings,
    scan,
)
from code_vetting_plugin.stacks import Stack
from code_vetting_plugin.targets import TargetTree
from code_vetting_plugin.toolrun import tool_available

_CHECKS_RUN: list[str] = []
_PYPROJECT = '[tool.pyright]\ninclude = ["src"]\n\n[tool.mypy]\nfiles = ["src"]\n'
_BAD = "def add(a: int, b: int) -> int:\n    return a + b\n\n\nx: str = add(1, 2)\n"

# The automake/Meson/CTest SKIP_RETURN_CODE convention, matching
# run_smokes.py's own _SKIP_EXIT_CODE. This file has TWO independent
# tool-availability guards, not one, and they are genuinely different
# compound conditions -- deliberately NOT unified into a single "skip when
# both are absent" rule, since that would be wrong for one of the two:
#   1. _check_self_vs_foreign's FOREIGN block: `if tool_available("pyright")
#      or tool_available("mypy")` -- an OR. Its skip condition (the negation,
#      De Morgan's) is "BOTH absent." When exactly one is present, this
#      block RUNS and checks generically against whichever tool actually
#      produced findings -- correct as written, nothing to change here.
#   2. _check_mypy_scope_live: `if not tool_available("mypy")` -- mypy
#      SPECIFICALLY, unconditional on pyright. RIDER-2's live probe tests
#      mypy's own files-less-config scope-enumeration behavior; pyright
#      being present is not a substitute for that, so this correctly skips
#      whenever mypy alone is absent, even with pyright present.
# The overall smoke reports a skip if EITHER site actually skipped --
# each represents real coverage that didn't run, and collapsing them into
# one "only if both tools are absent" condition would be wrong for site 2.
# Undeclared-dependency audit:
# workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
_SKIP_EXIT_CODE = 77
_type_check_coverage_skipped = False


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _fixture(root: Path, *, config: bool, venv: bool, foreign: bool = True) -> TargetTree:
    (root / "src").mkdir(parents=True)
    (root / "src" / "bad.py").write_text(_BAD, encoding="utf-8")
    if config:
        (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    if venv:
        (root / ".venv" / "bin").mkdir(parents=True)
    if foreign:
        return TargetTree.from_walk(root)
    tracked = ("src/bad.py", *(("pyproject.toml",) if config else ()))
    return TargetTree(root=root, tracked=tracked, enumeration="git", foreign=False)


def _reason(tree: TargetTree, *, flag: bool) -> str:
    return scan(tree, "vr-r9e", execute_target_toolchain=flag).coverage.gap_reason or ""


def _check_roster() -> None:
    spec = next((s for s in SCANNERS if s.name == "python_type_check"), None)
    _check("roster is 20 (python_type_check + rulebook_sync added)", len(SCANNERS) == 20, str(len(SCANNERS)))
    _check(
        "python_type_check: STACK/{PYTHON}, executes_target_code (opt-in tier, R7-4 mirror)",
        spec is not None and spec.applicability is Applicability.STACK and spec.stacks == frozenset({Stack.PYTHON}) and spec.executes_target_code,
        str(spec),
    )
    _check("TYPE_COVERAGE is zero-FP-promoted (findings render without a verdict hack)", Dimension.TYPE_COVERAGE in DEFAULT_ZERO_FP_DIMENSIONS, "")


def _check_gap_classes(base: Path) -> None:
    # All three return BEFORE any checker is touched → hermetic + deterministic.
    no_config = scan(_fixture(base / "nc", config=False, venv=False), "vr", execute_target_toolchain=True).coverage
    _check("NO-CONTRACT gap: no config → not_applicable, ran=False", no_config.ran is False and (no_config.gap_reason or "").startswith("not_applicable: no type-check configuration"), str(no_config))
    optin = _reason(_fixture(base / "oi", config=True, venv=True), flag=False)
    _check("OPT-IN gap: config present + flag unset", "not enabled" in optin and "opt-in" in optin, optin)
    materialization = _reason(_fixture(base / "mz", config=True, venv=False), flag=True)
    _check("MATERIALIZATION gap: config + flag set + no env", "no environment" in materialization and "never creates a venv" in materialization, materialization)


def _pyright_json(diagnostics: list[dict[str, Any]]) -> str:
    return json.dumps({"generalDiagnostics": diagnostics})


def _check_pyright_parse() -> None:
    payload = _pyright_json([
        {"file": "/t/src/a.py", "severity": "error", "rule": "reportGeneralTypeIssues", "message": "bad\nassign", "range": {"start": {"line": 4, "character": 0}}},
        {"file": "/t/src/a.py", "severity": "warning", "rule": "reportUnusedVariable", "message": "unused", "range": {"start": {"line": 9, "character": 0}}},
        {"file": "/t/.venv/lib/x.py", "severity": "error", "rule": "reportX", "message": "env", "range": {"start": {"line": 0, "character": 0}}},
    ])
    findings, dropped = _pyright_findings(payload, Path("/t"), ".venv", "vr", "pyright 1.1.409")
    _check("pyright parse: 2 target findings (env-anchored dropped)", len(findings) == 2, str(findings))
    _check("pyright parse: env-anchored drop counted", dropped == 1, str(dropped))
    by_rule = {f.constraint_violated: f for f in findings}
    _check("pyright: error → MEDIUM, 0-indexed line +1, message newline-flattened", by_rule["pyright:reportGeneralTypeIssues"].severity is Severity.MEDIUM and by_rule["pyright:reportGeneralTypeIssues"].line == 5 and "\n" not in by_rule["pyright:reportGeneralTypeIssues"].evidence, str(by_rule["pyright:reportGeneralTypeIssues"]))
    _check("pyright: warning → LOW", by_rule["pyright:reportUnusedVariable"].severity is Severity.LOW, "")
    _check("pyright: TYPE_COVERAGE dim + version provenance", all(f.dimension is Dimension.TYPE_COVERAGE for f in findings) and by_rule["pyright:reportGeneralTypeIssues"].provenance.tool_version == "pyright 1.1.409", "")


def _check_mypy_parse() -> None:
    stdout = (
        "src/a.py:5: error: Incompatible types in assignment [assignment]\n"
        "src/a.py:9: note: revealed type is X [misc]\n"
        ".venv/lib/x.py:1: error: env error [foo]\n"
        "not a diagnostic line\n"
    )
    findings, dropped = _mypy_findings(stdout, Path("/t"), ".venv", "vr", "mypy 2.1.0")
    _check("mypy parse: 2 target findings (env-anchored dropped)", len(findings) == 2, str(findings))
    _check("mypy parse: env-anchored drop counted", dropped == 1, str(dropped))
    by_rule = {f.constraint_violated: f for f in findings}
    _check("mypy: error → MEDIUM, code extracted", "mypy:assignment" in by_rule and by_rule["mypy:assignment"].severity is Severity.MEDIUM and by_rule["mypy:assignment"].line == 5, str(by_rule))
    _check("mypy: note → LOW", by_rule["mypy:misc"].severity is Severity.LOW, "")
    _check("mypy: TYPE_COVERAGE dim + version provenance", all(f.dimension is Dimension.TYPE_COVERAGE for f in findings) and by_rule["mypy:assignment"].provenance.tool_version == "mypy 2.1.0", "")


def _check_flood_cap() -> None:
    diagnostics = [
        {"file": f"/t/src/f{i}.py", "severity": "error", "rule": "reportX", "message": "m", "range": {"start": {"line": i, "character": 0}}}
        for i in range(_MAX_TOOL_FINDINGS + 5)
    ]
    findings, _ = _pyright_findings(_pyright_json(diagnostics), Path("/t"), ".venv", "vr", "v")
    _check("flood cap: parse yields all, the slice bounds to _MAX_TOOL_FINDINGS", len(findings) == _MAX_TOOL_FINDINGS + 5 and len(findings[:_MAX_TOOL_FINDINGS]) == _MAX_TOOL_FINDINGS, str(len(findings)))


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _check_failed_shim_is_gap(base: Path) -> None:
    """A binary that qualifies (answers --version) but fails the real invocation is incomplete
    coverage, never clean. The shim must succeed unconditionally on --version: qualify_tool's own
    probe (toolrun.py) runs from an arbitrary cwd, never the target's, so a shim conditioned on cwd
    would be misclassified as globally unusable at qualification and never reach the target-cwd-aware
    checker invocation this check exists to exercise."""
    fake_bin = base / "fake-bin"
    fake_bin.mkdir()
    target = base / "shim-target"
    (target / "src").mkdir(parents=True)
    (target / ".venv" / "bin").mkdir(parents=True)
    (target / "pyproject.toml").write_text('[tool.mypy]\nfiles = ["src"]\n', encoding="utf-8")
    (target / "src" / "bad.py").write_text("x: str = 1\n", encoding="utf-8")
    _write_executable(
        fake_bin / "mypy",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf '%s\\n' 'mypy 9.9.9'\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' 'mypy: internal error: corrupt cache' >&2\n"
        "exit 127\n",
    )
    prior_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}:{prior_path}"
    # A prior check's real-host qualification of "mypy" (e.g. RIDER-2, absent on a host with no
    # ambient mypy) is cached for the process lifetime (toolrun._TOOL_QUALIFICATIONS). Left
    # uncleared, that stale verdict shadows this shim on PATH and the scan never invokes it —
    # see l1_scanners_smoke.py for the same PATH-mutation-needs-a-cache-clear pattern.
    toolrun._TOOL_QUALIFICATIONS.clear()  # noqa: SLF001
    try:
        result = scan(TargetTree.from_walk(target), "vr-typecheck-shim-gap", execute_target_toolchain=True)
    finally:
        os.environ["PATH"] = prior_path
        toolrun._TOOL_QUALIFICATIONS.clear()  # noqa: SLF001
    reason = result.coverage.gap_reason or ""
    _check("failed pyenv shim marks coverage ran=False", result.coverage.ran is False, str(result.coverage))
    _check("exit 127 is disclosed", "exited 127" in reason, reason)
    _check("failed checker never reports zero diagnostics", "0 diagnostic(s)" not in reason, reason)


def _check_diagnostic_exit_is_valid(base: Path) -> None:
    """Exit 1 means findings for both supported checkers; do not reject it blindly."""
    target = base / "diagnostic-target"
    (target / "src").mkdir(parents=True)
    local_bin = target / ".venv" / "bin"
    local_bin.mkdir(parents=True)
    (target / "pyproject.toml").write_text('[tool.pyright]\ninclude = ["src"]\n', encoding="utf-8")
    (target / "src" / "bad.py").write_text("x: str = 1\n", encoding="utf-8")
    payload = _pyright_json([
        {
            "file": str(target / "src" / "bad.py"),
            "severity": "error",
            "rule": "reportAssignmentType",
            "message": "Type int is not assignable to str",
            "range": {"start": {"line": 0, "character": 0}},
        }
    ])
    _write_executable(
        local_bin / "pyright",
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf '%s\\n' 'pyright 9.9.9'\n"
        "  exit 0\n"
        "fi\n"
        f"printf '%s\\n' '{payload}'\n"
        "exit 1\n",
    )
    result = scan(TargetTree.from_walk(target), "vr-diagnostic", execute_target_toolchain=True)
    _check("diagnostic exit 1 completes coverage", result.coverage.ran is True, str(result.coverage))
    _check("diagnostic exit 1 emits its finding", len(result.findings) == 1 and result.findings[0].constraint_violated == "pyright:reportAssignmentType", str(result.findings))


def _check_self_vs_foreign(base: Path) -> None:
    # Self-suppress is deterministic (emitted = [] on a self-vet regardless of what the checkers found).
    self_result = scan(_fixture(base / "self", config=True, venv=True, foreign=False), "vr", execute_target_toolchain=True)
    all_available = tool_available("pyright") and tool_available("mypy")
    _check(
        "SELF: finding rows SUPPRESSED (the --strict gate is sole self authority)",
        self_result.findings == [] and self_result.coverage.ran is all_available,
        str(self_result.coverage),
    )
    # Foreign emit + version/env disclosure — absence-tolerant on the host checkers.
    global _type_check_coverage_skipped
    if tool_available("pyright") or tool_available("mypy"):
        foreign = scan(_fixture(base / "fgn", config=True, venv=True, foreign=True), "vr", execute_target_toolchain=True)
        _check("FOREIGN: emits TYPE_COVERAGE findings when a host checker is present", any(f.dimension is Dimension.TYPE_COVERAGE for f in foreign.findings), str([f.constraint_violated for f in foreign.findings]))
        _check("FOREIGN: coverage discloses the env + per-checker version", "env=.venv" in (foreign.coverage.gap_reason or ""), foreign.coverage.gap_reason or "")
    else:
        print("  SKIP  FOREIGN checks: neither pyright nor mypy on PATH (self-suppress check above still ran)")
        _type_check_coverage_skipped = True


def _filesless_scope_tree(base: Path) -> TargetTree:
    """A foreign tree whose mypy config declares NO files/packages (+ a .venv env dir + a planted error)."""
    filesless = base / "fl"
    (filesless / "src").mkdir(parents=True)
    (filesless / "src" / "bad.py").write_text("x: str = 1\n", encoding="utf-8")
    (filesless / "pyproject.toml").write_text("[tool.pyright]\ninclude = [\"src\"]\n\n[tool.mypy]\nignore_missing_imports = true\n", encoding="utf-8")
    (filesless / ".venv" / "bin").mkdir(parents=True)
    return TargetTree.from_walk(filesless)


def _check_mypy_scope_live(fl_tree: TargetTree) -> None:
    """RIDER-2 live probe (absence-tolerant): a files-less config + host mypy catches the planted error."""
    global _type_check_coverage_skipped
    if not tool_available("mypy"):
        print("  SKIP  RIDER-2 live probe: mypy not on PATH (pyright presence is not a substitute)")
        _type_check_coverage_skipped = True
        return
    result = scan(fl_tree, "vr-rider", execute_target_toolchain=True)
    caught = any(f.constraint_violated.startswith("mypy:") for f in result.findings)
    _check("RIDER-2: enumerated scope catches the planted error under a files-less config (live)", caught and "enumerated" in (result.coverage.gap_reason or ""), result.coverage.gap_reason or "")


def _check_mypy_scope_rider(base: Path) -> None:
    """RIDER-2: the config is 100% the rules source, but mypy needs a check target — provide the
    enumerated *.py (minus env dirs) ONLY when the config declares no files/packages; a declared scope wins."""
    _check("RIDER-2: env-dir files excluded from the enumerated set", _in_venv_dir(".venv/lib/x.py") and _in_venv_dir("venv/y.py") and not _in_venv_dir("src/a.py"), "")
    fl_tree = _filesless_scope_tree(base)
    args, note = _mypy_scope_args(fl_tree)
    _check("RIDER-2: files-less config → enumerated scope + provenance string", len(args) >= 1 and note == "scope: enumerated — config declares no files/packages", note)
    _check("RIDER-2: the enumerated set excludes the .venv env dir", not any(".venv" in arg for arg in args), str(args))
    declared = base / "dc"
    (declared / "src").mkdir(parents=True)
    (declared / "pyproject.toml").write_text("[tool.mypy]\nfiles = [\"src\"]\n", encoding="utf-8")
    dargs, dnote = _mypy_scope_args(TargetTree.from_walk(declared))
    _check("RIDER-2: a DECLARED scope WINS (no enumeration)", dargs == [] and dnote == "scope: config-declared", dnote)
    _check_mypy_scope_live(fl_tree)


def main() -> int:
    try:
        _check_roster()
        with tempfile.TemporaryDirectory() as tmp:
            _check_mypy_scope_rider(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_gap_classes(Path(tmp))
        _check_pyright_parse()
        _check_mypy_parse()
        _check_flood_cap()
        with tempfile.TemporaryDirectory() as tmp:
            _check_failed_shim_is_gap(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_diagnostic_exit_is_valid(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_self_vs_foreign(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"python_type_check_smoke OK: {len(_CHECKS_RUN)} checks passed")
    if _type_check_coverage_skipped:
        print(
            "SKIP: at least one host type-checker was absent -- some live "
            "coverage disclosed a gap rather than running; every other "
            "check ran and passed."
        )
        return _SKIP_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
