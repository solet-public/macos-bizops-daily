"""Bulk API v2 ingest verb implementations — pure functions over a ``SalesforceCliExecutor``.

Wave 1 of lane-sf-bulk-v2's 2026-08-24 Bulk API v2 capability plan:
submit/status/results/abort for Bulk v2 INGEST only — bulk query is wave 2,
out of this landing. Same shape as `record_actions.py`/`soql_actions.py`:
each function takes the executor and a ``params`` dict, returning a plain
result dict; invalid parameters raise ``ValueError``.

Two CLI surfaces, split the same way the plan measured them:

- **`bulk_ingest_submit`** uses the stable `sf data <op> bulk --async --json`
  envelope command via ``run_json`` — no field-value mini-language involved
  (the CSV carries the data), so the stable surface is safe here the same
  way `get_record`/`delete_record` use it.
- **`bulk_job_status`/`bulk_job_results`/`bulk_job_abort`** use
  `run_rest` against `/jobs/ingest/{id}` — the stable CLI has no bulk-job
  status/results/abort surface at all, only submission.

**Never `--wait`** on the submit call (constraint #2 of the plan): a Bulk v2
job can run minutes past the 30s ``SF_CLI_TIMEOUT_SECONDS`` subprocess bound.
`bulk_ingest_submit` returns the instant the job is CREATED (state ``Open``
or similar); the caller polls `bulk_job_status` in its own loop.

`bulk_job_results`' CSV bodies are fetched via `run_rest`'s raw-text mode
(`client.py`) rather than the CLI's own `sf data bulk results --job-id`
alternative — **the measured, recorded decision** (2026-08-24 plan WP3): the
CLI command writes into its own resolved cwd, which is not this plugin's
containment boundary; REST-raw + our own contained file write keeps every
byte this verb writes inside the same realpath+commonpath gate
(`export_containment.py`) that already governs `export_soql`, with no
CLI-cwd side channel to reason about.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .client import SalesforceCliExecutor
from .constants import BULK_INGEST_HARD_DELETE_OPERATION, BULK_INGEST_OPERATIONS, BULK_JOB_ABORT_STATE
from .errors import SalesforceCliCallError

# path_gate(path) -> realpath-resolved path, or raises a *PathRefusedError.
# Injected by the plugin: the import gate (import_allowed_roots) for
# bulk_ingest_submit's csv_path, the CSV-suffixed export gate
# (export_allowed_roots) for bulk_job_results' three output paths.
PathGate = Callable[[str], str]


def bulk_ingest_submit(
    executor: SalesforceCliExecutor, params: dict[str, Any], path_gate: PathGate,
) -> dict[str, Any]:
    """Submit a Bulk v2 ingest job (insert/update/upsert/delete) from a CSV file.

    `csv_path` must be contained under an operator-configured
    `import_allowed_roots` entry (`path_gate`, refuse-all default).
    `external_id_field` is required iff `operation == "upsert"` and rejected
    otherwise — fail loud on a mismatched pair rather than silently ignoring
    an extra param (mirrors `soql_actions._resolve_effective_limit`'s
    together-or-neither posture for its own override pair).
    """
    operation = _require_str(params, "operation")
    if operation == BULK_INGEST_HARD_DELETE_OPERATION:
        raise ValueError(
            f"operation {BULK_INGEST_HARD_DELETE_OPERATION!r} is excluded from bulk_ingest_submit "
            "(bypasses the recycle bin, needs an org permission, not covered by the standing "
            "delete ratification) — use 'delete', or ask the operator for a hardDelete request"
        )
    if operation not in BULK_INGEST_OPERATIONS:
        raise ValueError(
            f"'operation' must be one of {sorted(BULK_INGEST_OPERATIONS)} (got {operation!r})"
        )
    sobject = _require_str(params, "sobject")
    csv_path = _require_str(params, "csv_path")
    resolved_path = path_gate(csv_path)
    external_id_field = params.get("external_id_field")
    argv = ["data", operation, "bulk", "--sobject", sobject, "--file", resolved_path, "--async"]
    if operation == "upsert":
        if not isinstance(external_id_field, str) or not external_id_field:
            raise ValueError(
                "'external_id_field' is required and must be a non-empty string when "
                "operation is 'upsert'"
            )
        argv += ["--external-id", external_id_field]
    elif external_id_field is not None:
        raise ValueError("'external_id_field' is only valid when operation is 'upsert'")
    result = executor.run_json(argv)
    return _job_state_summary(result)


def bulk_job_status(executor: SalesforceCliExecutor, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch a Bulk v2 ingest job's current state + record counts (the poll target)."""
    job_id = _require_str(params, "job_id")
    result = executor.run_rest("GET", f"services/data/v{executor.api_version}/jobs/ingest/{job_id}")
    return _job_state_summary(result)


def bulk_job_abort(executor: SalesforceCliExecutor, params: dict[str, Any]) -> dict[str, Any]:
    """Abort a running Bulk v2 ingest job — the recovery lever."""
    job_id = _require_str(params, "job_id")
    result = executor.run_rest(
        "PATCH",
        f"services/data/v{executor.api_version}/jobs/ingest/{job_id}",
        body={"state": BULK_JOB_ABORT_STATE},
    )
    return _job_state_summary(result)


def bulk_job_results(
    executor: SalesforceCliExecutor, params: dict[str, Any], path_gate: PathGate,
) -> dict[str, Any]:
    """Fetch a completed Bulk v2 ingest job's three result CSVs, write each under containment.

    Each of the three caller-supplied paths is admitted independently via
    `path_gate` (the CSV-suffixed `export_allowed_roots` gate) before any
    network call is made — a refused path never dispatches its REST call.
    """
    job_id = _require_str(params, "job_id")
    successful_path = path_gate(_require_str(params, "successful_results_path"))
    failed_path = path_gate(_require_str(params, "failed_results_path"))
    unprocessed_path = path_gate(_require_str(params, "unprocessed_results_path"))
    base = f"services/data/v{executor.api_version}/jobs/ingest/{job_id}"
    _fetch_and_write_csv(executor, f"{base}/successfulResults", successful_path)
    _fetch_and_write_csv(executor, f"{base}/failedResults", failed_path)
    _fetch_and_write_csv(executor, f"{base}/unprocessedrecords", unprocessed_path)
    return {
        "successful_results_path": successful_path,
        "failed_results_path": failed_path,
        "unprocessed_results_path": unprocessed_path,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _fetch_and_write_csv(executor: SalesforceCliExecutor, path: str, resolved_path: str) -> None:
    text = executor.run_rest("GET", path, raw_response=True)
    content = text if isinstance(text, str) else ""
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(
            f"the parent directory of the results path does not exist ({parent_dir}); "
            "create it first — this verb writes files, it does not create directories"
        )
    with open(resolved_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _job_state_summary(result: Any) -> dict[str, Any]:
    """Extract the shared Bulk v2 Job Info shape (create/status/abort all return it)."""
    if not isinstance(result, dict):
        raise SalesforceCliCallError("", "sf CLI returned an unexpected bulk job response shape")
    job_id = result.get("id")
    state = result.get("state")
    if not isinstance(job_id, str) or not job_id or not isinstance(state, str) or not state:
        raise SalesforceCliCallError("", "sf CLI bulk job response is missing id/state")
    error_message = result.get("errorMessage")
    return {
        "job_id": job_id,
        "state": state,
        "processed_records": _as_int(result.get("numberRecordsProcessed")),
        "failed_records": _as_int(result.get("numberRecordsFailed")),
        "error_message": error_message if isinstance(error_message, str) else "",
    }


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value
