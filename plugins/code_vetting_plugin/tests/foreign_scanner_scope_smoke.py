"""foreign_scanner_scope_smoke.py — FT-1.1 defects 1 + 2 (foreign scanner scoping).

The first LIVE foreign vet (cultivar, a TS tree) surfaced two attribution/honesty bugs
the deterministic scanners must not have on a FOREIGN target:

  * DEFECT 1 (deps) — pip-audit, run with no manifest, audits the ACTIVE environment;
    on a foreign scan that is THIS engine's own venv, so its CVEs get mis-attributed to
    the target. A foreign target must NEVER trigger the environment-audit mode:
    no python dependency manifest -> ``not_applicable``; a manifest present -> a deferred
    coverage gap (R7-3 narrowed it: osv-scanner now covers lockfile-pinned foreign deps, so only
    UNPINNED manifests remain a Phase-2 foreign-deps gap).
  * DEFECT 2 (sast) — bandit's no-targets branch and semgrep both reported
    ``ran=True, files_examined=0`` on a no-python target, which reads as "ran clean" when
    they in fact examined nothing. A no-python quality-surface -> an honest coverage GAP.

Both fixes are gated on an EMPTY python surface, so a self-vet (always python-carrying)
is byte-unaffected — proven here by the positive python-tree cases.

Run directly: ``.venv/bin/python3 plugins/code_vetting_plugin/tests/foreign_scanner_scope_smoke.py``.
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

from code_vetting_plugin.scanners.deps import (
    _pip_audit_foreign_gap,  # noqa: PLC2701 — pin the tool-independent gap logic
)
from code_vetting_plugin.scanners.sast import scan_bandit
from code_vetting_plugin.targets import TargetTree
from code_vetting_plugin.toolrun import tool_available

_CHECKS_RUN: list[str] = []

# The automake/Meson/CTest SKIP_RETURN_CODE convention, matching
# run_smokes.py's own _SKIP_EXIT_CODE -- set when bandit is absent and the
# two bandit-specific assertions below are skipped (the bandit-independent
# structural assertions in the same functions always run regardless).
# Undeclared-dependency audit:
# workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
_SKIP_EXIT_CODE = 77
_bandit_checks_skipped = False


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _write(root: Path, rel: str, text: str = "x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _foreign_ts_tree(root: Path) -> TargetTree:
    """A pure TS/JS foreign target: no python, no python dependency manifest."""
    root.mkdir(parents=True)
    _write(root, "src/app.tsx", "export const x = 1;\n")
    _write(root, "src/util.ts", "export const y = 2;\n")
    _write(root, "package.json", "{}\n")
    return TargetTree.from_walk(root)


def _foreign_python_tree(root: Path) -> TargetTree:
    """A foreign target carrying python source under a quality-surface path + a manifest."""
    root.mkdir(parents=True)
    _write(root, "plugins/foo/src/foo/mod.py", "A = 1\n")
    _write(root, "pyproject.toml", "[project]\nname = 'foo'\n")
    return TargetTree.from_walk(root)


def _check_pip_audit_defect1(ts_root: Path, py_root: Path) -> None:
    ts_tree = _foreign_ts_tree(ts_root)
    _check(
        "foreign TS tree carries NO python dependency manifest",
        ts_tree.python_dependency_manifests() == (),
        str(ts_tree.python_dependency_manifests()),
    )
    ts_gap = _pip_audit_foreign_gap(ts_tree).coverage
    _check("pip-audit foreign no-manifest: ran=False", ts_gap.ran is False, str(ts_gap))
    _check(
        "pip-audit foreign no-manifest: not_applicable reason",
        (ts_gap.gap_reason or "").startswith("not_applicable:"),
        str(ts_gap.gap_reason),
    )

    py_tree = _foreign_python_tree(py_root)
    _check(
        "foreign python tree DETECTS its pyproject manifest",
        "pyproject.toml" in py_tree.python_dependency_manifests(),
        str(py_tree.python_dependency_manifests()),
    )
    py_gap = _pip_audit_foreign_gap(py_tree).coverage
    _check("pip-audit foreign with-manifest: ran=False", py_gap.ran is False, str(py_gap))
    _check(
        "pip-audit foreign with-manifest: narrowed deferred gap (R7-3), NOT not_applicable",
        # R7-3 narrowed the reason: osv-scanner now covers lockfile-pinned foreign deps, so only
        # UNPINNED manifests remain a Phase-2 gap (was the blanket 'hermetic -r/lock audit' adapter).
        "osv-scanner" in (py_gap.gap_reason or "")
        and "UNPINNED" in (py_gap.gap_reason or "")
        and not (py_gap.gap_reason or "").startswith("not_applicable:"),
        str(py_gap.gap_reason),
    )


def _check_sast_defect2(ts_root: Path, py_root: Path) -> None:
    ts_tree = _foreign_ts_tree(ts_root)
    _check(
        "foreign TS tree has an EMPTY python quality-surface (the gap precondition)",
        ts_tree.quality_surface_python() == (),
        str(ts_tree.quality_surface_python()),
    )
    # bandit stays no-python-gated: ran is False on a no-python foreign target regardless of
    # tool presence (a not-installed gap OR the no-python gap) — never a clean run over nothing.
    # (semgrep's FT-1.1 no-python gap is SUPERSEDED by R7-2's stack-pack selection — a foreign
    # TS tree now selects p/typescript, so semgrep's behavior is pinned hermetically in
    # semgrep_multistack_smoke.py, not here where a live registry hit would be non-deterministic.)
    global _bandit_checks_skipped
    bandit_cov = scan_bandit(ts_tree, "vr-ft11").coverage
    _check("bandit on foreign TS: ran=False (never 'ran clean' over 0 files)", bandit_cov.ran is False, str(bandit_cov))
    if tool_available("bandit"):
        _check(
            "bandit on foreign TS (installed): not_applicable no-python gap",
            (bandit_cov.gap_reason or "").startswith("not_applicable:") and "bandit examined 0 files" in (bandit_cov.gap_reason or ""),
            str(bandit_cov.gap_reason),
        )
    else:
        print("  SKIP  bandit-specific gap-reason assertion: bandit not on PATH")
        _bandit_checks_skipped = True

    # Self-vet safety: a python-carrying quality-surface makes the empty-surface gate FALSE,
    # so the fix cannot misfire on a self-vet (which always carries python).
    py_tree = _foreign_python_tree(py_root)
    _check(
        "python tree has a NON-empty python quality-surface (gap gate stays off)",
        py_tree.quality_surface_python() != (),
        str(py_tree.quality_surface_python()),
    )

    # RIDER-1 (W3C-1a, R9-D shape): a FOREIGN python tree whose *.py sit OUTSIDE the platform quality
    # surface (root-level) is now EXAMINED by bandit — it was gapping as 'no quality-surface python'
    # while semgrep ran. The widen mirrors the exact-block foreign widen.
    widen_root = py_root.parent / "bandit_widen"
    _write(widen_root, "app.py", "import subprocess\n\n\ndef run_cmd(c):\n    subprocess.call(c, shell=True)\n")
    widen_tree = TargetTree.from_walk(widen_root)
    _check("RIDER-1: foreign python tree carries NO quality-surface python (root-level *.py)", widen_tree.quality_surface_python() == (), str(widen_tree.quality_surface_python()))
    if tool_available("bandit"):
        widen_cov = scan_bandit(widen_tree, "vr-rider").coverage
        _check("RIDER-1: bandit foreign WIDENS to all *.py (root-level examined, no longer gapped)", widen_cov.ran is True and widen_cov.files_examined >= 1, str(widen_cov))
    else:
        print("  SKIP  RIDER-1 bandit-widen live assertion: bandit not on PATH")
        _bandit_checks_skipped = True


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _check_pip_audit_defect1(base / "ts1", base / "py1")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _check_sast_defect2(base / "ts2", base / "py2")
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"foreign_scanner_scope_smoke OK: {len(_CHECKS_RUN)} checks passed")
    if _bandit_checks_skipped:
        print('BORN_CLONE_SKIP_WITNESS={"reason":"ambient_executable_missing","executable":"bandit"}')
        print(
            "SKIP: bandit not on PATH -- bandit-specific live assertions "
            "disclosed a gap rather than running; the structural assertions "
            "ran and passed."
        )
        return _SKIP_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
