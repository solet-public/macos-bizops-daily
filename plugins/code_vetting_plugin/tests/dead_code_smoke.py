"""dead_code_smoke.py — R9-A: the rebuilt in-process vulture dead-code scanner (two-layer).

Pins the ruling's contract with the EXACT probed vulture confidences (so an upgrade shifting the
per-class semantics fails loud):

  * Roster + pin: the 'vulture' slot points at ``dead_code.scan``; vulture is pinned to 2.16 (== the
    running version, asserted at scan time).
  * EXACT confidences per class (direct vulture on a fixture): import = 90, unreachable_code = 100,
    and the whole function/class/method/variable family = 60 — the map that MADE the two-layer split
    mandatory (60% is below any clean threshold, so it can't be a Finding row).
  * Two-layer (FOREIGN): findings = unused-import (LOW) + unreachable (MEDIUM); the 60% family →
    candidate-dead-symbols payload, NEVER findings. An ``__init__.py`` import is NEVER a finding.
  * Per-class self emission: on SELF the unused-import is SUPPRESSED (ruff F401 owns) while unreachable
    is EMITTED; the ``@platform_process`` registry method is dropped from the candidate table on self
    (the project_local-tier ignore-decorators) but present on FOREIGN (we owe a foreign repo no such config).
  * Dimension DEAD_CODE (out of the zero-FP registry); candidate-table cap disclosure; the payload
    round-trips onto the run record + the vetting_runs schema carries the column.

Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
import tempfile
from pathlib import Path

import vulture

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.live_state import get_vetting_runs_schema
from code_vetting_plugin.models import Dimension, Severity
from code_vetting_plugin.report import DEFAULT_ZERO_FP_DIMENSIONS
from code_vetting_plugin.run_record import AllowlistDelta, RunTarget, build_run_metrics
from code_vetting_plugin.runner import SCANNERS
from code_vetting_plugin.scanners.dead_code import (  # noqa: PLC2701 — pin the internal two-layer + payload logic
    _RENDER_CAP,
    VULTURE_PINNED_VERSION,
    DeadSymbol,
    DeadSymbolsReport,
    render_candidate_dead_symbols_section,
    scan,
)
from code_vetting_plugin.targets import TargetTree

_CHECKS_RUN: list[str] = []
_BASE = "plugins/dcfix/src/dcfix"

_MOD = (
    "import os\n\n\n"
    "UNUSED_CONST = 42\n\n\n"
    "def dead_function(a, b):\n"
    "    return a + b\n\n\n"
    "def has_unreachable(x):\n"
    "    return x\n"
    "    print('never')\n\n\n"
    "class DeadClass:\n"
    "    def dead_method(self):\n"
    "        return 1\n\n"
    "    @platform_process\n"
    "    def registered(self):\n"
    "        return 2\n"
)
_INIT = "from .mod import dead_function\n"


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


def _fixture(root: Path, *, foreign: bool) -> TargetTree:
    _write(root, f"{_BASE}/mod.py", _MOD)
    _write(root, f"{_BASE}/__init__.py", _INIT)
    if foreign:
        return TargetTree.from_walk(root)
    return TargetTree(root=root, tracked=(f"{_BASE}/mod.py", f"{_BASE}/__init__.py"), enumeration="git", foreign=False)


def _check_roster_and_pin() -> None:
    spec = next((s for s in SCANNERS if s.name == "vulture"), None)
    _check("'vulture' roster slot → dead_code scanner", spec is not None and spec.run.__module__.endswith("scanners.dead_code"), str(spec))
    _check("vulture pinned exact to the running version", VULTURE_PINNED_VERSION == "2.16" == vulture.__version__, f"{VULTURE_PINNED_VERSION} / {vulture.__version__}")


def _check_exact_confidences(root: Path) -> None:
    _write(root, "mod.py", _MOD)
    scanner = vulture.Vulture()
    scanner.scavenge([str(root / "mod.py")])
    by_typ: dict[str, int] = {}
    for item in scanner.get_unused_code():
        by_typ.setdefault(item.typ, item.confidence)
    _check("EXACT: unused import confidence == 90", by_typ.get("import") == 90, str(by_typ))
    _check("EXACT: unreachable_code confidence == 100", by_typ.get("unreachable_code") == 100, str(by_typ))
    _check("EXACT: function/class/variable/method family == 60", all(by_typ.get(k) == 60 for k in ("function", "class", "variable", "method")), str(by_typ))


def _check_two_layer_foreign(root: Path) -> None:
    res = scan(_fixture(root, foreign=True), "vr-r9")
    constraints = {(f.constraint_violated, f.severity) for f in res.findings}
    _check("foreign: unused-import → LOW finding", ("dead_code:unused_import", Severity.LOW) in constraints, str(constraints))
    _check("foreign: unreachable → MEDIUM finding", ("dead_code:unreachable", Severity.MEDIUM) in constraints, str(constraints))
    _check("foreign: NO __init__.py import finding", not any(f.file.endswith("__init__.py") for f in res.findings), str([f.file for f in res.findings]))
    names = {c.name for c in res.dead_symbols.candidates}
    _check("foreign: the 60% family is CANDIDATES, not findings", {"dead_function", "has_unreachable", "DeadClass", "dead_method"} <= names, str(names))
    _check("foreign: candidates never become findings", not any(f.constraint_violated.endswith(("function", "class", "method", "variable")) for f in res.findings), "")
    _check("foreign (no ignore): @platform_process method IS a candidate", "registered" in names, str(names))
    _check("findings are DEAD_CODE dimension", all(f.dimension is Dimension.DEAD_CODE for f in res.findings), "")
    _check("DEAD_CODE is OUT of the zero-FP registry", Dimension.DEAD_CODE not in DEFAULT_ZERO_FP_DIMENSIONS, "")


def _check_self_per_class(root: Path) -> None:
    res = scan(_fixture(root, foreign=False), "vr-r9-self")
    constraints = {f.constraint_violated for f in res.findings}
    _check("self: unused-import SUPPRESSED (ruff F401 owns)", "dead_code:unused_import" not in constraints, str(constraints))
    _check("self: unreachable EMITTED (no gate covers it)", "dead_code:unreachable" in constraints, str(constraints))
    names = {c.name for c in res.dead_symbols.candidates}
    _check("self: @platform_process method DROPPED from candidates (ignore-decorators)", "registered" not in names, str(names))
    _check("self: real dead symbols still surface", "dead_function" in names, str(names))


def _check_cap_and_persistence() -> None:
    # Candidate-table cap disclosure (synthetic — the fixture is too small to overflow the cap).
    symbols = tuple(
        DeadSymbol(file=f"src/f{i}.py", line=i, name=f"sym{i}", kind="function", confidence=60, dead_lines=100 - i)
        for i in range(_RENDER_CAP + 5)
    )
    report = DeadSymbolsReport(tool="vulture", tool_version="2.16", total=len(symbols), by_kind={"function": len(symbols)}, candidates=symbols)
    section = render_candidate_dead_symbols_section(report)
    _check("candidate section header + not-findings framing", section.startswith("## Candidate Dead Symbols") and "NOT findings" in section, section[:80])
    _check("candidate table caps the rendered rows + discloses the overflow", f"+{len(symbols) - _RENDER_CAP} more" in section, section[-200:])
    _check("None payload → stable placeholder", render_candidate_dead_symbols_section(None) == "## Candidate Dead Symbols\n\n_No dead-code candidate evidence recorded._", "")
    # Persistence: the payload rides the run record + the schema carries the column.
    metrics = build_run_metrics(
        run_id="vr-r9", target=RunTarget(repo="t", ref="r", scope="s"), started="a", finished="b",
        substrate="heuristic", layers_run=[], findings=[], coverage=[], allowlist_delta=AllowlistDelta(totals={}),
        dead_symbols=report.to_dict(),
    )
    row = metrics.to_dict()
    _check("run record carries dead_symbols", isinstance(row.get("dead_symbols"), dict) and row["dead_symbols"]["total"] == len(symbols), str(type(row.get("dead_symbols"))))
    _check("vetting_runs schema declares the dead_symbols column", "dead_symbols" in get_vetting_runs_schema().tables["vetting_runs"].columns, "")


def main() -> int:
    try:
        _check_roster_and_pin()
        with tempfile.TemporaryDirectory() as tmp:
            _check_exact_confidences(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_two_layer_foreign(Path(tmp) / "f")
        with tempfile.TemporaryDirectory() as tmp:
            _check_self_per_class(Path(tmp) / "s")
        _check_cap_and_persistence()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"dead_code_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
