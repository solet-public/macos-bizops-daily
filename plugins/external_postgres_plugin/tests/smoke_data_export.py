#!/usr/bin/env python3
"""Data-export + override-friction smoke tests for external_postgres_plugin.

Business-data limits + data-export migration, 2026-08-02
(workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md).
Both run_query and export_query now ALWAYS write to the caller-supplied
output_tsv_path — the former inline-return/INLINE_BYTE_CAP branch is deleted,
not lowered (07-29 data-export requirement, unconditional). Effective row ceiling is
DEFAULT_ROW_LIMIT (500) unless the caller supplies BOTH
acknowledge_default_limit_override=true and an explicit row_limit.

Hermetic — a faked psycopg cursor for both verbs, and the REAL
export_containment gate bound to a temp workspace root (the containment
boundary is exactly what must not be mocked).

Per-verb 4-case behavioral set (§5), for BOTH run_query and export_query:
  1. default call (no override): fetch stops at the connector's default,
     file contains <= default rows, no vendor over-fetch.
  2. override call with a valid row_limit above default and below the hard
     cap: fetch reaches the requested count, not silently capped back down.
  3. malformed override (either half alone): fails loud, names which half
     was missing.
  4. row_limit above the hard cap: refused, names the cap, does not clamp
     silently.

Plus the pre-existing export-containment coverage (A2, unaffected by this
migration): workspace TSV write, path/root/suffix/parent-dir refusals.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_data_export.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin import query_actions  # noqa: E402
from external_postgres_plugin.constants import (  # noqa: E402
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    DEFAULT_ROW_LIMIT,
    EXPORT_ROW_CAP,
    MAX_ROWS_HARD_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
)
from external_postgres_plugin.export_containment import (  # noqa: E402
    ExportPathRefusedError,
    assert_export_path_allowed,
)

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _fake_conn(columns: list[str], rows: list[tuple[Any, ...]]) -> Any:
    """A cursor whose fetchmany returns AT MOST what the caller's cap allows.

    Mirrors a real server-side cursor: fetchmany(n) never returns more than
    n rows, regardless of how many rows the query actually matched.
    """
    cur = MagicMock()
    cur.description = [SimpleNamespace(name=c) for c in columns]
    cur.fetchmany.side_effect = lambda n: rows[:n]
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn


def _gate_for(roots: list[str]) -> Any:
    """The REAL containment gate bound to the given allowed roots."""

    def gate(output_tsv_path: str) -> str:
        return assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name="external_postgres_plugin",
        )

    return gate


def _read_tsv_rows(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return lines[1:]  # drop header


# ---------------------------------------------------------------------------
# run_query: 4-case override-friction set
# ---------------------------------------------------------------------------


def test_run_query_default_stops_at_default() -> None:
    rows = [(i,) for i in range(DEFAULT_ROW_LIMIT + 250)]  # more available than default
    conn = _fake_conn(["id"], rows)
    with tempfile.TemporaryDirectory(prefix="epg_rq_default_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = query_actions.run_query(
            conn, {"sql": "SELECT id FROM t", "output_tsv_path": out_path}, _gate_for([workspace]),
        )
        _assert(
            "run_query default stops at DEFAULT_ROW_LIMIT",
            result["row_count"] == DEFAULT_ROW_LIMIT,
            f"got {result['row_count']}",
        )
        _assert("run_query default: truncated=True (more rows existed)", result["truncated"] is True)
        _assert(
            "run_query default: file has exactly DEFAULT_ROW_LIMIT data rows",
            len(_read_tsv_rows(out_path)) == DEFAULT_ROW_LIMIT,
        )
        _assert("run_query never returns rows/records inline", "rows" not in result and "records" not in result)


def test_run_query_override_reaches_requested_count() -> None:
    requested = DEFAULT_ROW_LIMIT + 300  # above default, below MAX_ROWS_HARD_CAP
    rows = [(i,) for i in range(requested + 50)]  # even more available
    conn = _fake_conn(["id"], rows)
    with tempfile.TemporaryDirectory(prefix="epg_rq_override_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = query_actions.run_query(
            conn,
            {
                "sql": "SELECT id FROM t",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: requested,
            },
            _gate_for([workspace]),
        )
        _assert(
            "run_query override reaches the requested row_limit, not silently capped to default",
            result["row_count"] == requested,
            f"got {result['row_count']}",
        )


def test_run_query_malformed_override_fails_loud() -> None:
    conn = _fake_conn(["id"], [(1,)])
    with tempfile.TemporaryDirectory(prefix="epg_rq_malformed_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        gate = _gate_for([workspace])

        raised_flag_alone = None
        try:
            query_actions.run_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True},
                gate,
            )
        except ValueError as exc:
            raised_flag_alone = exc
        _assert("override flag alone (no row_limit) fails loud", raised_flag_alone is not None)
        _assert(
            "message names row_limit",
            raised_flag_alone is not None and PARAM_ROW_LIMIT in str(raised_flag_alone),
        )

        raised_limit_alone = None
        try:
            query_actions.run_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": out_path, PARAM_ROW_LIMIT: 700},
                gate,
            )
        except ValueError as exc:
            raised_limit_alone = exc
        _assert("row_limit alone (no override flag) fails loud", raised_limit_alone is not None)
        _assert(
            "message names the override flag",
            raised_limit_alone is not None and PARAM_ACKNOWLEDGE_OVERRIDE in str(raised_limit_alone),
        )


def test_run_query_over_hard_cap_refused() -> None:
    conn = _fake_conn(["id"], [(1,)])
    with tempfile.TemporaryDirectory(prefix="epg_rq_overcap_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        raised = None
        try:
            query_actions.run_query(
                conn,
                {
                    "sql": "SELECT id FROM t",
                    "output_tsv_path": out_path,
                    PARAM_ACKNOWLEDGE_OVERRIDE: True,
                    PARAM_ROW_LIMIT: MAX_ROWS_HARD_CAP + 1,
                },
                _gate_for([workspace]),
            )
        except ValueError as exc:
            raised = exc
        _assert("row_limit above MAX_ROWS_HARD_CAP is refused", raised is not None)
        _assert(
            "refusal names the hard cap, not a silent clamp",
            raised is not None and str(MAX_ROWS_HARD_CAP) in str(raised),
        )
        _assert("no file written on refusal", not Path(out_path).exists())


# ---------------------------------------------------------------------------
# export_query: same 4-case set, EXPORT_ROW_CAP ceiling
# ---------------------------------------------------------------------------


def test_export_query_default_stops_at_default() -> None:
    rows = [(i,) for i in range(DEFAULT_ROW_LIMIT + 250)]
    conn = _fake_conn(["id"], rows)
    with tempfile.TemporaryDirectory(prefix="epg_eq_default_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = query_actions.export_query(
            conn, {"sql": "SELECT id FROM t", "output_tsv_path": out_path}, _gate_for([workspace]),
        )
        _assert(
            "export_query default (no override) also stops at DEFAULT_ROW_LIMIT",
            result["row_count"] == DEFAULT_ROW_LIMIT,
            f"got {result['row_count']}",
        )


def test_export_query_override_reaches_requested_count() -> None:
    requested = 12_000
    rows = [(i,) for i in range(requested + 500)]
    conn = _fake_conn(["id"], rows)
    with tempfile.TemporaryDirectory(prefix="epg_eq_override_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = query_actions.export_query(
            conn,
            {
                "sql": "SELECT id FROM t",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: requested,
            },
            _gate_for([workspace]),
        )
        _assert(
            "export_query override reaches the requested row_limit",
            result["row_count"] == requested,
            f"got {result['row_count']}",
        )


def test_export_query_malformed_override_fails_loud() -> None:
    conn = _fake_conn(["id"], [(1,)])
    with tempfile.TemporaryDirectory(prefix="epg_eq_malformed_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        gate = _gate_for([workspace])
        raised = None
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": out_path, PARAM_ROW_LIMIT: 60_000},
                gate,
            )
        except ValueError as exc:
            raised = exc
        _assert("export_query row_limit alone (no override flag) fails loud", raised is not None)


def test_export_query_over_hard_cap_refused() -> None:
    conn = _fake_conn(["id"], [(1,)])
    with tempfile.TemporaryDirectory(prefix="epg_eq_overcap_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        raised = None
        try:
            query_actions.export_query(
                conn,
                {
                    "sql": "SELECT id FROM t",
                    "output_tsv_path": out_path,
                    PARAM_ACKNOWLEDGE_OVERRIDE: True,
                    PARAM_ROW_LIMIT: EXPORT_ROW_CAP + 1,
                },
                _gate_for([workspace]),
            )
        except ValueError as exc:
            raised = exc
        _assert("export_query row_limit above EXPORT_ROW_CAP is refused", raised is not None)
        _assert(
            "refusal names the cap, not a silent clamp",
            raised is not None and str(EXPORT_ROW_CAP) in str(raised),
        )


# ---------------------------------------------------------------------------
# Pre-existing export-containment coverage (A2) — unaffected by this migration
# ---------------------------------------------------------------------------


def test_export_gate_binds_via_initialize() -> None:
    # Red-first (live defect class 2026-07-16): the plugin never overrode
    # initialize(), so config_provider stayed None and a configured
    # export_allowed_roots was silently ignored (refuse-all).
    from external_postgres_plugin.plugin import ExternalPostgresPlugin  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="epg_gate_smoke_") as workspace:
        plugin = ExternalPostgresPlugin()
        plugin.initialize({CONFIG_KEY_EXPORT_ALLOWED_ROOTS: [workspace]})
        admitted = plugin._export_path_gate(str(Path(workspace) / "out.tsv"))
        _assert(
            "initialize() binds config; configured root admits the path",
            admitted.endswith("out.tsv"),
        )


def test_export_gate_unbound_provider_fails_loud() -> None:
    # Fast-fail: a plugin whose lifecycle skipped initialize() must raise a
    # config fault naming the missing binding — never silently refuse-all
    # (the pre-2026-07-16 'or {}' behavior this replaces).
    from external_postgres_plugin.app_config import ExternalPgConfigError  # noqa: PLC0415
    from external_postgres_plugin.plugin import ExternalPostgresPlugin  # noqa: PLC0415

    plugin = ExternalPostgresPlugin()
    raised = None
    try:
        plugin._export_path_gate("/tmp/out.tsv")
    except ExternalPgConfigError as exc:
        raised = exc
    _assert("unbound config_provider raises a config fault", raised is not None)
    _assert(
        "fault names initialize()",
        raised is not None and "initialize" in str(raised),
    )


def test_export_tsv_to_workspace() -> None:
    with tempfile.TemporaryDirectory(prefix="epg_export_smoke_") as workspace:
        conn = _fake_conn(["id", "name"], [(1, "alice"), (2, "bob")])
        out_path = str(Path(workspace) / "customers.tsv")
        result = query_actions.export_query(
            conn,
            {"sql": "SELECT id, name FROM t", "output_tsv_path": out_path},
            _gate_for([workspace]),
        )
        _assert("export returns the written path", result["path"] == str(Path(out_path).resolve()))
        _assert("export row_count", result["row_count"] == 2)
        _assert("export columns", result["columns"] == ["id", "name"])
        _assert("export not truncated", result["truncated"] is False)
        text = Path(result["path"]).read_text(encoding="utf-8")
        lines = text.splitlines()
        _assert("tsv header row is tab-separated", lines[0] == "id\tname")
        _assert("tsv data rows", "1\talice" in lines and "2\tbob" in lines)
        _assert("no blob key in the result", "result_blob_key" not in result)


def test_export_refused_outside_root() -> None:
    with (
        tempfile.TemporaryDirectory(prefix="epg_root_") as workspace,
        tempfile.TemporaryDirectory(prefix="epg_outside_") as outside,
    ):
        conn = _fake_conn(["id"], [(1,)])
        out_path = str(Path(outside) / "escape.tsv")
        raised = None
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": out_path},
                _gate_for([workspace]),
            )
        except ExportPathRefusedError as exc:
            raised = exc
        _assert("outside-root path is refused", raised is not None)
        _assert("no file written on refusal", not Path(out_path).exists())
        _assert(
            "statement never executed on refusal (gate runs first)",
            not conn.cursor.called,
        )


def test_export_refused_empty_roots() -> None:
    conn = _fake_conn(["id"], [(1,)])
    raised = None
    try:
        query_actions.export_query(
            conn,
            {"sql": "SELECT id FROM t", "output_tsv_path": "/tmp/anywhere.tsv"},
            _gate_for([]),
        )
    except ExportPathRefusedError as exc:
        raised = exc
    _assert("empty export_allowed_roots refuses (refuse-all default)", raised is not None)
    _assert(
        "refusal names the config key",
        raised is not None and CONFIG_KEY_EXPORT_ALLOWED_ROOTS in str(raised),
    )


def test_export_refused_misconfigured_roots() -> None:
    # Codex-proven hole (2026-07-15 Wave-2 review): a RELATIVE configured root
    # realpaths against the SERVER process cwd and admitted paths under it; a
    # BLANK root realpaths TO the cwd. Both must be refused as config faults.
    conn = _fake_conn(["id"], [(1,)])
    cwd_relative_target = str(Path.cwd() / "relative-root" / "out.tsv")
    for bad_roots, label in (
        (["relative-root"], "relative configured root is refused"),
        ([""], "blank configured root is refused"),
    ):
        raised = None
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": cwd_relative_target},
                _gate_for(bad_roots),
            )
        except ExportPathRefusedError as exc:
            raised = exc
        _assert(label, raised is not None)
        _assert(
            f"{label}: message names the offending entry as misconfigured",
            raised is not None and "misconfigured" in str(raised),
        )


def test_export_refused_relative_path() -> None:
    with tempfile.TemporaryDirectory(prefix="epg_root_") as workspace:
        conn = _fake_conn(["id"], [(1,)])
        raised = False
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": "exports/out.tsv"},
                _gate_for([workspace]),
            )
        except ExportPathRefusedError:
            raised = True
        _assert("relative output_tsv_path is refused", raised)


def test_export_refused_wrong_suffix() -> None:
    with tempfile.TemporaryDirectory(prefix="epg_root_") as workspace:
        conn = _fake_conn(["id"], [(1,)])
        raised = False
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": str(Path(workspace) / "out.csv")},
                _gate_for([workspace]),
            )
        except ExportPathRefusedError:
            raised = True
        _assert("non-.tsv suffix is refused", raised)


def test_export_missing_parent_raises() -> None:
    with tempfile.TemporaryDirectory(prefix="epg_root_") as workspace:
        conn = _fake_conn(["id"], [(1,)])
        out_path = str(Path(workspace) / "no_such_dir" / "out.tsv")
        raised = False
        try:
            query_actions.export_query(
                conn,
                {"sql": "SELECT id FROM t", "output_tsv_path": out_path},
                _gate_for([workspace]),
            )
        except ValueError:
            raised = True
        _assert("missing parent directory raises ValueError", raised)


def main() -> int:
    print("\nexternal_postgres_plugin data-export/override smoke tests")
    print("=" * 60)
    test_run_query_default_stops_at_default()
    test_run_query_override_reaches_requested_count()
    test_run_query_malformed_override_fails_loud()
    test_run_query_over_hard_cap_refused()
    test_export_query_default_stops_at_default()
    test_export_query_override_reaches_requested_count()
    test_export_query_malformed_override_fails_loud()
    test_export_query_over_hard_cap_refused()
    test_export_gate_binds_via_initialize()
    test_export_gate_unbound_provider_fails_loud()
    test_export_tsv_to_workspace()
    test_export_refused_outside_root()
    test_export_refused_empty_roots()
    test_export_refused_misconfigured_roots()
    test_export_refused_relative_path()
    test_export_refused_wrong_suffix()
    test_export_missing_parent_raises()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All data-export/override smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
