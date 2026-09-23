#!/usr/bin/env python3
"""Smoke tests for ``quality_gates/sql_access_gate.py`` (no pytest, per project rule).

Covers each category detector (incl. nested/lazy occurrences), the structural
owner-exemption (including the state-service-impl-layer case from B1), the
bare-``execute`` exclusion (N1), the false-positive vs migrate-debt allowlist
convention (N3), allowlist suffix/specifier matching, and ``--require-clean``.

Run: ``.venv/bin/python3 quality_gates/tests/sql_access_gate_smoke.py``
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_GATE_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _GATE_DIR.parent
_VENV_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python3"
if str(_GATE_DIR) not in sys.path:
    sys.path.insert(0, str(_GATE_DIR))

import sql_access_gate as gate  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if not condition:
        _FAILURES.append(name)


def _scan(source: str, path: str) -> list[gate.Finding]:
    return gate.scan_module(ast.parse(source), path)


# ---------------------------------------------------------------------------


def test_s0_driver_import() -> None:
    print("S0 driver-import (module-level AND nested/lazy):")
    src = (
        "import psycopg\n"
        "from asyncpg.pool import Pool\n"
        "def lazy():\n"
        "    import sqlalchemy\n"
        "    return sqlalchemy\n"
    )
    findings = _scan(src, "ananta/src/ananta/x.py")
    s0 = [f for f in findings if f.category_id == gate.CAT_DRIVER]
    check("3 driver imports (incl. nested lazy import)", len(s0) == 3)
    check("nested import psycopg/asyncpg/sqlalchemy all caught",
          {f.line for f in s0} == {1, 2, 4})


def test_s1_sql_call_and_bare_execute_excluded() -> None:
    print("S1 raw-SQL call (bare .execute() + .transactional() must NOT be flagged):")
    src = (
        "def f(state, txn, obj):\n"
        "    state.execute_sql('q')\n"
        "    with state.transactional() as t:\n"
        "        txn.fetch_one()\n"
        "    obj.execute('not sql — PluginBase.execute / pipeline.execute')\n"
    )
    findings = _scan(src, "ananta/src/ananta/x.py")
    s1 = sorted(f.detail for f in findings if f.category_id == gate.CAT_SQL_CALL)
    check("execute_sql + fetch_one flagged (the raw-SQL cursor surface)",
          s1 == [".execute_sql()", ".fetch_one()"])
    check("bare .execute() NOT flagged (collides with PluginBase.execute)",
          ".execute()" not in s1)
    check(".transactional() NOT flagged (typed-txn exemption — forks-ruling §4)",
          ".transactional()" not in s1)

    # A typed-txn block (no raw cursor methods inside) is the sanctioned
    # house-style the §4 exemption protects — it must produce ZERO findings.
    typed_src = (
        "def g(state):\n"
        "    with state.transactional() as txn:\n"
        "        txn.update_state('ns', {}, {})\n"
        "        txn.delete_records('ns', {})\n"
    )
    typed_findings = _scan(typed_src, "ananta/src/ananta/y.py")
    check("pure typed-txn block → ZERO findings (sanctioned house-style)",
          typed_findings == [])


def test_s2_raw_sql_string() -> None:
    print("S2 raw-SQL string (literal + f-string + markers; negatives):")
    src = (
        "a = 'SELECT * FROM t WHERE id = 1'\n"
        "b = f'INSERT INTO t VALUES ({v})'\n"
        "c = 'foo bar ON CONFLICT do nothing'\n"
        "d = 'hello world'\n"
        "e = 'SELECT'\n"
        "g = 'Update the config and reload'\n"
        "h = f'SELECT {cols} FROM threads WHERE rowid > ?'\n"
    )
    findings = _scan(src, "plugins/some_plugin/src/some_plugin/x.py")
    s2_lines = {f.line for f in findings if f.category_id == gate.CAT_RAW_SQL}
    check("SELECT…FROM…WHERE literal flagged (line 1)", 1 in s2_lines)
    check("INSERT INTO…VALUES f-string flagged (line 2)", 2 in s2_lines)
    check("ON CONFLICT marker flagged (line 3)", 3 in s2_lines)
    check("non-SQL prose NOT flagged (line 4)", 4 not in s2_lines)
    check("bare 'SELECT' keyword (no space) NOT flagged (line 5)", 5 not in s2_lines)
    check("verb-prose without a SQL companion NOT flagged (line 6 — v1 tuning)",
          6 not in s2_lines)
    check("dynamic f-string SQL with verb+companion straddling {…} flagged "
          "(line 7 — Codex f-string false-negative fix)", 7 in s2_lines)


def test_s3_hand_ddl_partition() -> None:
    print("S3 hand-DDL (partitioned from S2):")
    src = (
        "a = 'CREATE TABLE foo (id int)'\n"
        "b = 'DROP INDEX ix_foo'\n"
        "c = 'Create the cache directory if missing'\n"
    )
    findings = _scan(src, "ananta/src/ananta/x.py")
    s3 = {f.line for f in findings if f.category_id == gate.CAT_DDL}
    s2 = {f.line for f in findings if f.category_id == gate.CAT_RAW_SQL}
    check("CREATE TABLE → S3 (line 1)", 1 in s3 and 1 not in s2)
    check("DROP INDEX → S3 (line 2)", 2 in s3)
    check("prose 'Create the…' NOT flagged (line 3 — v1 tuning)",
          3 not in s3 and 3 not in s2)


def test_docstrings_skipped() -> None:
    print("Docstrings skipped (module/class/function — dominant S2 FP source):")
    src = (
        "'Update the record where it is stale.'\n"
        "def f():\n"
        "    'Create a table of contents from the index.'\n"
        "    q = 'SELECT id FROM t WHERE x = 1'\n"
        "    return q\n"
        "class C:\n"
        "    'Delete entries returning their ids.'\n"
    )
    findings = _scan(src, "ananta/src/ananta/x.py")
    s2 = {f.line for f in findings if f.category_id == gate.CAT_RAW_SQL}
    check("module docstring NOT flagged (line 1)", 1 not in s2)
    check("function docstring 'Create a table…' NOT flagged (line 3)", 3 not in s2)
    check("real SQL in function body STILL flagged (line 4)", 4 in s2)
    check("class docstring with 'returning' NOT flagged (line 7)", 7 not in s2)


def test_owner_exemption() -> None:
    print("Structural owner-exemption (plugins + state-service impl layer — B1):")
    check("postgres state plugin exempt",
          gate._owner_exempt("plugins/postgres_state_management_plugin/src/x.py"))
    check("pgvector plugin exempt",
          gate._owner_exempt("plugins/pgvector_service_plugin/src/x.py"))
    check("state_service/ core-impl exempt (B1)",
          gate._owner_exempt("ananta/src/ananta/services/state_service/__init__.py"))
    check("database_operations/ core-impl exempt (B1)",
          gate._owner_exempt("ananta/src/ananta/services/database_operations/x.py"))
    check("real-debt core caller NOT exempt (stays flagged)",
          not gate._owner_exempt("ananta/src/ananta/core/actions/action_queue_poller.py"))
    # scan_module honors the exemption end-to-end:
    sql = "x = 'SELECT * FROM t WHERE a = 1'\n"
    owner = _scan(sql, "ananta/src/ananta/services/state_service/__init__.py")
    caller = _scan(sql, "ananta/src/ananta/core/actions/action_queue_poller.py")
    check("owner path → 0 findings (suppressed)", len(owner) == 0)
    check("non-owner caller path → flagged", len(caller) >= 1)


def test_allowlist_match_and_fp_convention() -> None:
    print("Allowlist suffix/specifier match + permanent vs migrate convention (N3):")
    allow = gate.Allowlist((
        gate.AllowEntry(gate.CAT_SQL_CALL, "core/orchestration/service_manager.py", "333",
                        is_permanent=True),
        gate.AllowEntry(gate.CAT_RAW_SQL, "llm/session_ledger/read.py", "*",
                        is_permanent=False),
    ))
    fp = gate.Finding(gate.CAT_SQL_CALL,
                      "ananta/src/ananta/core/orchestration/service_manager.py", 333, ".execute_sql()")
    star = gate.Finding(gate.CAT_RAW_SQL, "ananta/src/ananta/llm/session_ledger/read.py", 99, "SELECT …")
    miss_cat = gate.Finding(gate.CAT_DRIVER,
                            "ananta/src/ananta/core/orchestration/service_manager.py", 333, "import psycopg")
    miss_line = gate.Finding(gate.CAT_SQL_CALL,
                             "ananta/src/ananta/core/orchestration/service_manager.py", 999, ".execute_sql()")
    m_fp = allow.match(fp)
    check("suffix + exact-line match (StateServiceAdapter FP)", m_fp is not None)
    check("permanent entry flagged is_permanent", m_fp is not None and m_fp.is_permanent)
    check("'*' wildcard matches any line", allow.match(star) is not None)
    check("category mismatch → no match", allow.match(miss_cat) is None)
    check("line mismatch (no wildcard) → no match", allow.match(miss_line) is None)


def test_note_families_map_to_is_permanent() -> None:
    print("load_allowlist: both permanent note families parse → is_permanent (N3 + GAP-CORE-4):")
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "al.txt"
        f.write_text(
            "S1::a/fp.py::1  # false-positive: not actually SQL\n"
            "S0::a/guard.py::80  # sanctioned-exempt: runtime lockdown enforcement guard (operator)\n"
            "S2::a/debt.py::5  # migrate-with: ledger\n",
            encoding="utf-8",
        )
        al = gate.load_allowlist(f)
    by = {e.path_suffix: e.is_permanent for e in al.entries}
    check("'# false-positive:' → is_permanent True", by.get("a/fp.py") is True)
    check("'# sanctioned-exempt:' → is_permanent True (GAP-CORE-4)", by.get("a/guard.py") is True)
    check("'# migrate-with:' → is_permanent False (removable debt)", by.get("a/debt.py") is False)


def test_require_clean_honors_permanent() -> None:
    print("--require-clean ignores permanent (FP + sanctioned-exempt), fails on real debt:")
    allow = gate.Allowlist((
        gate.AllowEntry(gate.CAT_SQL_CALL, "svc/adapter.py", "10", is_permanent=True),
        gate.AllowEntry(gate.CAT_DRIVER, "svc/guard.py", "80", is_permanent=True),
    ))
    fp_in = gate.Finding(gate.CAT_SQL_CALL, "ananta/src/ananta/svc/adapter.py", 10, ".execute_sql()")
    exempt_in = gate.Finding(gate.CAT_DRIVER, "ananta/src/ananta/svc/guard.py", 80, "import psycopg")
    debt_in = gate.Finding(gate.CAT_SQL_CALL, "ananta/src/ananta/svc/caller.py", 5, ".execute_sql()")
    out_of = gate.Finding(gate.CAT_SQL_CALL, "ananta/src/ananta/other/x.py", 5, ".execute_sql()")
    violations = gate._require_clean_violations(
        [fp_in, exempt_in, debt_in, out_of], allow, ["ananta/src/ananta/svc"])
    paths = {f.path for f in violations}
    check("FP-allowlisted under prefix → NOT a violation",
          "ananta/src/ananta/svc/adapter.py" not in paths)
    check("sanctioned-exempt under prefix → NOT a violation (GAP-CORE-4)",
          "ananta/src/ananta/svc/guard.py" not in paths)
    check("real-debt under prefix → violation",
          "ananta/src/ananta/svc/caller.py" in paths)
    check("finding outside prefix → ignored", "ananta/src/ananta/other/x.py" not in paths)


def test_s2_statement_shape() -> None:
    print("S2 statement-shape (2026-07-02 refinement — verb-led prose rejected, "
          "real statements still flagged):")
    # The three LIVE false-positives this refinement targets: a leading SQL verb
    # + an incidental companion word downstream in prose.
    fps = (
        ("thinking plugin.py 'Create the …'",
         "Create the Work Breakdown Structure document for X from the focused sketch"),
        ("scheduling plugin.py 'Create recurring …using…'",
         "Create recurring scheduled jobs using cron expressions for a table"),
        ("s3 plugin.py 'Delete temporary …from…'",
         "Delete temporary blobs from S3"),
    )
    for label, text in fps:
        check(f"verb-led prose NOT flagged: {label}", gate.classify_sql(text) is None)
    # RED-FIRST — real statements MUST still classify. A FAIL-phase gate that
    # stops seeing real SQL is worse than a false positive.
    check("INSERT INTO … still flagged (closed follower INTO)",
          gate.classify_sql("INSERT INTO t (a) VALUES (1)") == gate.CAT_RAW_SQL)
    check("DELETE FROM … still flagged (closed follower FROM)",
          gate.classify_sql("DELETE FROM t WHERE id = 1") == gate.CAT_RAW_SQL)
    check("CREATE VIEW … AS … still flagged (non-DDL object keyword → S2)",
          gate.classify_sql("CREATE VIEW v AS SELECT id FROM t") == gate.CAT_RAW_SQL)
    check("CREATE TABLE … still flagged as DDL (S3 prefix path, unaffected)",
          gate.classify_sql("CREATE TABLE t (id int)") == gate.CAT_DDL)
    check("SELECT … FROM … still flagged (open-follower verb, unchanged)",
          gate.classify_sql("SELECT id FROM t WHERE x = 1") == gate.CAT_RAW_SQL)
    check("UPDATE … SET … still flagged (open-follower verb, unchanged)",
          gate.classify_sql("UPDATE t SET x = 1 WHERE id = 2") == gate.CAT_RAW_SQL)
    # helper units:
    check("_leading_sql_verb('CREATE THE X') == 'CREATE'",
          gate._leading_sql_verb("CREATE THE X") == "CREATE")
    check("_leading_sql_verb('NOT A VERB') is None",
          gate._leading_sql_verb("NOT A VERB") is None)
    check("_statement_shaped rejects CREATE+prose ('THE')",
          not gate._statement_shaped("CREATE", "CREATE THE X FROM Y"))
    check("_statement_shaped accepts CREATE+object ('VIEW')",
          gate._statement_shaped("CREATE", "CREATE VIEW V AS SELECT ..."))
    check("_statement_shaped: open-follower SELECT always True",
          gate._statement_shaped("SELECT", "SELECT THE X"))


def test_content_anchor() -> None:
    print("Content-anchored specifier (sql:<snippet>) — line-agnostic pin (FIX-2a):")
    snippet = "upsert_session canonical race: Phase 1 ON CONFLICT fired but…"
    allow = gate.Allowlist((
        gate.AllowEntry(gate.CAT_RAW_SQL, "session_ledger/ingest.py",
                        gate._CONTENT_ANCHOR_PREFIX + snippet, is_permanent=True),
    ))
    base = "ananta/src/ananta/llm/session_ledger/ingest.py"
    check("matches at the original line",
          allow.match(gate.Finding(gate.CAT_RAW_SQL, base, 352, snippet)) is not None)
    check("matches after line drift (line-agnostic — the whole point)",
          allow.match(gate.Finding(gate.CAT_RAW_SQL, base, 999, snippet)) is not None)
    check("reworded snippet → NO match (correctly re-surfaces)",
          allow.match(gate.Finding(gate.CAT_RAW_SQL, base, 352, "different text entirely")) is None)
    check("file-scoped → wrong file no match",
          allow.match(gate.Finding(gate.CAT_RAW_SQL, "ananta/src/ananta/other.py", 352, snippet)) is None)
    check("category-scoped → wrong category no match",
          allow.match(gate.Finding(gate.CAT_DDL, base, 352, snippet)) is None)
    # load_allowlist parses a sql: specifier verbatim, even with embedded "::":
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "al.txt"
        f.write_text(
            "S2::a/x.py::sql:SELECT a FROM t -- note::with::colons  # false-positive: fixture\n",
            encoding="utf-8",
        )
        al = gate.load_allowlist(f)
    entry = next(iter(al.entries))
    check("load_allowlist keeps sql:<snippet> verbatim (embedded '::' survives split)",
          entry.specifier == "sql:SELECT a FROM t -- note::with::colons")
    check("content-anchor entry is_permanent (false-positive note)", entry.is_permanent is True)


_SUMMARY_RE = re.compile(
    r"SUMMARY: (\d+) finding\(s\) — (\d+) non-allowlisted, (\d+) allowlisted\."
)
_CANONICAL_ARGS = (
    "--allowlist", "quality_gates/sql_access_allowlist.txt",
    "--require-clean", "ananta/src/ananta/llm/session_ledger",
    "--require-clean", "ananta/src/ananta/llm/agent_messaging",
    "--require-clean", "ananta/src/ananta/core/actions",
    "--require-clean", "plugins/default_knowledge_plugin/src",
    "--require-clean", "plugins/actr_memory_plugin/src",
    "--require-clean", "plugins/default_thinking_plugin/src",
)


def _run_gate(extra_args: tuple[str, ...], cwd: Path) -> tuple[int, str]:
    """Invoke the gate as a real subprocess (exercises actual argv parsing)."""
    proc = subprocess.run(
        [str(_VENV_PYTHON), str(_GATE_DIR / "sql_access_gate.py"), *extra_args],
        cwd=str(cwd), capture_output=True, text=True, timeout=120,
    )
    return proc.returncode, proc.stdout


def _summary_counts(stdout: str) -> tuple[int, int, int]:
    match = _SUMMARY_RE.search(stdout)
    assert match is not None, f"no SUMMARY line in gate output:\n{stdout[-500:]}"
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def test_bare_invocation_matches_canonical_run_vs_run() -> None:
    print("Bare invocation (no args) run-vs-run equivalence with the canonical "
          "invocation (2026-08-14 lane E — the standing false-alarm generator):")
    canonical_code, canonical_out = _run_gate(_CANONICAL_ARGS, cwd=_REPO_ROOT)
    bare_code, bare_out = _run_gate((), cwd=_REPO_ROOT)
    check("canonical invocation exits 0 on a clean tree", canonical_code == 0)
    check("bare invocation now exits the SAME as canonical (was: always exit 1)",
          bare_code == canonical_code)
    check("bare invocation's finding counts match canonical's EXACTLY "
          "(live comparison, not a hardcoded count — the tree's census drifts)",
          _summary_counts(bare_out) == _summary_counts(canonical_out))


def test_bare_invocation_cwd_independent() -> None:
    print("Bare invocation from a subdirectory still resolves REPO_ROOT-relative "
          "defaults (the smokes-cwd trap) and matches canonical run-vs-run:")
    canonical_code, canonical_out = _run_gate(_CANONICAL_ARGS, cwd=_REPO_ROOT)
    subdir_code, subdir_out = _run_gate((), cwd=_GATE_DIR)
    check("bare-from-subdirectory exits the SAME as canonical",
          subdir_code == canonical_code)
    check("bare-from-subdirectory finding counts match canonical EXACTLY",
          _summary_counts(subdir_out) == _summary_counts(canonical_out))


def test_explicit_allowlist_only_unaffected() -> None:
    print("Explicit --allowlist with NO --require-clean (the gate_registry.py / "
          "platform_gates.py caller shape) is byte-untouched by the bare-default fix:")
    allowlist_only_code, allowlist_only_out = _run_gate(
        ("--allowlist", "quality_gates/sql_access_allowlist.txt"), cwd=_REPO_ROOT,
    )
    check("explicit --allowlist-only still exits 0 (unchanged)", allowlist_only_code == 0)
    check("explicit --allowlist-only prints NO --require-clean section "
          "(defaults did NOT get bolted on)",
          "--require-clean" not in allowlist_only_out)
    canonical_code, canonical_out = _run_gate(_CANONICAL_ARGS, cwd=_REPO_ROOT)
    check("explicit --allowlist-only's finding counts still match canonical's "
          "(same tree, same allowlist — only the --require-clean ratchets differ)",
          _summary_counts(allowlist_only_out) == _summary_counts(canonical_out))


def test_explicit_args_override_defaults() -> None:
    print("An explicit --allowlist continues to override the bare-invocation default "
          "(a nonexistent path proves the default was NOT silently substituted):")
    code, out = _run_gate(("--allowlist", "quality_gates/does_not_exist.txt"), cwd=_REPO_ROOT)
    check("nonexistent explicit --allowlist path is honored as an EMPTY allowlist "
          "(load_allowlist returns Allowlist() for a missing path — every finding "
          "surfaces non-allowlisted, proving the canonical default was overridden)",
          code == 1 and _summary_counts(out)[1] > 0)


def main() -> int:
    for test in (
        test_s0_driver_import,
        test_s1_sql_call_and_bare_execute_excluded,
        test_s2_raw_sql_string,
        test_s3_hand_ddl_partition,
        test_docstrings_skipped,
        test_owner_exemption,
        test_allowlist_match_and_fp_convention,
        test_note_families_map_to_is_permanent,
        test_require_clean_honors_permanent,
        test_s2_statement_shape,
        test_content_anchor,
        test_bare_invocation_matches_canonical_run_vs_run,
        test_bare_invocation_cwd_independent,
        test_explicit_allowlist_only_unaffected,
        test_explicit_args_override_defaults,
    ):
        test()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
