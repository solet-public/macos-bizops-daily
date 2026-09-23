"""SOQL query verb — a pure function over a ``SalesforceCliExecutor``.

This module is S2-exempt (whole-file): SOQL strings like
``SELECT Id, Name FROM Account`` match the gate's S2 verb+FROM heuristic even
though they target the FOREIGN Salesforce org (containment invariant #1 — no
org/domain parameter on any verb; the client is built solely from the
"salesforce_org" address-book entry). See
knowledge_base/01_salesforce_overview.md §3 for the full gate-treatment note.

The query is written to a tempfile and run via `sf data query --file`
(file-based — the operator's proven work-script pattern, and it keeps SOQL
text, which can carry business-sensitive filter values, out of the process
argv `ps` would otherwise expose).

No manual pagination: `sf data query` runs on jsforce with `autoFetch: true`
internally (verified by reading the CLI's own source,
``@salesforce/plugin-data/lib/commands/data/query.js``) — it already collects
every page up to a fetch cap before returning. This module passes
``SF_ORG_MAX_QUERY_LIMIT`` as a subprocess env override so the CLI stops
fetching at the effective limit server-side (never the unbounded default),
then slices the returned list to that limit again client-side as
defense-in-depth.

Both ``soql_query`` and ``export_soql`` ALWAYS write their result to the
caller-supplied ``output_tsv_path`` and return a handle only — never records
inline, at any size (business-data limits + data-export migration,
2026-08-02; the former inline-return/``INLINE_BYTE_CAP`` branch is deleted,
not lowered). The effective record ceiling is DEFAULT_ROW_LIMIT unless the
caller supplies BOTH ``acknowledge_default_limit_override=true`` and an
explicit ``row_limit`` (up to the verb's hard cap) — see
``_resolve_effective_limit``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Callable
from typing import Any

from .client import SalesforceCliExecutor
from .constants import (
    DEFAULT_ROW_LIMIT,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    SOQL_EXPORT_ROW_CAP,
    SOQL_MAX_RECORDS_CAP,
)

# path_gate(output_tsv_path) -> realpath-resolved path to write, or raises
# ExportPathRefusedError. Injected by the plugin, which binds the operator's
# export_allowed_roots config (export_containment.assert_export_path_allowed).
PathGate = Callable[[str], str]


def fetch_org_id(executor: SalesforceCliExecutor) -> str:
    """Fetch the org's own Id via a minimal query — used by test_connection.

    Kept here (not in plugin.py) so this module stays the ONE place composing
    SOQL-shaped literal strings — the S2 gate exemption boundary (see
    knowledge_base/01_salesforce_overview.md §3).
    """
    result = _run_soql(executor, "SELECT Id FROM Organization LIMIT 1", max_records=1)
    records = result.get("records") if isinstance(result, dict) else None
    if isinstance(records, list) and records and isinstance(records[0], dict):
        return str(records[0].get("Id") or "")
    return ""


def soql_query(
    executor: SalesforceCliExecutor, params: dict[str, Any], path_gate: PathGate,
) -> dict[str, Any]:
    """Run a SOQL query; write the result to output_tsv_path, return a handle.

    Defaults to DEFAULT_ROW_LIMIT records; an acknowledged override may
    request up to SOQL_MAX_RECORDS_CAP. For pulls beyond that ceiling, use
    export_soql (up to SOQL_EXPORT_ROW_CAP with the same override).
    """
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=SOQL_MAX_RECORDS_CAP, verb="soql_query",
    )
    query = _require_str(params, "query")
    output_tsv_path = _require_str(params, "output_tsv_path")
    return _run_and_write_tsv(executor, query, effective_limit, path_gate, output_tsv_path)


def export_soql(
    executor: SalesforceCliExecutor, params: dict[str, Any], path_gate: PathGate
) -> dict[str, Any]:
    """Export a SOQL query's result as a workspace TSV — the N>>500 route.

    Same caller-supplied-path destination and override mechanism as
    soql_query; only the override's hard cap differs (SOQL_EXPORT_ROW_CAP,
    not SOQL_MAX_RECORDS_CAP). Absent the override this also defaults to
    DEFAULT_ROW_LIMIT — for the common small/default case, soql_query has an
    identical interface with a lower ceiling. The gate runs BEFORE the query
    is sent.
    """
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=SOQL_EXPORT_ROW_CAP, verb="export_soql",
    )
    query = _require_str(params, "query")
    output_tsv_path = _require_str(params, "output_tsv_path")
    return _run_and_write_tsv(executor, query, effective_limit, path_gate, output_tsv_path)


def _resolve_effective_limit(
    params: dict[str, Any], *, default: int, hard_cap: int, verb: str,
) -> int:
    """Resolve the effective fetch ceiling from the §5 override pair.

    Absent (or ``acknowledge_default_limit_override`` not exactly ``True``)
    with no ``row_limit``: returns ``default``. Both must be given together —
    the override flag alone, or ``row_limit`` alone, fails loud rather than
    silently honoring half. A ``row_limit`` above ``hard_cap`` is refused,
    never silently clamped back down.
    """
    override = params.get(PARAM_ACKNOWLEDGE_OVERRIDE)
    row_limit = params.get(PARAM_ROW_LIMIT)
    override_present = override is True
    row_limit_present = row_limit is not None
    if override_present != row_limit_present:
        raise ValueError(
            f"{verb}: '{PARAM_ACKNOWLEDGE_OVERRIDE}' and '{PARAM_ROW_LIMIT}' must be "
            f"given together — got {PARAM_ACKNOWLEDGE_OVERRIDE}={override!r}, "
            f"{PARAM_ROW_LIMIT}={row_limit!r}"
        )
    if not override_present:
        return default
    if not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 1:
        raise ValueError(f"{verb}: '{PARAM_ROW_LIMIT}' must be a positive integer")
    if row_limit > hard_cap:
        raise ValueError(
            f"{verb}: '{PARAM_ROW_LIMIT}'={row_limit} exceeds the hard cap of "
            f"{hard_cap}; refusing rather than silently clamping"
        )
    return row_limit


def _run_and_write_tsv(
    executor: SalesforceCliExecutor,
    query: str,
    cap: int,
    path_gate: PathGate,
    output_tsv_path: str,
) -> dict[str, Any]:
    """Admit the path, run the SOQL query up to ``cap`` records, write a TSV, return a handle.

    The containment gate runs BEFORE the query is sent, so a refused path
    never sends it.
    """
    resolved_path = path_gate(output_tsv_path)
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(
            f"the parent directory of output_tsv_path does not exist ({parent_dir}); "
            "create it first — this verb writes one file, it does not create directories"
        )
    result = _run_soql(executor, query, cap)
    records = _strip_attributes(result.get("records") or [] if isinstance(result, dict) else [])
    total_size = _as_int(result.get("totalSize") if isinstance(result, dict) else None, default=len(records))
    # Salesforce's own incompleteness signal: done=false means more data
    # remains server-side even when totalSize equals the fetched count
    # (Codex Wave-3 review, A3-1) — never report such a fetch as complete.
    done_false = isinstance(result, dict) and result.get("done") is False
    records = records[:cap]
    columns = _ordered_columns(records)
    row_lists = [[_cell_value(record.get(column)) for column in columns] for record in records]
    with open(resolved_path, "wb") as handle:
        handle.write(_to_tsv(columns, row_lists))
    return {
        "path": resolved_path,
        "columns": columns,
        "row_count": len(row_lists),
        "total_size": total_size,
        "truncated": (
            done_false
            or len(row_lists) >= cap
            or total_size > len(row_lists)
        ),
    }


def _run_soql(executor: SalesforceCliExecutor, query: str, max_records: int) -> Any:
    fd, path = tempfile.mkstemp(suffix=".soql", prefix="salesforce_plugin_query_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(query)
        return executor.run_json(
            ["data", "query", "--file", path],
            env_overrides={"SF_ORG_MAX_QUERY_LIMIT": str(max_records)},
        )
    finally:
        os.unlink(path)


def _ordered_columns(records: list[dict[str, Any]]) -> list[str]:
    """Union of record keys in first-appearance order (SOQL SELECT order)."""
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def _cell_value(value: Any) -> Any:
    """Coerce a record value to a TSV-safe form; nested objects become JSON text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _to_tsv(columns: list[str], row_lists: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(row_lists)
    return buffer.getvalue().encode("utf-8")


def _strip_attributes(records: list[Any]) -> list[dict[str, Any]]:
    """Drop Salesforce's injected ``attributes`` metadata key from each record."""
    out: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        out.append({k: v for k, v in record.items() if k != "attributes"})
    return out


def _as_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


