"""ts_toolchain_smoke.py — R7-4: the tsc + eslint target-toolchain scanners.

Pins the materialized-deps tier (design §2/§5.1/§5.2/§6/§8) WITHOUT needing the target's
node_modules installed — every branch is hermetic (canned tool output; crafted trees; a
whole-tree hash) or absence-tolerant (the host-node-dependent gap ordering):

  * Roster wiring: SCANNERS carries tsc (STACK/{TYPESCRIPT}) + eslint (STACK/{TS,JS}), both
    ``executes_target_code=True``; the full roster is 17.
  * R1 opt-in gate (BOTH new gap branches, design §9): ``node_modules`` present + flag unset →
    the opt-in gap (returns before any tool touch — hermetic); flag set + ``node_modules`` absent
    → the materialization gap (node present) / node gap (absence-tolerant).
  * Parse: canned ``tsc --pretty false`` lines → TYPE_COVERAGE findings (``tsc:TSnnnn``); canned
    eslint JSON → CODE_QUALITY (sev 2→MEDIUM / 1→LOW; null ruleId → ``eslint:fatal``). A diagnostic
    anchored inside ``node_modules/`` is DROPPED (a materialized dep's own issue), count disclosed.
  * Flood cap (design §5.1/§5.2): >200 findings → first 200 kept, overflow disclosed.
  * R3 ran-with-disclosure (design §6): a ``ran=True`` record carrying the cap/drop disclosure is
    NOT a coverage gap (``coverage_gaps`` excludes it) and renders with ``ran=yes``.
  * Read-only invariant (design §8): the target tree is BYTE-IDENTICAL before/after a full
    ``run_all`` (flag on and off) — tsc ``--incremental false`` + eslint no ``--fix``/``--cache``.

Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import hashlib
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.coverage import CoverageRecord
from code_vetting_plugin.models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from code_vetting_plugin.report import ReportRenderer
from code_vetting_plugin.run_record import coverage_gaps
from code_vetting_plugin.runner import SCANNERS, Applicability, run_all
from code_vetting_plugin.scanners.ts_toolchain import (  # noqa: PLC2701 — pin the internal parse/gate/cap logic
    _MAX_TOOL_FINDINGS,
    _NODE_GAP,
    _cap,
    _disclosure,
    _eslint_findings,
    _has_eslint_config,
    _toolchain_precondition_gap,
    _tsc_findings,
    scan_eslint,
    scan_tsc,
)
from code_vetting_plugin.stacks import Stack
from code_vetting_plugin.targets import TargetTree
from code_vetting_plugin.toolrun import tool_available

_CHECKS_RUN: list[str] = []
_NODE_PRESENT = tool_available("node")


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ts_fixture(root: Path, *, with_node_modules: bool) -> TargetTree:
    """A tiny self-contained TS target: strict tsconfig, one deliberate type error, a flat eslint
    config using core rules only. An EMPTY ``node_modules/`` dir satisfies the materialization
    gate without shipping deps (design §9 fixture strategy)."""
    root.mkdir(parents=True, exist_ok=True)
    _write(root, "src/bad.ts", "export const n: string = 1;\nconst unused = 2;\n")
    _write(root, "tsconfig.json", '{"compilerOptions": {"strict": true, "noEmit": true}}\n')
    _write(root, "eslint.config.js", "module.exports = [{rules: {'no-unused-vars': 'error'}}];\n")
    _write(root, "package.json", '{"name": "fixture", "version": "1.0.0"}\n')
    if with_node_modules:
        (root / "node_modules").mkdir(exist_ok=True)
    return TargetTree.from_walk(root)


def _check_roster_wiring() -> None:
    by_name = {spec.name: spec for spec in SCANNERS}
    _check("SCANNERS declares 20 scanners (secrets/SAST/deps/gates + tsc/eslint + structural_metrics + python_type_check + rulebook_sync)", len(SCANNERS) == 20, str(len(SCANNERS)))
    tsc = by_name.get("tsc")
    eslint = by_name.get("eslint")
    _check("tsc: STACK/{TYPESCRIPT}, executes_target_code", tsc is not None and tsc.applicability is Applicability.STACK and tsc.stacks == frozenset({Stack.TYPESCRIPT}) and tsc.executes_target_code, str(tsc))
    _check("eslint: STACK/{TYPESCRIPT,JAVASCRIPT} (R2), executes_target_code", eslint is not None and eslint.stacks == frozenset({Stack.TYPESCRIPT, Stack.JAVASCRIPT}) and eslint.executes_target_code, str(eslint))


def _reason(cov: CoverageRecord) -> str:
    return cov.gap_reason or ""


def _check_optin_present(with_nm: TargetTree) -> None:
    # BRANCH 1 (present-without-flag): node_modules present + flag unset → opt-in gap. This path
    # returns BEFORE any tool touch, so it is fully hermetic and deterministic.
    optin = _toolchain_precondition_gap(with_nm, "tsc", execute_target_toolchain=False)
    _check("helper: nm present + flag off → opt-in gap (hermetic)", optin is not None and optin.coverage.ran is False and "not enabled" in _reason(optin.coverage), str(optin))
    tsc_optin = scan_tsc(with_nm, "vr-r74", execute_target_toolchain=False).coverage
    _check("scan_tsc: nm present + flag off → opt-in gap", tsc_optin.ran is False and "not enabled" in _reason(tsc_optin), str(tsc_optin))
    eslint_optin = scan_eslint(with_nm, "vr-r74", execute_target_toolchain=False).coverage
    _check("scan_eslint: nm present + flag off → opt-in gap", eslint_optin.ran is False and "not enabled" in _reason(eslint_optin), str(eslint_optin))


def _check_optin_absent(without_nm: TargetTree) -> None:
    # BRANCH 2 (flag-without-node_modules): flag set + node_modules absent → materialization gap
    # (node present) or node gap (node absent) — absence-tolerant on host node.
    expected = "not materialized" if _NODE_PRESENT else _NODE_GAP
    tsc_nonm = scan_tsc(without_nm, "vr-r74", execute_target_toolchain=True).coverage
    _check("scan_tsc: flag on + no node_modules → ran=False gap", tsc_nonm.ran is False, str(tsc_nonm))
    _check("scan_tsc: flag-without-node_modules gap names the missing prerequisite", expected in _reason(tsc_nonm), f"node_present={_NODE_PRESENT} :: {tsc_nonm.gap_reason}")
    eslint_nonm = scan_eslint(without_nm, "vr-r74", execute_target_toolchain=True).coverage
    _check("scan_eslint: flag on + no node_modules → ran=False gap", eslint_nonm.ran is False and expected in _reason(eslint_nonm), str(eslint_nonm))


def _check_optin_gate(base: Path) -> None:
    _check_optin_present(_ts_fixture(base / "withnm", with_node_modules=True))
    _check_optin_absent(_ts_fixture(base / "nonm", with_node_modules=False))


def _check_eslint_config_detection(base: Path) -> None:
    configured = _ts_fixture(base / "cfg", with_node_modules=False)
    _check("_has_eslint_config detects the flat eslint.config.js", _has_eslint_config(configured) is True, "")
    bare = base / "bare"
    _write(bare, "src/a.ts", "export const x = 1;\n")
    _write(bare, "package.json", '{"name": "bare"}\n')
    _check("_has_eslint_config false when no config present", _has_eslint_config(TargetTree.from_walk(bare)) is False, "")


def _check_tsc_parse() -> None:
    stdout = (
        "src/bad.ts(1,14): error TS2322: Type 'number' is not assignable to type 'string'.\n"
        "node_modules/dep/index.d.ts(3,1): error TS1005: ';' expected.\n"
        "this line is not a diagnostic and is ignored\n"
    )
    findings, dropped = _tsc_findings(stdout, Path("/t"), "vr-r74", "Version 5.4.2")
    _check("tsc parse: one target-anchored diagnostic → one finding", len(findings) == 1, str(findings))
    _check("tsc parse: node_modules-anchored diagnostic DROPPED (count=1)", dropped == 1, str(dropped))
    only = findings[0]
    _check("tsc finding: TYPE_COVERAGE / MEDIUM", only.dimension is Dimension.TYPE_COVERAGE and only.severity is Severity.MEDIUM, str(only))
    _check("tsc finding: file + line from the diagnostic", only.file == "src/bad.ts" and only.line == 1, f"{only.file}:{only.line}")
    _check("tsc finding: constraint pins the TS code", only.constraint_violated == "tsc:TS2322", only.constraint_violated)
    _check("tsc finding: L1 deterministic + provenance rule", only.layer is Layer.L1_DETERMINISTIC and only.provenance.rule_id == "TS2322", str(only.provenance))


def _check_eslint_parse() -> None:
    payload = [
        {"filePath": "/t/src/bad.ts", "messages": [
            {"ruleId": "no-unused-vars", "severity": 2, "line": 2, "message": "'unused' is assigned but never used."},
            {"ruleId": "eqeqeq", "severity": 1, "line": 5, "message": "Expected ==="},
            {"ruleId": None, "severity": 2, "line": 1, "message": "Parsing error: fatal"},
        ]},
        {"filePath": "/t/node_modules/dep/index.js", "messages": [
            {"ruleId": "no-var", "severity": 1, "line": 1, "message": "unexpected var"},
        ]},
    ]
    findings, dropped, files_linted = _eslint_findings(payload, Path("/t"), "vr-r74", "v9.1.0")
    _check("eslint parse: 3 target findings (node_modules messages dropped)", len(findings) == 3, str(len(findings)))
    _check("eslint parse: node_modules message dropped (count=1)", dropped == 1, str(dropped))
    _check("eslint parse: files_linted counts both JSON entries", files_linted == 2, str(files_linted))
    by_rule = {f.constraint_violated: f for f in findings}
    _check("eslint: sev 2 → MEDIUM", by_rule["eslint:no-unused-vars"].severity is Severity.MEDIUM, "")
    _check("eslint: sev 1 → LOW", by_rule["eslint:eqeqeq"].severity is Severity.LOW, "")
    _check("eslint: null ruleId → eslint:fatal", "eslint:fatal" in by_rule, str(sorted(by_rule)))
    _check("eslint: CODE_QUALITY dimension", all(f.dimension is Dimension.CODE_QUALITY for f in findings), "")


def _synthetic_findings(count: int) -> list[Finding]:
    return [
        Finding.build(
            run_id="vr-r74", layer=Layer.L1_DETERMINISTIC, dimension=Dimension.TYPE_COVERAGE,
            severity=Severity.MEDIUM, file=f"src/f{index:04d}.ts", line=index,
            constraint_violated=f"tsc:TS{2000 + index}", evidence=f"diagnostic {index}",
            provenance=Provenance(source="tsc"), context_profile=ContextProfile.PRODUCTION,
        )
        for index in range(count)
    ]


def _check_flood_cap_and_disclosure() -> None:
    over = _synthetic_findings(_MAX_TOOL_FINDINGS + 1)
    capped = _cap(over)
    _check("flood cap: findings bounded to _MAX_TOOL_FINDINGS", len(capped) == _MAX_TOOL_FINDINGS, str(len(capped)))
    _check("flood cap: deterministic file/line order (first kept)", capped[0].file == "src/f0000.ts", capped[0].file)
    _check("disclosure: overflow discloses emitted count + cap", (_disclosure(_MAX_TOOL_FINDINGS + 1, 0) or "").startswith(f"emitted {_MAX_TOOL_FINDINGS + 1} diagnostics"), str(_disclosure(_MAX_TOOL_FINDINGS + 1, 0)))
    _check("disclosure: node_modules-drop note when dropped>0", "node_modules dropped" in (_disclosure(5, 3) or ""), str(_disclosure(5, 3)))
    _check("disclosure: None when nothing capped and nothing dropped", _disclosure(5, 0) is None, str(_disclosure(5, 0)))


def _check_ran_with_disclosure_r3() -> None:
    # A ran=True record carrying the cap disclosure is ran-WITH-disclosure, NOT a coverage gap (R3).
    disclosed = CoverageRecord(scanner="tsc", ran=True, files_examined=3, gap_reason="emitted 201 diagnostics; first 200 converted to findings")
    real_gap = CoverageRecord(scanner="eslint", ran=False, files_examined=0, gap_reason="no eslint config in target")
    gaps = coverage_gaps([disclosed, real_gap])
    _check("R3: coverage_gaps EXCLUDES the ran=True disclosure record", not any("tsc:" in g for g in gaps), str(gaps))
    _check("R3: coverage_gaps still reports the real ran=False gap", any(g.startswith("eslint:") for g in gaps), str(gaps))
    section = ReportRenderer()._coverage_section([disclosed])  # noqa: SLF001 — pin the ran-with-disclosure render
    _check("R3: renderer shows the disclosure row with ran=yes (not NO)", "| tsc | yes | 3 | emitted 201 diagnostics" in section, section)


def _tree_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        hasher.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_file() and not path.is_symlink():
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _check_read_only_invariant(base: Path) -> None:
    # Full run_all over the TS fixture must leave the target tree BYTE-IDENTICAL (design §8), whether
    # the toolchain opt-in is on or off: tsc --incremental false writes no .tsbuildinfo; eslint runs
    # without --fix/--cache. (With an EMPTY node_modules + no bundled binary, tsc/eslint gap at the
    # binary check — no execution — so this holds regardless of which tools the host has installed.)
    for label, flag in (("flag OFF", False), ("flag ON", True)):
        fixture = _ts_fixture(base / f"ro_{flag}", with_node_modules=True)
        before = _tree_digest(fixture.root)
        run_all(fixture, "vr-r74-ro", execute_target_toolchain=flag)
        after = _tree_digest(fixture.root)
        _check(f"read-only: target tree byte-identical after run_all ({label})", before == after, f"{before} != {after}")


def main() -> int:
    try:
        _check_roster_wiring()
        with tempfile.TemporaryDirectory() as tmp:
            _check_optin_gate(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_eslint_config_detection(Path(tmp))
        _check_tsc_parse()
        _check_eslint_parse()
        _check_flood_cap_and_disclosure()
        _check_ran_with_disclosure_r3()
        with tempfile.TemporaryDirectory() as tmp:
            _check_read_only_invariant(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"ts_toolchain_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
