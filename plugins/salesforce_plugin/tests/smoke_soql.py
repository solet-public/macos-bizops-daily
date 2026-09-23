#!/usr/bin/env python3
"""SOQL query + export + override-friction smoke tests for salesforce_plugin.

Business-data limits + data-export migration, 2026-08-02
(workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md).
Both soql_query and export_soql now ALWAYS write to the caller-supplied
output_tsv_path — the former inline-return/INLINE_BYTE_CAP branch is deleted,
not lowered (07-29 data-export requirement, unconditional). Effective record ceiling is
DEFAULT_ROW_LIMIT (500) unless the caller supplies BOTH
acknowledge_default_limit_override=true and an explicit row_limit.

Hermetic — a ``MagicMock`` standing in for ``SalesforceCliExecutor``
(``run_json`` mocked directly), no live org, no subprocess; the export half
drives the REAL export_containment gate bound to a temp workspace root (the
containment boundary is exactly what must not be mocked).

No manual pagination: `sf data query` autoFetches internally (verified by
reading the CLI's own source — see `soql_actions.py`'s module docstring), so
these tests cover the query-file lifecycle, the `SF_ORG_MAX_QUERY_LIMIT` env
passthrough, the client-side cap logic, and the A3 workspace-TSV contract.

Exercises:
  1. soql_query — writes a TSV handle, attributes key stripped, never
     records/rows inline
  2. soql_query — writes the query text to a tempfile, passes `--file <path>`,
     and cleans the tempfile up afterward
  3. soql_query — passes SF_ORG_MAX_QUERY_LIMIT as an env override matching
     the effective limit (server-side cap)
  4. soql_query — client-side slice still caps at the effective limit even if
     the executor returns more (defense-in-depth against a server-side cap miss)
  5-8. soql_query — 4-case override-friction set (§5): default-caps,
     override-succeeds, malformed-refused (either half alone),
     cap-exceeded-refused
  9-12. export_soql — same 4-case override-friction set, SOQL_EXPORT_ROW_CAP ceiling
  13. export_soql — writes a TSV at the caller's absolute path under the
     allowed root; column order follows first appearance; nested objects
     serialize as JSON text; returns {path, columns, row_count, total_size,
     truncated}
  14. red-first: export path OUTSIDE every allowed root -> ExportPathRefusedError,
     no file written, the query never runs
  15. red-first: EMPTY export_allowed_roots -> refused naming the config key
  16. red-first: RELATIVE or BLANK configured root -> refused as misconfigured
  17. red-first: export_soql truncated=True on done=false ALONE (totalSize equal
      to the fetched count — Salesforce's own incompleteness signal wins)
  18. export_soql truncated flag set when the org reports more rows than fetched

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/salesforce_plugin/tests/smoke_soql.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "salesforce_plugin" / "src"))

from salesforce_plugin import soql_actions  # noqa: E402
from salesforce_plugin.constants import (  # noqa: E402
    CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
    DEFAULT_ROW_LIMIT,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    SOQL_EXPORT_ROW_CAP,
    SOQL_MAX_RECORDS_CAP,
)
from salesforce_plugin.export_containment import (  # noqa: E402
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


def _gate_for(roots: list[str]) -> Any:
    """The REAL containment gate bound to the given allowed roots."""

    def gate(output_tsv_path: str) -> str:
        return assert_export_path_allowed(
            output_tsv_path,
            roots,
            config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS,
            plugin_name="salesforce_plugin",
        )

    return gate


def _passthrough_gate(path: str) -> str:
    """A no-op containment gate for shape-only tests — real containment is covered below."""
    return path


def _executor_returning(records: list[dict[str, Any]], *, total_size: int, done: bool = True) -> Any:
    executor = MagicMock()
    executor.run_json.return_value = {"totalSize": total_size, "done": done, "records": records}
    return executor


def test_soql_query_writes_tsv() -> None:
    executor = _executor_returning(
        [
            {"attributes": {"type": "Account"}, "Id": "001x1", "Name": "Acme"},
            {"attributes": {"type": "Account"}, "Id": "001x2", "Name": "Globex"},
        ],
        total_size=2,
    )
    with tempfile.TemporaryDirectory(prefix="sf_shape_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = soql_actions.soql_query(
            executor, {"query": "SELECT Id, Name FROM Account", "output_tsv_path": out_path}, _passthrough_gate,
        )
        _assert("row_count matches", result["row_count"] == 2)
        _assert("total_size carried", result["total_size"] == 2)
        _assert("not truncated", result["truncated"] is False)
        _assert("no records/rows field — never inline", "records" not in result and "rows" not in result)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("attributes key stripped from the tsv columns", "attributes" not in lines[0])
        _assert("record fields carried", "Acme" in lines[1])


def test_query_file_written_and_cleaned_up() -> None:
    executor = MagicMock()
    executor.run_json.return_value = {"totalSize": 0, "done": True, "records": []}
    written_path: str = ""

    def _capture_run_json(argv_tail: list[str], *, env_overrides: dict[str, str] | None = None) -> dict[str, Any]:
        nonlocal written_path
        file_flag_index = argv_tail.index("--file")
        written_path = argv_tail[file_flag_index + 1]
        _assert("query file exists during the call", Path(written_path).exists())
        _assert("query file carries the SOQL text", Path(written_path).read_text() == "SELECT Id FROM Account")
        return {"totalSize": 0, "done": True, "records": []}

    executor.run_json.side_effect = _capture_run_json
    with tempfile.TemporaryDirectory(prefix="sf_filelife_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        soql_actions.soql_query(
            executor, {"query": "SELECT Id FROM Account", "output_tsv_path": out_path}, _passthrough_gate,
        )
    _assert("query file cleaned up after the call", not Path(written_path).exists())


def test_max_query_limit_env_passthrough() -> None:
    executor = MagicMock()
    executor.run_json.return_value = {"totalSize": 0, "done": True, "records": []}
    with tempfile.TemporaryDirectory(prefix="sf_env_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        soql_actions.soql_query(
            executor,
            {
                "query": "SELECT Id FROM Account",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: 50,
            },
            _passthrough_gate,
        )
    kwargs = executor.run_json.call_args.kwargs
    _assert(
        "SF_ORG_MAX_QUERY_LIMIT env override matches the effective limit",
        kwargs.get("env_overrides") == {"SF_ORG_MAX_QUERY_LIMIT": "50"},
        str(kwargs),
    )


def test_client_side_cap_defense_in_depth() -> None:
    executor = _executor_returning(
        [{"attributes": {}, "Id": str(i)} for i in range(10)], total_size=10,
    )
    with tempfile.TemporaryDirectory(prefix="sf_defense_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = soql_actions.soql_query(
            executor,
            {
                "query": "SELECT Id FROM Account",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: 3,
            },
            _passthrough_gate,
        )
        _assert("client-side slice caps at the effective limit", result["row_count"] == 3, result["row_count"])
        _assert("total_size still reflects the true total", result["total_size"] == 10, result["total_size"])


# ---------------------------------------------------------------------------
# soql_query: 4-case override-friction set
# ---------------------------------------------------------------------------


def test_soql_query_default_stops_at_default() -> None:
    executor = _executor_returning(
        [{"attributes": {}, "Id": str(i)} for i in range(DEFAULT_ROW_LIMIT + 100)],
        total_size=DEFAULT_ROW_LIMIT + 100,
    )
    with tempfile.TemporaryDirectory(prefix="sf_sq_default_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = soql_actions.soql_query(
            executor, {"query": "SELECT Id FROM Account", "output_tsv_path": out_path}, _gate_for([workspace]),
        )
        _assert(
            "soql_query default stops at DEFAULT_ROW_LIMIT",
            result["row_count"] == DEFAULT_ROW_LIMIT,
            f"got {result['row_count']}",
        )
        _assert("soql_query default: truncated=True (more records existed)", result["truncated"] is True)


def test_soql_query_override_reaches_requested_count() -> None:
    requested = DEFAULT_ROW_LIMIT + 200
    executor = _executor_returning(
        [{"attributes": {}, "Id": str(i)} for i in range(requested + 50)], total_size=requested + 50,
    )
    with tempfile.TemporaryDirectory(prefix="sf_sq_override_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = soql_actions.soql_query(
            executor,
            {
                "query": "SELECT Id FROM Account",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: requested,
            },
            _gate_for([workspace]),
        )
        _assert(
            "soql_query override reaches the requested row_limit",
            result["row_count"] == requested,
            f"got {result['row_count']}",
        )


def test_soql_query_malformed_override_fails_loud() -> None:
    executor = _executor_returning([{"attributes": {}, "Id": "1"}], total_size=1)
    with tempfile.TemporaryDirectory(prefix="sf_sq_malformed_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        gate = _gate_for([workspace])

        raised_flag_alone = None
        try:
            soql_actions.soql_query(
                executor,
                {"query": "SELECT Id FROM Account", "output_tsv_path": out_path, PARAM_ACKNOWLEDGE_OVERRIDE: True},
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
            soql_actions.soql_query(
                executor,
                {"query": "SELECT Id FROM Account", "output_tsv_path": out_path, PARAM_ROW_LIMIT: 700},
                gate,
            )
        except ValueError as exc:
            raised_limit_alone = exc
        _assert("row_limit alone (no override flag) fails loud", raised_limit_alone is not None)
        _assert(
            "message names the override flag",
            raised_limit_alone is not None and PARAM_ACKNOWLEDGE_OVERRIDE in str(raised_limit_alone),
        )


def test_soql_query_over_hard_cap_refused() -> None:
    executor = _executor_returning([{"attributes": {}, "Id": "1"}], total_size=1)
    with tempfile.TemporaryDirectory(prefix="sf_sq_overcap_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        raised = None
        try:
            soql_actions.soql_query(
                executor,
                {
                    "query": "SELECT Id FROM Account",
                    "output_tsv_path": out_path,
                    PARAM_ACKNOWLEDGE_OVERRIDE: True,
                    PARAM_ROW_LIMIT: SOQL_MAX_RECORDS_CAP + 1,
                },
                _gate_for([workspace]),
            )
        except ValueError as exc:
            raised = exc
        _assert("row_limit above SOQL_MAX_RECORDS_CAP is refused", raised is not None)
        _assert(
            "refusal names the hard cap, not a silent clamp",
            raised is not None and str(SOQL_MAX_RECORDS_CAP) in str(raised),
        )
        _assert("no file written on refusal", not Path(out_path).exists())


# ---------------------------------------------------------------------------
# export_soql: same 4-case set, SOQL_EXPORT_ROW_CAP ceiling
# ---------------------------------------------------------------------------


def test_export_soql_default_stops_at_default() -> None:
    executor = _executor_returning(
        [{"attributes": {}, "Id": str(i)} for i in range(DEFAULT_ROW_LIMIT + 100)],
        total_size=DEFAULT_ROW_LIMIT + 100,
    )
    with tempfile.TemporaryDirectory(prefix="sf_eq_default_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = soql_actions.export_soql(
            executor, {"query": "SELECT Id FROM Account", "output_tsv_path": out_path}, _gate_for([workspace]),
        )
        _assert(
            "export_soql default (no override) also stops at DEFAULT_ROW_LIMIT",
            result["row_count"] == DEFAULT_ROW_LIMIT,
            f"got {result['row_count']}",
        )


def test_export_soql_override_reaches_requested_count() -> None:
    requested = 12_000
    executor = _executor_returning(
        [{"attributes": {}, "Id": str(i)} for i in range(requested + 500)], total_size=requested + 500,
    )
    with tempfile.TemporaryDirectory(prefix="sf_eq_override_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        result = soql_actions.export_soql(
            executor,
            {
                "query": "SELECT Id FROM Account",
                "output_tsv_path": out_path,
                PARAM_ACKNOWLEDGE_OVERRIDE: True,
                PARAM_ROW_LIMIT: requested,
            },
            _gate_for([workspace]),
        )
        _assert(
            "export_soql override reaches the requested row_limit",
            result["row_count"] == requested,
            f"got {result['row_count']}",
        )


def test_export_soql_malformed_override_fails_loud() -> None:
    executor = _executor_returning([{"attributes": {}, "Id": "1"}], total_size=1)
    with tempfile.TemporaryDirectory(prefix="sf_eq_malformed_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        raised = None
        try:
            soql_actions.export_soql(
                executor,
                {"query": "SELECT Id FROM Account", "output_tsv_path": out_path, PARAM_ROW_LIMIT: 60_000},
                _gate_for([workspace]),
            )
        except ValueError as exc:
            raised = exc
        _assert("export_soql row_limit alone (no override flag) fails loud", raised is not None)


def test_export_soql_over_hard_cap_refused() -> None:
    executor = _executor_returning([{"attributes": {}, "Id": "1"}], total_size=1)
    with tempfile.TemporaryDirectory(prefix="sf_eq_overcap_") as workspace:
        out_path = str(Path(workspace) / "out.tsv")
        raised = None
        try:
            soql_actions.export_soql(
                executor,
                {
                    "query": "SELECT Id FROM Account",
                    "output_tsv_path": out_path,
                    PARAM_ACKNOWLEDGE_OVERRIDE: True,
                    PARAM_ROW_LIMIT: SOQL_EXPORT_ROW_CAP + 1,
                },
                _gate_for([workspace]),
            )
        except ValueError as exc:
            raised = exc
        _assert("export_soql row_limit above SOQL_EXPORT_ROW_CAP is refused", raised is not None)
        _assert(
            "refusal names the cap, not a silent clamp",
            raised is not None and str(SOQL_EXPORT_ROW_CAP) in str(raised),
        )


# ---------------------------------------------------------------------------
# Pre-existing export-containment + truncation-signal coverage — unaffected
# ---------------------------------------------------------------------------


def test_export_soql_to_workspace() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_export_smoke_") as workspace:
        executor = _executor_returning(
            [
                {"attributes": {}, "Id": "001x1", "Name": "Acme", "Owner": {"Name": "Pat"}},
                {"attributes": {}, "Id": "001x2", "Name": "Globex", "AnnualRevenue": 5},
            ],
            total_size=2,
        )
        out_path = str(Path(workspace) / "accounts.tsv")
        result = soql_actions.export_soql(
            executor,
            {"query": "SELECT Id, Name FROM Account", "output_tsv_path": out_path},
            _gate_for([workspace]),
        )
        _assert("export returns the written path", result["path"] == str(Path(out_path).resolve()))
        _assert("export row_count", result["row_count"] == 2)
        _assert(
            "columns follow first appearance across records",
            result["columns"] == ["Id", "Name", "Owner", "AnnualRevenue"],
            str(result["columns"]),
        )
        _assert("export not truncated", result["truncated"] is False)
        lines = Path(result["path"]).read_text(encoding="utf-8").splitlines()
        _assert("tsv header row is tab-separated", lines[0] == "Id\tName\tOwner\tAnnualRevenue")
        _assert(
            "nested relationship object serialized as JSON text",
            '""Name"": ""Pat""' in lines[1],  # csv-quoted JSON cell in the TSV
            lines[1],
        )
        _assert(
            "default cap passed as the env override (no acknowledged override given)",
            executor.run_json.call_args.kwargs.get("env_overrides")
            == {"SF_ORG_MAX_QUERY_LIMIT": str(DEFAULT_ROW_LIMIT)},
        )


def test_export_refused_outside_root() -> None:
    with (
        tempfile.TemporaryDirectory(prefix="sf_root_") as workspace,
        tempfile.TemporaryDirectory(prefix="sf_outside_") as outside,
    ):
        executor = MagicMock()
        out_path = str(Path(outside) / "escape.tsv")
        raised = None
        try:
            soql_actions.export_soql(
                executor,
                {"query": "SELECT Id FROM Account", "output_tsv_path": out_path},
                _gate_for([workspace]),
            )
        except ExportPathRefusedError as exc:
            raised = exc
        _assert("outside-root path is refused", raised is not None)
        _assert("no file written on refusal", not Path(out_path).exists())
        _assert("query never runs on refusal (gate runs first)", not executor.run_json.called)


def test_export_refused_empty_roots() -> None:
    executor = MagicMock()
    raised = None
    try:
        soql_actions.export_soql(
            executor,
            {"query": "SELECT Id FROM Account", "output_tsv_path": "/tmp/anywhere.tsv"},
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
    executor = MagicMock()
    cwd_relative_target = str(Path.cwd() / "relative-root" / "out.tsv")
    for bad_roots, label in (
        (["relative-root"], "relative configured root is refused"),
        ([""], "blank configured root is refused"),
    ):
        raised = None
        try:
            soql_actions.export_soql(
                executor,
                {"query": "SELECT Id FROM Account", "output_tsv_path": cwd_relative_target},
                _gate_for(bad_roots),
            )
        except ExportPathRefusedError as exc:
            raised = exc
        _assert(label, raised is not None)
        _assert(
            f"{label}: message names the offending entry as misconfigured",
            raised is not None and "misconfigured" in str(raised),
        )


def test_export_truncated_on_done_false_alone() -> None:
    # Codex Wave-3 A3-1 red-first: done=false with totalSize EQUAL to the
    # fetched count must still flag truncation — Salesforce's own
    # incompleteness signal wins even when the counts look complete.
    with tempfile.TemporaryDirectory(prefix="sf_root_") as workspace:
        executor = _executor_returning([{"attributes": {}, "Id": "001x1"}], total_size=1, done=False)
        out_path = str(Path(workspace) / "done_false.tsv")
        result = soql_actions.export_soql(
            executor,
            {"query": "SELECT Id FROM Account", "output_tsv_path": out_path},
            _gate_for([workspace]),
        )
        _assert("truncated True on done=false alone", result["truncated"] is True)


def test_export_truncated_when_org_reports_more() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_root_") as workspace:
        executor = _executor_returning(
            [{"attributes": {}, "Id": "001x1"}], total_size=99999, done=False,  # org reports more than fetched
        )
        out_path = str(Path(workspace) / "partial.tsv")
        result = soql_actions.export_soql(
            executor,
            {"query": "SELECT Id FROM Account", "output_tsv_path": out_path},
            _gate_for([workspace]),
        )
        _assert("truncated True when total_size exceeds fetched rows", result["truncated"] is True)


def main() -> int:
    print("\nsalesforce_plugin SOQL query/export/override smoke tests")
    print("=" * 58)
    test_soql_query_writes_tsv()
    test_query_file_written_and_cleaned_up()
    test_max_query_limit_env_passthrough()
    test_client_side_cap_defense_in_depth()
    test_soql_query_default_stops_at_default()
    test_soql_query_override_reaches_requested_count()
    test_soql_query_malformed_override_fails_loud()
    test_soql_query_over_hard_cap_refused()
    test_export_soql_default_stops_at_default()
    test_export_soql_override_reaches_requested_count()
    test_export_soql_malformed_override_fails_loud()
    test_export_soql_over_hard_cap_refused()
    test_export_soql_to_workspace()
    test_export_refused_outside_root()
    test_export_refused_empty_roots()
    test_export_refused_misconfigured_roots()
    test_export_truncated_on_done_false_alone()
    test_export_truncated_when_org_reports_more()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All SOQL query/export/override smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
