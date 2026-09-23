#!/usr/bin/env python3
"""Bulk API v2 ingest smoke tests for salesforce_plugin (lane-sf-bulk-v2, 2026-08-24).

Hermetic — a ``MagicMock`` standing in for ``SalesforceCliExecutor``
(``run_json``/``run_rest`` mocked directly), no live org, no subprocess; the
containment half drives the REAL import_containment/export_containment gates
bound to a temp workspace root (the containment boundary is exactly what
must not be mocked — mirrors smoke_soql.py's own pattern).

Exercises:
  1. bulk_ingest_submit — argv shape per operation (insert/update/delete);
     upsert argv carries --external-id
  2. bulk_ingest_submit — upsert REQUIRES external_id_field (rejected absent);
     external_id_field REJECTED for every non-upsert operation
  3. bulk_ingest_submit — hardDelete is explicitly excluded (ValueError, never
     reaches the executor)
  4. bulk_ingest_submit — unknown operation rejected
  5. bulk_ingest_submit — csv_path containment: refused outside every
     import_allowed_roots entry, refused when import_allowed_roots is empty
     (refuse-all default), refused when the file does not exist under an
     otherwise-allowed root — none of these ever reach the executor
  6. bulk_ingest_submit — csv_path containment: ADMITTED when the file exists
     under an allowed root (positive case)
  7. bulk_job_status — state/count passthrough, including a Failed state with
     errorMessage carried through
  8. bulk_job_abort — PATCH .../state=Aborted, same state-summary shape
  9. bulk_job_results — writes all three CSVs (successful/failed/unprocessed)
     under export_allowed_roots containment (same gate export_soql uses, .csv
     suffix); refused outside the allowed root, no file written, no REST call
     made on refusal
  10. TOPOLOGY-LEAK (SECURITY): an unrecognized bulk job response shape
      raises a classifiable error, never a raw exception string
  11. EDGE parity: all 13 verbs (this smoke only asserts the 4 bulk verbs are
      present + the total; smoke_records.py/smoke_config.py own the full
      validator run)

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/salesforce_plugin/tests/smoke_bulk.py

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

from salesforce_plugin import bulk_actions  # noqa: E402
from salesforce_plugin.constants import CSV_SUFFIX  # noqa: E402
from salesforce_plugin.errors import SalesforceCliCallError, classify_salesforce_error  # noqa: E402
from salesforce_plugin.export_containment import (  # noqa: E402
    ExportPathRefusedError,
    assert_export_path_allowed,
)
from salesforce_plugin.import_containment import (  # noqa: E402
    ImportPathRefusedError,
    assert_import_path_allowed,
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


def _fake_executor(*, api_version: str = "62.0") -> MagicMock:
    executor = MagicMock()
    executor.api_version = api_version
    return executor


def _job_info(**overrides: Any) -> dict[str, Any]:
    base = {"id": "750xx000001AAAA", "state": "Open", "numberRecordsProcessed": 0, "numberRecordsFailed": 0}
    base.update(overrides)
    return base


def _real_import_gate(allowed_roots: list[str]):
    def gate(csv_path: str) -> str:
        return assert_import_path_allowed(
            csv_path, allowed_roots, config_key="import_allowed_roots", plugin_name="salesforce_plugin",
        )
    return gate


def _real_export_gate(allowed_roots: list[str]):
    def gate(output_csv_path: str) -> str:
        return assert_export_path_allowed(
            output_csv_path,
            allowed_roots,
            config_key="export_allowed_roots",
            plugin_name="salesforce_plugin",
            required_suffix=CSV_SUFFIX,
        )
    return gate


def test_submit_argv_shape_per_operation() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_bulk_smoke_") as workspace:
        csv_path = str(Path(workspace) / "records.csv")
        Path(csv_path).write_text("Id,Name\n001x,Acme\n", encoding="utf-8")
        # The gate realpath-resolves csv_path (e.g. macOS /var -> /private/var);
        # argv carries the RESOLVED path, so compare against that, not the input.
        resolved_csv_path = str(Path(csv_path).resolve())
        gate = _real_import_gate([workspace])

        for operation in ("insert", "update", "delete"):
            executor = _fake_executor()
            executor.run_json.return_value = _job_info()
            bulk_actions.bulk_ingest_submit(
                executor, {"operation": operation, "sobject": "Account", "csv_path": csv_path}, gate,
            )
            argv = executor.run_json.call_args.args[0]
            _assert(
                f"{operation}: argv shape",
                argv == ["data", operation, "bulk", "--sobject", "Account", "--file", resolved_csv_path, "--async"],
                str(argv),
            )

        executor = _fake_executor()
        executor.run_json.return_value = _job_info()
        bulk_actions.bulk_ingest_submit(
            executor,
            {
                "operation": "upsert", "sobject": "Account", "csv_path": csv_path,
                "external_id_field": "External_Id__c",
            },
            gate,
        )
        argv = executor.run_json.call_args.args[0]
        _assert(
            "upsert: argv carries --external-id",
            argv == [
                "data", "upsert", "bulk", "--sobject", "Account", "--file", resolved_csv_path, "--async",
                "--external-id", "External_Id__c",
            ],
            str(argv),
        )


def test_upsert_external_id_field_pairing() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_bulk_smoke_") as workspace:
        csv_path = str(Path(workspace) / "records.csv")
        Path(csv_path).write_text("Id\n001x\n", encoding="utf-8")
        gate = _real_import_gate([workspace])
        executor = _fake_executor()

        raised = False
        try:
            bulk_actions.bulk_ingest_submit(
                executor, {"operation": "upsert", "sobject": "Account", "csv_path": csv_path}, gate,
            )
        except ValueError:
            raised = True
        _assert("upsert without external_id_field is rejected", raised)
        _assert("rejected before the executor is ever called", not executor.run_json.called)

        raised = False
        try:
            bulk_actions.bulk_ingest_submit(
                executor,
                {
                    "operation": "insert", "sobject": "Account", "csv_path": csv_path,
                    "external_id_field": "Whatever__c",
                },
                gate,
            )
        except ValueError:
            raised = True
        _assert("external_id_field given for a non-upsert operation is rejected", raised)
        _assert("rejected before the executor is ever called (non-upsert case)", not executor.run_json.called)


def test_hard_delete_and_unknown_operation_excluded() -> None:
    gate = _real_import_gate(["/nonexistent"])
    executor = _fake_executor()

    raised = None
    try:
        bulk_actions.bulk_ingest_submit(
            executor, {"operation": "hardDelete", "sobject": "Account", "csv_path": "/tmp/x.csv"}, gate,
        )
    except ValueError as exc:
        raised = exc
    _assert("hardDelete is excluded", raised is not None)
    _assert("hardDelete refusal names the exclusion reason", "hardDelete" in str(raised), str(raised))
    _assert("hardDelete never reaches the executor", not executor.run_json.called)

    raised = None
    try:
        bulk_actions.bulk_ingest_submit(
            executor, {"operation": "merge", "sobject": "Account", "csv_path": "/tmp/x.csv"}, gate,
        )
    except ValueError as exc:
        raised = exc
    _assert("an unknown operation is rejected", raised is not None)
    _assert("unknown-operation refusal never reaches the executor", not executor.run_json.called)


def test_submit_csv_path_containment_refusals() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_bulk_smoke_") as workspace:
        allowed_dir = Path(workspace) / "allowed"
        allowed_dir.mkdir()
        outside_path = str(Path(workspace) / "outside.csv")
        Path(outside_path).write_text("Id\n001x\n", encoding="utf-8")

        # Refused: path exists but lies outside every allowed root.
        gate = _real_import_gate([str(allowed_dir)])
        executor = _fake_executor()
        raised = False
        try:
            bulk_actions.bulk_ingest_submit(
                executor, {"operation": "insert", "sobject": "Account", "csv_path": outside_path}, gate,
            )
        except ImportPathRefusedError:
            raised = True
        _assert("csv_path outside every allowed root is refused", raised)
        _assert("outside-root refusal never reaches the executor", not executor.run_json.called)

        # Refused: empty import_allowed_roots (refuse-all default).
        empty_gate = _real_import_gate([])
        raised = False
        try:
            bulk_actions.bulk_ingest_submit(
                executor, {"operation": "insert", "sobject": "Account", "csv_path": outside_path}, empty_gate,
            )
        except ImportPathRefusedError as exc:
            raised = True
            _assert("empty-roots refusal names the config key", "import_allowed_roots" in str(exc), str(exc))
        _assert("empty import_allowed_roots refuses (refuse-all default)", raised)

        # Refused: contained under the allowed root, but the file does not exist.
        missing_path = str(allowed_dir / "missing.csv")
        raised = False
        try:
            bulk_actions.bulk_ingest_submit(
                executor, {"operation": "insert", "sobject": "Account", "csv_path": missing_path}, gate,
            )
        except ImportPathRefusedError:
            raised = True
        _assert("a contained but nonexistent csv_path is refused", raised)
        _assert("nonexistent-file refusal never reaches the executor", not executor.run_json.called)


def test_submit_csv_path_containment_admits() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_bulk_smoke_") as workspace:
        csv_path = str(Path(workspace) / "records.csv")
        Path(csv_path).write_text("Id,Name\n001x,Acme\n", encoding="utf-8")
        gate = _real_import_gate([workspace])
        executor = _fake_executor()
        executor.run_json.return_value = _job_info()
        result = bulk_actions.bulk_ingest_submit(
            executor, {"operation": "insert", "sobject": "Account", "csv_path": csv_path}, gate,
        )
        _assert("admitted csv_path reaches the executor", executor.run_json.called)
        _assert("submit returns the created job_id", result["job_id"] == "750xx000001AAAA", str(result))
        _assert("submit returns the initial state", result["state"] == "Open", str(result))


def test_bulk_job_status_state_passthrough() -> None:
    executor = _fake_executor()
    executor.run_rest.return_value = _job_info(
        state="JobComplete", numberRecordsProcessed=100, numberRecordsFailed=3,
    )
    result = bulk_actions.bulk_job_status(executor, {"job_id": "750xx000001AAAA"})
    _assert("status carries job_id", result["job_id"] == "750xx000001AAAA")
    _assert("status carries state", result["state"] == "JobComplete")
    _assert("status carries processed_records", result["processed_records"] == 100)
    _assert("status carries failed_records", result["failed_records"] == 3)
    method, path = executor.run_rest.call_args.args
    _assert("status invoked via GET on /jobs/ingest/{id}", method == "GET" and path.endswith("/jobs/ingest/750xx000001AAAA"), path)


def test_bulk_job_status_failed_state_carries_error_message() -> None:
    executor = _fake_executor()
    executor.run_rest.return_value = _job_info(state="Failed", errorMessage="InvalidBatch: malformed CSV")
    result = bulk_actions.bulk_job_status(executor, {"job_id": "750xx000001AAAA"})
    _assert("Failed state passes through honestly", result["state"] == "Failed")
    _assert("error_message carried through on Failed", result["error_message"] == "InvalidBatch: malformed CSV", str(result))


_JOB_STATE_SUMMARY_KEYS = {"job_id", "state", "processed_records", "failed_records", "error_message"}


def test_bulk_job_status_non_terminal_branch_emits_every_schema_key() -> None:
    """Schema-parity regression guard (external audit relay, 2026-08-24): a prior Class-A
    defect pattern omitted schema-declared keys on a non-terminal poll response. A freshly
    Open/InProgress Salesforce job's raw REST body genuinely OMITS numberRecordsProcessed/
    numberRecordsFailed/errorMessage as keys (processing hasn't started, no error exists yet)
    -- distinct from a terminal response that includes them as explicit values. Every branch
    of _job_state_summary must still emit the full 5-key shape, defaulted, not partial."""
    for state in ("Open", "UploadComplete", "InProgress"):
        executor = _fake_executor()
        # Deliberately minimal -- only the two fields Salesforce guarantees on every response.
        executor.run_rest.return_value = {"id": "750xx000001AAAA", "state": state}
        result = bulk_actions.bulk_job_status(executor, {"job_id": "750xx000001AAAA"})
        _assert(
            f"{state}: every schema-declared key present despite a sparse raw response",
            set(result.keys()) == _JOB_STATE_SUMMARY_KEYS,
            str(result),
        )
        _assert(f"{state}: processed_records defaults to 0, not missing/None", result["processed_records"] == 0, str(result))
        _assert(f"{state}: failed_records defaults to 0, not missing/None", result["failed_records"] == 0, str(result))
        _assert(f"{state}: error_message defaults to '', not missing/None", result["error_message"] == "", str(result))

    # The same helper backs submit's initial response and abort's PATCH response --
    # prove the branch-completeness property holds for those call sites too, not just status.
    executor = _fake_executor()
    with tempfile.TemporaryDirectory(prefix="sf_bulk_smoke_") as workspace:
        csv_path = str(Path(workspace) / "records.csv")
        Path(csv_path).write_text("Id\n001x\n", encoding="utf-8")
        executor.run_json.return_value = {"id": "750xx000001AAAA", "state": "Open"}  # no counts/error yet
        submit_result = bulk_actions.bulk_ingest_submit(
            executor, {"operation": "insert", "sobject": "Account", "csv_path": csv_path}, _real_import_gate([workspace]),
        )
        _assert(
            "bulk_ingest_submit's freshly-created-job branch emits every schema-declared key",
            set(submit_result.keys()) == _JOB_STATE_SUMMARY_KEYS,
            str(submit_result),
        )

    executor = _fake_executor()
    executor.run_rest.return_value = {"id": "750xx000001AAAA", "state": "Aborted"}  # no counts/error carried
    abort_result = bulk_actions.bulk_job_abort(executor, {"job_id": "750xx000001AAAA"})
    _assert(
        "bulk_job_abort's minimal-response branch emits every schema-declared key",
        set(abort_result.keys()) == _JOB_STATE_SUMMARY_KEYS,
        str(abort_result),
    )


def test_bulk_job_abort() -> None:
    executor = _fake_executor()
    executor.run_rest.return_value = _job_info(state="Aborted")
    result = bulk_actions.bulk_job_abort(executor, {"job_id": "750xx000001AAAA"})
    _assert("abort returns the resulting state", result["state"] == "Aborted", str(result))
    method, path = executor.run_rest.call_args.args
    _assert("abort invoked via PATCH on /jobs/ingest/{id}", method == "PATCH" and path.endswith("/jobs/ingest/750xx000001AAAA"), path)
    _assert("abort body sets state=Aborted", executor.run_rest.call_args.kwargs.get("body") == {"state": "Aborted"})


def test_bulk_job_results_writes_three_csvs_under_containment() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_bulk_smoke_") as workspace:
        gate = _real_export_gate([workspace])
        executor = _fake_executor()
        executor.run_rest.side_effect = [
            "sf__Id,sf__Created,Id,Name\n001a,true,001a,Acme\n",
            "sf__Id,sf__Error,Id\n,ROW_MISMATCH,002b\n",
            "Id\n003c\n",
        ]
        successful_path = str(Path(workspace) / "successful.csv")
        failed_path = str(Path(workspace) / "failed.csv")
        unprocessed_path = str(Path(workspace) / "unprocessed.csv")
        result = bulk_actions.bulk_job_results(
            executor,
            {
                "job_id": "750xx000001AAAA",
                "successful_results_path": successful_path,
                "failed_results_path": failed_path,
                "unprocessed_results_path": unprocessed_path,
            },
            gate,
        )
        _assert("result carries the resolved successful path", result["successful_results_path"] == str(Path(successful_path).resolve()))
        _assert("successful CSV written", Path(successful_path).read_text(encoding="utf-8").startswith("sf__Id,sf__Created"))
        _assert("failed CSV written", "ROW_MISMATCH" in Path(failed_path).read_text(encoding="utf-8"))
        _assert("unprocessed CSV written", Path(unprocessed_path).read_text(encoding="utf-8").strip() == "Id\n003c".strip())
        paths_called = [call.args[1] for call in executor.run_rest.call_args_list]
        expected_suffixes = ["/successfulResults", "/failedResults", "/unprocessedrecords"]
        _assert(
            "three distinct REST endpoints hit, in order",
            [path.endswith(suffix) for path, suffix in zip(paths_called, expected_suffixes, strict=True)] == [True, True, True],
            str(paths_called),
        )
        _assert("every call used raw_response=True", all(c.kwargs.get("raw_response") is True for c in executor.run_rest.call_args_list))


def test_bulk_job_results_containment_refusal() -> None:
    with tempfile.TemporaryDirectory(prefix="sf_bulk_smoke_") as workspace:
        allowed_dir = Path(workspace) / "allowed"
        allowed_dir.mkdir()
        gate = _real_export_gate([str(allowed_dir)])
        executor = _fake_executor()
        outside_path = str(Path(workspace) / "outside.csv")
        raised = False
        try:
            bulk_actions.bulk_job_results(
                executor,
                {
                    "job_id": "750xx000001AAAA",
                    "successful_results_path": outside_path,
                    "failed_results_path": str(allowed_dir / "failed.csv"),
                    "unprocessed_results_path": str(allowed_dir / "unprocessed.csv"),
                },
                gate,
            )
        except ExportPathRefusedError:
            raised = True
        _assert("a results path outside every allowed root is refused", raised)
        _assert("refusal happens before ANY REST call is made", not executor.run_rest.called)
        _assert("no file written on refusal", not Path(outside_path).exists())


def test_classify_topology_leak_on_malformed_bulk_response() -> None:
    executor = _fake_executor()
    executor.run_rest.return_value = {"unexpected": "shape"}
    raised = None
    try:
        bulk_actions.bulk_job_status(executor, {"job_id": "750xx000001AAAA"})
    except SalesforceCliCallError as exc:
        raised = exc
    _assert("an unrecognized job response shape raises SalesforceCliCallError", raised is not None)
    code, message = classify_salesforce_error(raised) if raised else ("", "")
    _assert("classifies to the generic catch-all", code == "sf.api_error", code)
    _assert("message never echoes the raw response dict", "unexpected" not in message, message)


def test_edge_parity_bulk_verbs_present() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from salesforce_plugin.plugin import SalesforcePlugin

    plugin = SalesforcePlugin()
    actions = discover_actions(plugin)
    names = {action.name for action in actions}
    expected_bulk = {"bulk_ingest_submit", "bulk_job_status", "bulk_job_results", "bulk_job_abort"}
    _assert("all four bulk verbs discovered", expected_bulk.issubset(names), str(sorted(names)))
    _assert("thirteen verbs total (9 pre-existing + 4 bulk)", len(actions) == 13, str(len(actions)))


def main() -> int:
    print("\nsalesforce_plugin Bulk API v2 ingest smoke tests")
    print("=" * 51)
    test_submit_argv_shape_per_operation()
    test_upsert_external_id_field_pairing()
    test_hard_delete_and_unknown_operation_excluded()
    test_submit_csv_path_containment_refusals()
    test_submit_csv_path_containment_admits()
    test_bulk_job_status_state_passthrough()
    test_bulk_job_status_failed_state_carries_error_message()
    test_bulk_job_status_non_terminal_branch_emits_every_schema_key()
    test_bulk_job_abort()
    test_bulk_job_results_writes_three_csvs_under_containment()
    test_bulk_job_results_containment_refusal()
    test_classify_topology_leak_on_malformed_bulk_response()
    test_edge_parity_bulk_verbs_present()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Bulk API v2 ingest smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
