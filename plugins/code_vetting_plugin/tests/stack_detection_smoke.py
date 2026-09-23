"""stack_detection_smoke.py — R7-1 stack detection + applicability spine.

Pins the spine that makes the L1 roster multi-ecosystem-aware without forking it:

  * ``detect_stacks``: enumeration-driven, deterministic — pure-python, TS, mixed, and
    pure-JS fixture trees map to the expected ``Stack`` sets (``.d.ts`` counts as TS; a
    root ``tsconfig.json`` counts even without ``.ts`` files).
  * ``ScannerSpec.__post_init__`` (R2): ``stacks`` is non-empty IFF applicability is STACK.
  * The R2 INTERSECTION gate: an eslint-like ``{TYPESCRIPT, JAVASCRIPT}`` scanner is
    applicable on a pure-JS tree while a tsc-like ``{TYPESCRIPT}`` scanner is not — a
    single-stack field would false-skip eslint on pure JS (the honesty failure R7 closes).
  * ``run_all``: a STACK scanner whose stacks miss the detected set skips execution and
    ledgers a distinct ``not_applicable:`` record (roster never forks, Y stays full);
    it executes when the stacks intersect. The solet's self-vet is ``{PYTHON}`` so the TS
    scanners self-skip with no self/foreign special-casing.
  * The report header carries a ``- **Stacks:** …`` provenance line.

Hermetic (tmp walk-trees; a no-op probe scanner; no tools). Run directly or via run_smokes.py.
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

import code_vetting_plugin.runner as runner_mod
from code_vetting_plugin.coverage import CoverageRecord, ScannerResult
from code_vetting_plugin.models import ContextProfile
from code_vetting_plugin.report import ReportRenderer
from code_vetting_plugin.run_record import RunTarget
from code_vetting_plugin.runner import Applicability, ScannerSpec, run_all
from code_vetting_plugin.stacks import Stack, detect_stacks, render_stacks
from code_vetting_plugin.targets import TargetTree

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _tree(root: Path, files: dict[str, str]) -> TargetTree:
    root.mkdir(parents=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return TargetTree.from_walk(root)


def _noop_scanner(tree: TargetTree, run_id: str) -> ScannerResult:
    del tree, run_id
    return ScannerResult(findings=[], coverage=CoverageRecord(scanner="ts-only-probe", ran=True, files_examined=1))


def _check_detect_stacks(base: Path) -> None:
    py = _tree(base / "py", {"mod.py": "A = 1\n", "pyproject.toml": "[project]\nname='x'\n"})
    _check("pure-python tree detects {PYTHON}", detect_stacks(py) == frozenset({Stack.PYTHON}), str(detect_stacks(py)))

    ts = _tree(base / "ts", {"src/app.tsx": "export const x=1\n", "src/t.d.ts": "export {}\n", "tsconfig.json": "{}\n"})
    _check("TS tree detects {TYPESCRIPT} (.tsx + .d.ts + tsconfig)", detect_stacks(ts) == frozenset({Stack.TYPESCRIPT}), str(detect_stacks(ts)))
    _check("typescript_files enumerates .tsx and .d.ts", set(ts.typescript_files()) == {"src/app.tsx", "src/t.d.ts"}, str(ts.typescript_files()))

    tsconfig_only = _tree(base / "tsc", {"tsconfig.json": "{}\n", "readme.md": "x\n"})
    _check("root tsconfig.json alone detects TYPESCRIPT", Stack.TYPESCRIPT in detect_stacks(tsconfig_only), str(detect_stacks(tsconfig_only)))

    mixed = _tree(base / "mix", {"api.py": "A=1\n", "web/app.ts": "export const y=2\n", "web/util.js": "export const z=3\n", "package-lock.json": "{}\n"})
    _check("mixed tree detects all three stacks", detect_stacks(mixed) == frozenset({Stack.PYTHON, Stack.TYPESCRIPT, Stack.JAVASCRIPT}), str(detect_stacks(mixed)))
    _check("npm_lockfiles enumerates package-lock.json", set(mixed.npm_lockfiles()) == {"package-lock.json"}, str(mixed.npm_lockfiles()))

    js = _tree(base / "js", {"index.mjs": "export const a=1\n", "lib.cjs": "module.exports={}\n", "package.json": "{}\n"})
    _check("pure-JS tree detects {JAVASCRIPT} only (no TS)", detect_stacks(js) == frozenset({Stack.JAVASCRIPT}), str(detect_stacks(js)))
    return None


def _check_scannerspec_r2() -> None:
    # Valid: STACK with stacks; UNIVERSAL/SELF_ONLY with none.
    ScannerSpec("ok-stack", _noop_scanner, Applicability.STACK, frozenset({Stack.TYPESCRIPT}))
    ScannerSpec("ok-univ", _noop_scanner, Applicability.UNIVERSAL)
    for label, bad in (
        ("STACK without stacks", lambda: ScannerSpec("bad", _noop_scanner, Applicability.STACK)),
        ("UNIVERSAL with stray stacks", lambda: ScannerSpec("bad", _noop_scanner, Applicability.UNIVERSAL, frozenset({Stack.TYPESCRIPT}))),
    ):
        _CHECKS_RUN.append(f"R2 __post_init__ rejects a {label}")
        try:
            bad()
        except ValueError:
            continue
        raise SmokeFailureError(f"R2 __post_init__ rejects a {label}: expected ValueError, none raised")


def _check_intersection_gate(base: Path) -> None:
    js = _tree(base / "js2", {"index.js": "const a=1\n"})
    detected = detect_stacks(js)
    tsc_like = ScannerSpec("tsc-like", _noop_scanner, Applicability.STACK, frozenset({Stack.TYPESCRIPT}))
    eslint_like = ScannerSpec("eslint-like", _noop_scanner, Applicability.STACK, frozenset({Stack.TYPESCRIPT, Stack.JAVASCRIPT}))
    _check("R2: eslint-like {TS,JS} is APPLICABLE on a pure-JS tree", bool(eslint_like.stacks & detected), str(detected))
    _check("R2: tsc-like {TS} is NOT applicable on a pure-JS tree", not (tsc_like.stacks & detected), str(detected))


def _check_run_all_stack_branch(base: Path) -> None:
    py = _tree(base / "py2", {"mod.py": "A=1\n"})
    ts = _tree(base / "ts2", {"app.ts": "export const x=1\n"})
    probe = ScannerSpec("ts-only-probe", _noop_scanner, Applicability.STACK, frozenset({Stack.TYPESCRIPT}))
    original = runner_mod.SCANNERS
    try:
        runner_mod.SCANNERS = (probe,)
        _, py_cov, _ = run_all(py, "vr-r71")
        _, ts_cov, _ = run_all(ts, "vr-r71")
    finally:
        runner_mod.SCANNERS = original
    _check("run_all: full roster preserved (1 record for the 1-spec probe roster)", len(py_cov) == 1 and len(ts_cov) == 1, f"{len(py_cov)},{len(ts_cov)}")
    _check("run_all: STACK scanner not_applicable on a no-matching-stack tree", py_cov[0].ran is False and (py_cov[0].gap_reason or "").startswith("not_applicable:") and "typescript" in (py_cov[0].gap_reason or ""), str(py_cov[0]))
    _check("run_all: STACK scanner EXECUTES when its stack is present", ts_cov[0].ran is True, str(ts_cov[0]))
    # The real roster is unchanged after restore (non-vacuous guard against a leaked monkeypatch).
    _check("run_all: real roster restored + still declares 20 scanners", len(runner_mod.SCANNERS) == 20, str(len(runner_mod.SCANNERS)))


def _check_report_header() -> None:
    report = ReportRenderer().render(
        run_id="vr-r71", target=RunTarget(repo="example", ref="deadbeef", scope="s"),
        context_profile=ContextProfile.PRODUCTION, generated_at="t", findings=[], coverage=[],
        stacks=render_stacks(frozenset({Stack.PYTHON, Stack.TYPESCRIPT})),
    )
    _check("report header renders the Stacks provenance line", "- **Stacks:** python, typescript" in report, report[:400])
    # No stacks passed -> no Stacks line (byte-compatible for callers that don't detect stacks).
    plain = ReportRenderer().render(
        run_id="vr-r71", target=RunTarget(repo="example", ref="deadbeef", scope="s"),
        context_profile=ContextProfile.PRODUCTION, generated_at="t", findings=[], coverage=[],
    )
    _check("report header omits the Stacks line when stacks is None", "**Stacks:**" not in plain, plain[:400])


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _check_detect_stacks(Path(tmp))
        _check_scannerspec_r2()
        with tempfile.TemporaryDirectory() as tmp:
            _check_intersection_gate(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_run_all_stack_branch(Path(tmp))
        _check_report_header()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"stack_detection_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
