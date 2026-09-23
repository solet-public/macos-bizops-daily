#!/usr/bin/env python3
"""Meta-smoke for the W-INT Cycle 2 driver-import gate (W-WINT2-EARLY).

Exercises the gate's collaborators against synthetic source modules and
hand-crafted allowlists, then runs the live gate against the real tree
with the shipped allowlist and asserts it returns 0 findings.

Cases:
  1. Detects a module-level `import psycopg`.
  2. Detects `from psycopg import sql`.
  3. Detects an inside-function `import psycopg_pool` (drift hides here).
  4. Detects all five driver roots: psycopg, psycopg_pool, asyncpg, pg8000, sqlalchemy.
  5. Does NOT detect non-driver imports.
  6. Allowlist wildcard `*` covers any specifier for the scope.
  7. Allowlist exact-match covers only the named specifier.
  8. `_is_in_scope`: ananta/src is in scope.
  9. `_is_in_scope`: plugins/<X>/src is in scope.
 10. `_is_in_scope`: plugins/<X>/tests is in scope.
 11. `_is_in_scope`: plugins/<X>/research / tools / migrations / parity_tests are OUT.
 12. `_is_in_scope`: plugins/<X>/knowledge_base is OUT.
 13. `_is_in_scope`: .venv / __pycache__ are OUT.
 14. End-to-end: live gate against real tree with shipped allowlist returns
     exit 0 and reports 0 non-allowlisted findings (proves wiring + allowlist
     remain coherent against the current inventory).
 15. End-to-end: live gate without allowlist reports non-zero blocking count
     (proves the gate actually flags the current bypass sites).
 16. Allowlist parse: malformed lines emit WARN and are skipped.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "quality_gates"))

from wint2_driver_import_check import (  # noqa: E402
    CHECK_ID,
    Allowlist,
    AllowlistEntry,
    Finding,
    _findings_from_module,
    _is_in_scope,
    _scan_module_for_drivers,
    load_allowlist,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _parse(src: str) -> ast.Module:
    return ast.parse(src)


def _case_module_level_import_psycopg() -> None:
    print("\nCase 1: module-level `import psycopg`")
    hits = _scan_module_for_drivers(_parse("import psycopg\n"))
    _check(hits == [(1, "psycopg")], "single hit at line 1 for `import psycopg`")


def _case_from_psycopg_import_sql() -> None:
    print("\nCase 2: `from psycopg import sql`")
    hits = _scan_module_for_drivers(_parse("from psycopg import sql\n"))
    _check(hits == [(1, "psycopg")], "single hit at line 1 for `from psycopg import sql`")


def _case_inside_function_import_psycopg_pool() -> None:
    print("\nCase 3: inside-function `import psycopg_pool` (drift hiding)")
    src = "def f():\n    import psycopg_pool\n    return psycopg_pool\n"
    hits = _scan_module_for_drivers(_parse(src))
    _check(hits == [(2, "psycopg_pool")],
           "nested import detected at line 2 (gate refuses lazy-import free pass)")


def _case_all_five_driver_roots() -> None:
    print("\nCase 4: all five driver roots detected")
    src = (
        "import psycopg\n"
        "import psycopg_pool\n"
        "import asyncpg\n"
        "import pg8000\n"
        "import sqlalchemy\n"
    )
    hits = _scan_module_for_drivers(_parse(src))
    _check(len(hits) == 5, "5 hits for 5 driver roots")
    _check({root for _, root in hits} == {
        "psycopg", "psycopg_pool", "asyncpg", "pg8000", "sqlalchemy",
    }, "all five roots reported")


def _case_non_driver_imports_ignored() -> None:
    print("\nCase 5: non-driver imports ignored")
    src = (
        "import os\n"
        "import json\n"
        "from pathlib import Path\n"
        "from typing import Any\n"
    )
    hits = _scan_module_for_drivers(_parse(src))
    _check(hits == [], "no hits for stdlib imports")


def _case_allowlist_wildcard_covers() -> None:
    print("\nCase 6: allowlist `*` wildcard covers any specifier")
    allowlist = Allowlist(frozenset({
        AllowlistEntry(CHECK_ID, "some/file.py", "*"),
    }))
    finding = Finding(
        check_id=CHECK_ID,
        scope_qualifier="some/file.py",
        specifier="42::psycopg",
        message="…",
        file_path="some/file.py",
        lineno=42,
    )
    _check(allowlist.covers(finding), "wildcard covers a specific finding")


def _case_allowlist_exact_match() -> None:
    print("\nCase 7: allowlist exact-match")
    allowlist = Allowlist(frozenset({
        AllowlistEntry(CHECK_ID, "some/file.py", "42::psycopg"),
    }))
    covered = Finding(check_id=CHECK_ID, scope_qualifier="some/file.py",
                      specifier="42::psycopg", message="…")
    not_covered = Finding(check_id=CHECK_ID, scope_qualifier="some/file.py",
                          specifier="43::psycopg", message="…")
    _check(allowlist.covers(covered), "exact specifier matches")
    _check(not allowlist.covers(not_covered), "different specifier does NOT match")


def _case_scope_ananta_src() -> None:
    print("\nCase 8: ananta/src is in scope")
    _check(_is_in_scope(REPO_ROOT / "ananta" / "src" / "ananta" / "platform" / "foo.py"),
           "ananta/src/ananta/.../foo.py is in scope")


def _case_scope_plugin_src() -> None:
    print("\nCase 9: plugins/<X>/src is in scope")
    _check(_is_in_scope(REPO_ROOT / "plugins" / "x_plugin" / "src" / "x_plugin" / "p.py"),
           "plugins/x_plugin/src/x_plugin/p.py is in scope")


def _case_scope_plugin_tests() -> None:
    print("\nCase 10: plugins/<X>/tests is in scope")
    _check(_is_in_scope(REPO_ROOT / "plugins" / "x_plugin" / "tests" / "smoke.py"),
           "plugins/x_plugin/tests/smoke.py is in scope")


def _case_scope_operator_tooling_out() -> None:
    print("\nCase 11: plugins/<X>/{research,tools,migrations,parity_tests} OUT")
    for segment in ("research", "tools", "migrations", "parity_tests"):
        path = REPO_ROOT / "plugins" / "x_plugin" / segment / "foo.py"
        _check(not _is_in_scope(path), f"plugins/x_plugin/{segment}/foo.py is OUT")


def _case_scope_knowledge_base_out() -> None:
    print("\nCase 12: plugins/<X>/knowledge_base OUT")
    path = REPO_ROOT / "plugins" / "x_plugin" / "knowledge_base" / "processes" / "x.py"
    _check(not _is_in_scope(path),
           "plugins/x_plugin/knowledge_base/.../x.py is OUT")


def _case_scope_venv_and_cache_out() -> None:
    print("\nCase 13: .venv / __pycache__ OUT")
    venv = REPO_ROOT / "plugins" / "x_plugin" / "src" / ".venv_cosyvoice" / "lib" / "x.py"
    cache = REPO_ROOT / "plugins" / "x_plugin" / "src" / "x_plugin" / "__pycache__" / "x.py"
    _check(not _is_in_scope(venv), ".venv* path is OUT")
    _check(not _is_in_scope(cache), "__pycache__ path is OUT")


def _case_findings_from_module_message_shape() -> None:
    print("\nCase 14: _findings_from_module produces well-formed Finding")
    module = _parse("import psycopg\n")
    fake_path = REPO_ROOT / "plugins" / "fake_plugin" / "src" / "fake_plugin" / "p.py"
    findings = _findings_from_module(module, fake_path)
    _check(len(findings) == 1, "one finding produced")
    if findings:
        f = findings[0]
        _check(f.check_id == CHECK_ID, "check_id is D1.1")
        _check(f.scope_qualifier
               == "plugins/fake_plugin/src/fake_plugin/p.py",
               "scope_qualifier is repo-relative POSIX path")
        _check(f.specifier == "1::psycopg",
               "specifier is `<lineno>::<module-root>`")
        _check("master plan" in f.message,
               "message references the master plan")


def _case_end_to_end_against_real_tree() -> None:
    print("\nCase 15: live gate against real tree with shipped allowlist returns 0")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "quality_gates" / "wint2_driver_import_check.py"),
         "--allowlist", str(REPO_ROOT / "quality_gates"
                            / "wint2_driver_import_allowlist.txt"),
         "--json"],
        capture_output=True, text=True, timeout=120,
    )
    _check(result.returncode == 0,
           f"gate exits 0 with shipped allowlist (got {result.returncode})")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        payload = None
        _check(False, f"gate JSON output parses ({exc})")
    if payload is not None:
        _check(payload["blocking"] == 0,
               f"0 non-allowlisted findings (got {payload['blocking']})")
        _check(payload["allowlisted"] > 0,
               f"some findings are allowlisted (got {payload['allowlisted']})")


def _case_end_to_end_without_allowlist() -> None:
    print("\nCase 16: live gate without allowlist reports current bypass sites")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "quality_gates" / "wint2_driver_import_check.py"),
         "--json"],
        capture_output=True, text=True, timeout=120,
    )
    _check(result.returncode == 1,
           f"gate exits 1 without allowlist (got {result.returncode})")
    try:
        payload = json.loads(result.stdout)
        _check(payload["blocking"] > 0,
               f"non-zero blocking count (got {payload['blocking']})")
    except json.JSONDecodeError as exc:
        _check(False, f"gate JSON output parses ({exc})")


def _case_warn_only_always_exits_zero() -> None:
    print("\nCase 17: --warn-only forces exit 0 even with non-allowlisted findings")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "quality_gates" / "wint2_driver_import_check.py"),
         "--warn-only", "--json"],
        capture_output=True, text=True, timeout=120,
    )
    _check(result.returncode == 0,
           f"--warn-only exits 0 even with findings (got {result.returncode})")


def _case_allowlist_parse_malformed_skipped() -> None:
    print("\nCase 18: malformed allowlist lines are skipped with WARN")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write("# comment\n")
        tmp.write(
            "D1.1::some/file.py::*  # owner: smoke reason: test fixture "
            "expires: 2099-01-01\n",
        )
        tmp.write(
            "malformed_no_separators  # owner: smoke reason: malformed key "
            "expires: 2099-01-01\n",
        )
        tmp.write("\n")  # blank line
        tmp_path = tmp.name
    try:
        allowlist = load_allowlist(Path(tmp_path))
        _check(len(allowlist.entries) == 1,
               f"one valid entry parsed (got {len(allowlist.entries)})")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def main() -> int:
    print("W-INT Cycle 2 driver-import gate meta-smoke")
    print("=" * 60)

    _case_module_level_import_psycopg()
    _case_from_psycopg_import_sql()
    _case_inside_function_import_psycopg_pool()
    _case_all_five_driver_roots()
    _case_non_driver_imports_ignored()
    _case_allowlist_wildcard_covers()
    _case_allowlist_exact_match()
    _case_scope_ananta_src()
    _case_scope_plugin_src()
    _case_scope_plugin_tests()
    _case_scope_operator_tooling_out()
    _case_scope_knowledge_base_out()
    _case_scope_venv_and_cache_out()
    _case_findings_from_module_message_shape()
    _case_end_to_end_against_real_tree()
    _case_end_to_end_without_allowlist()
    _case_warn_only_always_exits_zero()
    _case_allowlist_parse_malformed_skipped()

    print("\n" + "=" * 60)
    print(f"Passed: {_passed}")
    print(f"Failed: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
