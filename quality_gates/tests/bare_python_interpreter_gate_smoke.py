#!/usr/bin/env python3
"""Regression smoke for GTE-A4 -- no gate spawns a bare `python3`/`python`.

A gate that births a subprocess with the literal list-literal first argument
``["python3", ...]`` (or ``["python", ...]``) makes its own verdict a
property of whatever the CALLING PROCESS's ``PATH`` happens to resolve that
name to, rather than of the code under test -- the exact defect measured in
``iss_835bf2f4`` (``born_clone_gate.py``'s throwaway-venv creation, which on
this host silently started resolving to `brew python@3.14`) and
``iss_b860d6d0`` (``code_quality_check.py``'s Python-syntax phase, same
shape). The fix in both cases is to spawn the interpreter that already,
explicitly, validated the code -- ``sys.executable`` of the calling process,
or an already-resolved venv-python path -- never a bare name looked up fresh
through the caller's ambient PATH.

This smoke is a CENSUS, not a spot-check on the two named files: it AST-walks
every ``.py`` file under the directories in ``_SCANNED_ROOTS`` (excluding
tests/fixtures, which legitimately construct bare-python argv literals as
DATA rather than spawning anything) for a ``subprocess``-style call whose
first positional argument is a list literal beginning with the exact string
``"python3"`` or ``"python"``. A tracked allowlist
(``bare_python_interpreter_allowlist.txt``, same model as
``god_class_allowlist.txt``) is the only sanctioned way to add a new
exception -- an un-allowlisted new instance is a red, not a warning.
"""

from __future__ import annotations

import ast
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_CHECKS = 0

# Directories this census covers: quality_gates itself (script-invoked, no
# src/ layout) plus every shipped plugin's own src/ tree, which is where a
# gate-shaped subprocess spawn (a syntax check, a venv birth, a tool
# invocation feeding a pass/fail verdict) actually lives in this repo.
_SCANNED_ROOTS: tuple[str, ...] = ("quality_gates", "plugins")

# A path segment anywhere in the relative path excludes it from the census:
# these construct bare-python argv literals as fixture DATA (asserting a
# generated file's contents, or a synthetic smoke source string), never as an
# actual subprocess spawn this gate needs to police.
_EXCLUDED_PATH_SEGMENTS: tuple[str, ...] = (
    "tests", "test", ".venv", "venv", "__pycache__",
)

_BARE_NAMES: frozenset[str] = frozenset({"python3", "python"})
_SUBPROCESS_ATTRS: frozenset[str] = frozenset(
    {"run", "Popen", "check_call", "check_output", "call"}
)

_ALLOWLIST_PATH = _REPO_ROOT / "quality_gates" / "bare_python_interpreter_allowlist.txt"


@dataclass(frozen=True)
class Violation:
    """One bare-`python3`/`python` subprocess-spawn call site."""

    path: str
    line: int
    snippet: str

    @property
    def key(self) -> str:
        """Content-anchored allowlist key: survives the line moving, same
        model as dependency_declaration_gate's (package, import_root)."""
        return f"{self.path}::{self.snippet}"


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_ATTRS:
        base = func.value
        return isinstance(base, ast.Name) and base.id == "subprocess"
    return False


def _bare_python_arg(node: ast.Call) -> str | None:
    """The offending literal string, or None if this call is clean."""
    if not node.args:
        return None
    first = node.args[0]
    if not isinstance(first, ast.List) or not first.elts:
        return None
    head = first.elts[0]
    if isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value in _BARE_NAMES:
        return head.value
    return None


def _iter_python_files(root: Path, rel_dir: str) -> Iterator[Path]:
    base = root / rel_dir
    if not base.is_dir():
        return
    for path in base.rglob("*.py"):
        rel_parts = path.relative_to(root).parts
        if any(segment in _EXCLUDED_PATH_SEGMENTS for segment in rel_parts):
            continue
        yield path


def scan_repository(root: Path) -> list[Violation]:
    """Census every scanned root; returns one Violation per bare-python spawn."""
    violations: list[Violation] = []
    for rel_dir in _SCANNED_ROOTS:
        for path in _iter_python_files(root, rel_dir):
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            lines = source.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
                    continue
                bare = _bare_python_arg(node)
                if bare is None:
                    continue
                line_no = node.lineno
                snippet = lines[line_no - 1].strip() if 0 < line_no <= len(lines) else bare
                violations.append(
                    Violation(path=str(path.relative_to(root)), line=line_no, snippet=snippet)
                )
    return violations


def _load_allowlist(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _check_detector_catches_the_named_defect_shape() -> None:
    """Failing-mutation control: a synthetic bare-`python3` venv spawn, the
    exact shape iss_835bf2f4/iss_b860d6d0 were, must be caught."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        quality_gates = root / "quality_gates"
        quality_gates.mkdir()
        (quality_gates / "toy_gate.py").write_text(
            "import subprocess\n"
            "subprocess.run(['python3', '-m', 'venv', 'x'])\n",
            encoding="utf-8",
        )
        found = scan_repository(root)
    _check(len(found) == 1, "detector catches a synthetic bare-python3 venv spawn")
    _check(found[0].snippet.startswith("subprocess.run"), "reported violation names the call site")


def _check_detector_does_not_flag_validated_interpreters() -> None:
    """A clean call site -- sys.executable, or an already-resolved venv path
    -- must never be flagged; a detector that fires on the FIX is useless."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        quality_gates = root / "quality_gates"
        quality_gates.mkdir()
        (quality_gates / "toy_gate.py").write_text(
            "import subprocess\n"
            "import sys\n"
            "subprocess.run([sys.executable, '-m', 'venv', 'x'])\n"
            "subprocess.run([str(venv_python), '-m', 'py_compile', f])\n",
            encoding="utf-8",
        )
        found = scan_repository(root)
    _check(found == [], "sys.executable / venv_python call sites are never flagged")


def _check_detector_ignores_test_fixture_data() -> None:
    """A bare-python3 argv literal inside a tests/ path is fixture DATA
    (asserting a generated source string), not a real spawn -- excluded."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        tests_dir = root / "plugins" / "toy_plugin" / "tests"
        tests_dir.mkdir(parents=True)
        (tests_dir / "toy_smoke.py").write_text(
            "import subprocess\n"
            "subprocess.run(['python3', 'compute.py'], check=True)\n",
            encoding="utf-8",
        )
        found = scan_repository(root)
    _check(found == [], "tests/ paths are excluded from the census")


def _check_live_repository_census_is_allowlist_clean() -> None:
    """The real census against THIS checkout: every finding must be covered
    by the tracked allowlist, or this smoke reds -- this is the actual
    regression guard, not just the synthetic fixtures above."""
    found = scan_repository(_REPO_ROOT)
    allowlisted = _load_allowlist(_ALLOWLIST_PATH)
    unlisted = [v for v in found if v.key not in allowlisted]
    _check(
        unlisted == [],
        "unallowlisted bare-python3/python subprocess spawn(s) found: "
        + "; ".join(f"{v.path}:{v.line}: {v.snippet}" for v in unlisted),
    )


def main() -> int:
    _check_detector_catches_the_named_defect_shape()
    _check_detector_does_not_flag_validated_interpreters()
    _check_detector_ignores_test_fixture_data()
    _check_live_repository_census_is_allowlist_clean()
    print(f"✅ bare_python_interpreter_gate_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
