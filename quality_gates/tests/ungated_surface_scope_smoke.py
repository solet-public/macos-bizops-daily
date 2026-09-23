#!/usr/bin/env python3
"""Regression for the ungated-surface class of defect (no pytest, per project rule).

``solet_cli/`` and ``bootstrap_adapter/`` were confirmed live twice
(``unt_e5120eb0``, ``unt_52f2dadb``) to be outside every per-file and
whole-tree gate surface: ``is_per_file_gate_scoped_path`` returned ``False``
for real changed files under both, so a standard scoped run derived an
EMPTY ``.py`` scope and reported a vacuous clean over code nobody had
measured (iss_f99ef8c2/577, iss_7e4169ea/632, iss_17bb31c5/707,
iss_2456a716/777, iss_e252fc48/702, iss_e9990877/990016).

This asserts the actually-observable symptom — a scoped run over a file
under each directory returns a NONZERO scanned-file count — not merely
that ``_PER_FILE_GATE_TOP_LEVEL`` lists the directory's name. A config
entry with a typo, a stale path, or a predicate that silently fails to
match it would still pass a "does the table contain this string" check;
only running the actual scope-resolution code over a real synthetic file
catches that class of drift. This is the same discipline
``gate_crash_rendering_smoke.py`` already applies to the aggregate scope
walk (``test_aggregate_scope_is_in_repo_only``) — mirrored here for the
specific two directories these six issues name.

Each property carries a NEGATIVE CONTROL: ``solet_cli/tools`` and
``solet_cli/homebrew/scripts`` are deliberately excluded from this widening
(no filed issue names them; ``solet_cli/tools`` is documented
operator-tooling per ``quality_gates/gate_smokes.txt``'s ``generator_tooling``
not-shipped class) — a fixture file under either must stay OUT of scope, or
this smoke would not distinguish "the fix worked" from "everything is now
in scope, which happens to include the two directories we wanted."

Run: ``.venv/bin/python3 quality_gates/tests/ungated_surface_scope_smoke.py``
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

_GATE_DIR = Path(__file__).resolve().parent.parent
if str(_GATE_DIR) not in sys.path:
    sys.path.insert(0, str(_GATE_DIR))

import code_quality_check as cqc  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if not condition:
        _FAILURES.append(name)


@contextmanager
def _scratch_repo() -> Generator[Path]:
    """A throwaway git repo carrying one fixture file under each surface in
    question: the two now-gated directories, plus two deliberately-excluded
    siblings as negative controls. Same shape as
    ``gate_crash_rendering_smoke.py``'s ``_scratch_repo`` — ``init`` + ``add``
    only, no commit needed, so this runs the same in a born clone with no
    ``.git`` history as it does in a full checkout.
    """
    with tempfile.TemporaryDirectory(prefix="ungated_surface_fixture_") as tmp:
        root = Path(tmp).resolve()

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args], cwd=str(root), check=True,
                capture_output=True, text=True, timeout=60,
            )

        git("init", "-q")
        fixtures = {
            "solet_cli/src/solet_manager/_fixture_module.py": "def f() -> None:\n    pass\n",
            "solet_cli/tests/_fixture_smoke.py": "def main() -> int:\n    return 0\n",
            "bootstrap_adapter/_fixture_module.py": "def g() -> None:\n    pass\n",
            # Negative controls: real sibling classes this widening does NOT cover.
            "solet_cli/tools/_fixture_tool.py": "def t() -> None:\n    pass\n",
            "solet_cli/homebrew/scripts/_fixture_script.py": "def s() -> None:\n    pass\n",
        }
        for rel_path, content in fixtures.items():
            path = root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        git("add", *fixtures.keys())
        yield root


_IN_SCOPE = (
    "solet_cli/src/solet_manager/_fixture_module.py",
    "solet_cli/tests/_fixture_smoke.py",
    "bootstrap_adapter/_fixture_module.py",
)
_OUT_OF_SCOPE = (
    "solet_cli/tools/_fixture_tool.py",
    "solet_cli/homebrew/scripts/_fixture_script.py",
)


def test_is_per_file_gate_scoped_path_admits_the_two_directories() -> None:
    print("is_per_file_gate_scoped_path: solet_cli/{src,tests} and bootstrap_adapter/ are scoped")
    for rel_path in _IN_SCOPE:
        check(f"{rel_path} is scoped", cqc.is_per_file_gate_scoped_path(rel_path))


def test_is_per_file_gate_scoped_path_negative_control() -> None:
    print("NEGATIVE CONTROL: deliberately-excluded siblings stay unscoped")
    for rel_path in _OUT_OF_SCOPE:
        check(f"{rel_path} is NOT scoped", not cqc.is_per_file_gate_scoped_path(rel_path))


def test_scoped_run_over_each_directory_yields_nonzero_files() -> None:
    """The actual symptom the six issues measured: a scoped run over a file
    under each of these directories must not silently scan zero files."""
    print("a scoped run over a fixture file under each directory scans it (nonzero count)")
    with _scratch_repo() as root:
        paths = {p.resolve() for p in cqc._per_file_gate_paths(root)}
        for rel_path in _IN_SCOPE:
            fixture = (root / rel_path).resolve()
            check(f"{rel_path} is in the resolved scope (count > 0, not skipped)",
                  fixture in paths)
        for rel_path in _OUT_OF_SCOPE:
            fixture = (root / rel_path).resolve()
            check(f"NEGATIVE CONTROL: {rel_path} is NOT in the resolved scope",
                  fixture not in paths)


def test_scope_roots_resolves_both_new_top_level_directories() -> None:
    print("_scope_roots resolves solet_cli/src, solet_cli/tests, bootstrap_adapter as roots")
    with _scratch_repo() as root:
        roots = {str(p.relative_to(root)) for p in cqc._scope_roots(root)}
        for expected in ("solet_cli/src", "solet_cli/tests", "bootstrap_adapter"):
            check(f"{expected} is a resolved scope root", expected in roots)


def main() -> int:
    print("Ungated-surface scope smoke (solet_cli/, bootstrap_adapter/)\n")
    for test in (
        test_is_per_file_gate_scoped_path_admits_the_two_directories,
        test_is_per_file_gate_scoped_path_negative_control,
        test_scoped_run_over_each_directory_yields_nonzero_files,
        test_scope_roots_resolves_both_new_top_level_directories,
    ):
        test()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
