"""duplication_disjoint_smoke.py — R8-2: disjoint-by-language duplicate detection.

Pins the §A.4-revision contract:

  * ZERO-REGRESSION (the load-bearing proof): the Python exact-block findings are BYTE-IDENTICAL
    whether or not non-Python files are present — adding a TS token-clone lane does not perturb the
    Python lane. The exact-block detector is unchanged from pre-R8-2.
  * NON-PYTHON token-clone: a TS fixture with a known cross-file clone yields a ``dup:token_clone``
    finding via lizard at the default 70-token threshold; a Python-only tree yields ZERO token-clone
    findings (the lanes are disjoint by language — Python never enters the token-clone lane).
  * DISTINCT mechanism ids + provenance: ``dup:exact_block`` (gate:duplication / rule dup) vs
    ``dup:token_clone`` (lizard / rule token_clone) — trend lines never conflate.
  * COVERAGE DISCLOSURE (rider 3): the ran=True coverage row discloses the per-language split —
    mechanism, file counts, and the token threshold.

The exact-block lane scans ``quality_surface_python()``, so the fixture places files under a
quality-surface path (``plugins/<x>/src/...``). Run directly or via run_smokes.py.
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

from code_vetting_plugin.models import Finding
from code_vetting_plugin.scanners import duplication
from code_vetting_plugin.targets import TargetTree

_CHECKS_RUN: list[str] = []

_BASE = "plugins/dupfix/src/dupfix"

# 12 significant (post-normalization) lines, byte-identical in two files → an exact-block duplicate.
_PY_DUP_BODY = (
    "    total = 0\n"
    "    scale = 1\n"
    "    for value in values:\n"
    "        adjusted = value * scale\n"
    "        if adjusted > limit:\n"
    "            total += adjusted\n"
    "        elif adjusted < 0:\n"
    "            total -= adjusted\n"
    "        else:\n"
    "            total += 1\n"
    "        scale += 1\n"
    "    result = total / count\n"
)


def _ts_clone_block() -> str:
    body = "\n".join(f"  const step{i} = compute(base{i}, factor{i}) + offset{i} * weight{i};" for i in range(30))
    return "export function large(base: number): number {\n" + body + "\n  return step29;\n}\n"


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


def _fixture(root: Path, *, with_ts: bool) -> TargetTree:
    """Two python files sharing an exact block (under a quality-surface path), optionally plus two
    TS files sharing a token-clone. ``foreign=False`` so quality_surface_python matches the paths."""
    _write(root, f"{_BASE}/a.py", f"import os\n\n\ndef alpha(values, limit, count):\n{_PY_DUP_BODY}    return result\n")
    _write(root, f"{_BASE}/b.py", f"import sys\n\n\ndef beta(values, limit, count):\n{_PY_DUP_BODY}    return result\n")
    tracked = [f"{_BASE}/a.py", f"{_BASE}/b.py"]
    if with_ts:
        block = _ts_clone_block()
        _write(root, f"{_BASE}/x.ts", f"import {{compute}} from './p';\n{block}")
        _write(root, f"{_BASE}/y.ts", f"import {{compute}} from './q';\n{block}")
        tracked += [f"{_BASE}/x.ts", f"{_BASE}/y.ts"]
    return TargetTree(root=root, tracked=tuple(tracked), enumeration="git", foreign=False)


def _by_id(findings: list[Finding], mechanism: str) -> list[Finding]:
    return [f for f in findings if f.constraint_violated == mechanism]


def _exact_signature(findings: list[Finding]) -> list[tuple[str, int | None, str]]:
    """The identity of the exact-block findings — (file, line, evidence), sorted."""
    return sorted((f.file, f.line, f.evidence) for f in _by_id(findings, "dup:exact_block"))


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp_py, tempfile.TemporaryDirectory() as tmp_mixed:
            py_only = duplication.scan(_fixture(Path(tmp_py), with_ts=False), "vr-r82")
            mixed = duplication.scan(_fixture(Path(tmp_mixed), with_ts=True), "vr-r82")

            exact_py = _by_id(py_only.findings, "dup:exact_block")
            _check("python-only: exact-block finds the duplicate", len(exact_py) >= 1, str(len(exact_py)))
            _check("python-only: ZERO token-clone (disjoint — python never enters that lane)", not _by_id(py_only.findings, "dup:token_clone"), "")

            # THE zero-regression proof: adding non-python files leaves the exact-block findings byte-identical.
            _check(
                "ZERO-REGRESSION: exact-block findings byte-identical with/without non-python files",
                _exact_signature(py_only.findings) == _exact_signature(mixed.findings),
                f"{_exact_signature(py_only.findings)} != {_exact_signature(mixed.findings)}",
            )

            token = _by_id(mixed.findings, "dup:token_clone")
            _check("mixed: token-clone finds the TS cross-file clone", len(token) >= 1, str(len(token)))
            _check("token-clone points at the origin fragment", "token-clone" in getattr(token[0], "evidence", ""), getattr(token[0], "evidence", ""))

            # Distinct provenance per mechanism.
            eb = exact_py[0]
            _check("exact-block provenance unchanged (gate:duplication / rule dup)", eb.provenance.source == "gate:duplication" and eb.provenance.rule_id == "dup", str(eb.provenance))
            _check("token-clone provenance (lizard / rule token_clone)", token[0].provenance.source == "lizard" and token[0].provenance.rule_id == "token_clone", str(token[0].provenance))
            _check("both lanes are the DUP dimension", all(f.dimension.value == "dup" for f in (eb, token[0])), "")

            # Coverage disclosure of the per-language split (rider 3).
            gap = mixed.coverage.gap_reason or ""
            _check("coverage row ran=True (structurally present)", mixed.coverage.ran is True, str(mixed.coverage))
            _check("coverage discloses the exact-block lane (python file count)", "exact-block:" in gap and "python file" in gap, gap)
            _check("coverage discloses the token-clone lane + threshold", "token-clone:" in gap and "min_duplicate_tokens=70" in gap, gap)

        # R9-D exact-block widen: a FOREIGN target scans all *.py (root-level, outside the quality
        # surface); a self-vet stays quality-surface-scoped (so self findings are byte-identical).
        with tempfile.TemporaryDirectory() as widen_tmp:
            wroot = Path(widen_tmp) / "w"
            wroot.mkdir()
            dup = "\n".join(f"    step{i} = compute(base{i}) + off{i}" for i in range(12))
            _write(wroot, "a.py", f"def alpha():\n{dup}\n    return step0\n")
            _write(wroot, "b.py", f"def beta():\n{dup}\n    return step0\n")
            foreign_dup = _by_id(duplication.scan(TargetTree.from_walk(wroot), "vr-r9d").findings, "dup:exact_block")
            _check("R9-D: foreign exact-block widens to root-level *.py (was quality-surface-only)", len(foreign_dup) >= 1, str(len(foreign_dup)))
            self_tree = TargetTree(root=wroot, tracked=("a.py", "b.py"), enumeration="git", foreign=False)
            self_dup = _by_id(duplication.scan(self_tree, "vr-r9d-self").findings, "dup:exact_block")
            _check("R9-D: self scope UNCHANGED (root-level *.py not in quality-surface → 0 exact-block)", len(self_dup) == 0, str(self_dup))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"duplication_disjoint_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
