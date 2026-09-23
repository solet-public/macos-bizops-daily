"""Zuora verb implementations — pure functions over a built ``ZuoraClient``.

Each function takes an already-built :class:`http_client.ZuoraClient` and a
``params`` dict, returning a plain result dict. Invalid parameters raise
``ValueError`` (mapped to ``zuora.invalid_params``); non-2xx responses raise
``ZuoraResponseError`` (carrying the response for the plugin's classifier).

No delete verb exists (v1 scope, matching the 2026-06-20 design + the
umbrella wave's read/write posture register): billing records are
voided/cancelled through Zuora's own workflow, not deleted through this tool.

Business-data limits + data-export migration (2026-08-02 —
workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md).
``data_query``, ``bulk_export``, ``list_subscriptions``, and ``list_invoices``
now ALWAYS write to the caller-supplied ``output_tsv_path`` and return a
handle only — never records inline, at any size (the former blob-export /
``INLINE_BYTE_CAP`` branches are deleted, not lowered). Effective record
ceiling is ``DEFAULT_ROW_LIMIT`` unless the caller supplies BOTH
``acknowledge_default_limit_override=true`` and an explicit ``row_limit``
(see ``_resolve_effective_limit``). ``get_object``/``get_invoice`` are
single-record fetch-by-id verbs (§1.2 exemption) and are unaffected.

``data_query``/``bulk_export`` real continuation (fixing the previously
vacuous ``BULK_EXPORT_ROW_CAP``): both now call the documented, current
Zuora Actions endpoints ``POST /v1/action/query`` and
``POST /v1/action/queryMore`` (operationIds ``Action_POSTquery`` /
``Action_POSTqueryMore``) rather than the undocumented legacy ``/v1/query``
alias this module used previously — the two are the same underlying ZOQL
query mechanism (confirmed: ``/v1/query`` is absent from Zuora's current
OpenAPI bundle exactly like ``/v1/action/query``'s sibling paths, yet is
independently verified live and 2,000-record-capped, the same figure
``/v1/action/query``'s own documented "Limitations" state), but only the
``/v1/action/*`` pair has a citable, current contract for the queryMore
continuation this rebuild depends on. Zuora returns at most
``ZUORA_QUERY_PAGE_ROW_CAP`` (2,000) records per call; when the response's
``done`` is ``false`` it carries a ``queryLocator`` marker this module passes
to ``queryMore`` to fetch the next page, looping until ``done`` is ``true``
or the effective row limit is reached — never truncating a list that could
never exceed ~2,000 elements, the pre-existing defect this rebuild fixes.

``list_subscriptions`` real continuation: ``GET /v1/subscriptions/accounts/
{account-key}`` is a documented, page/pageSize-paginated endpoint
(operationId ``GET_SubscriptionsByAccount``; the ``pageSize`` parameter's own
component caps at ``ZUORA_LIST_PAGE_SIZE_MAX`` (40), defaulting to 20 when
omitted). The previous implementation passed neither parameter — silently
returning at most 20 subscriptions with no signal that more existed. This
module now pages internally up to the effective row limit.

``list_invoices`` — the vendor endpoint this module calls,
``GET /v1/invoices/accounts/{account-key}``, is (like ``/v1/query``) absent
from Zuora's current OpenAPI bundle; unlike ``/v1/query`` there is no
independent live-behavior verification and no documented current pagination
contract for it (Zuora's documented CURRENT account-scoped billing-document
listing is a materially different endpoint, ``GET /v1/billing-documents``,
which mixes invoices with credit/debit memos and offers no ``documentType``
query filter — migrating to it is a verb-contract change outside this
wave's approved scope, flagged separately for a ruling rather than built
silently). This module therefore still issues a single call and applies
``row_limit`` as a post-fetch cap on what is WRITTEN, not a pre-fetch
vendor-side ceiling — disclosed in the process description rather than
presented as a confirmed pagination guarantee.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Callable
from typing import Any, Final

import httpx

from .constants import (
    BULK_EXPORT_ROW_CAP,
    DATA_QUERY_MAX_ROWS_CAP,
    DEFAULT_ROW_LIMIT,
    LIST_ROW_LIMIT_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    SUPPORTED_OBJECT_TYPES,
    ZUORA_LIST_PAGE_SIZE_MAX,
)

# path_gate(output_tsv_path) -> realpath-resolved path to write, or raises
# ExportPathRefusedError. Injected by the plugin, which binds the operator's
# export_allowed_roots config (export_containment.assert_export_path_allowed).
PathGate = Callable[[str], str]

# Defense-in-depth circuit breaker on the queryMore loop — independent of
# any configured cap, this bounds worst-case call count even if a future cap
# is raised well past today's 50,000 (25 calls at the vendor's 2,000/call
# ceiling); never expected to bind in practice.
_MAX_QUERY_MORE_CALLS: Final[int] = 64


class ZuoraResponseError(Exception):
    """Carries a non-2xx ``httpx.Response`` for the plugin's classifier to map."""

    def __init__(self, response: httpx.Response, *, is_query: bool) -> None:
        super().__init__(f"Zuora request failed with status {response.status_code}")
        self.response = response
        self.is_query = is_query


def data_query(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Run a ZOQL query; write the result to output_tsv_path, return a handle.

    Defaults to DEFAULT_ROW_LIMIT records; an acknowledged override may
    request up to DATA_QUERY_MAX_ROWS_CAP. For pulls beyond that ceiling, use
    bulk_export (same override mechanism, higher hard cap).
    """
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=DATA_QUERY_MAX_ROWS_CAP, verb="data_query",
    )
    zoql = _require_str(params, "zoql")
    output_tsv_path = _require_str(params, "output_tsv_path")
    return _write_zoql_result_tsv(client, zoql, effective_limit, path_gate, output_tsv_path)


def bulk_export(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Export a ZOQL query's result as a workspace TSV — the N>>500 route.

    Same caller-supplied-path destination and override mechanism as
    data_query; only the override's hard cap differs (BULK_EXPORT_ROW_CAP).
    Reaches past the vendor's 2,000-record-per-call ceiling via the
    queryMore continuation loop (_run_zoql_query) — the previously vacuous
    50,000 cap is now genuinely reachable.
    """
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=BULK_EXPORT_ROW_CAP, verb="bulk_export",
    )
    zoql = _require_str(params, "zoql")
    output_tsv_path = _require_str(params, "output_tsv_path")
    return _write_zoql_result_tsv(client, zoql, effective_limit, path_gate, output_tsv_path)


def get_object(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one object by type + id via the Object API."""
    object_type = _require_object_type(params)
    object_id = _require_str(params, "id")
    response = client.get(f"/v1/object/{object_type}/{object_id}")
    _raise_for_status(response, is_query=False)
    return {"object": response.json()}


def create_object(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create an object of the given type with the given fields."""
    object_type = _require_object_type(params)
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("'fields' is required and must be a non-empty object")
    response = client.post(f"/v1/object/{object_type}", json=fields)
    _raise_for_status(response, is_query=False)
    payload = response.json()
    return {
        "id": _as_str(payload.get("Id")),
        "success": bool(payload.get("Success", True)),
    }


def update_object(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Update an existing object by type + id with a non-empty fields object."""
    object_type = _require_object_type(params)
    object_id = _require_str(params, "id")
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("'fields' is required and must be a non-empty object")
    response = client.put(f"/v1/object/{object_type}/{object_id}", json=fields)
    _raise_for_status(response, is_query=False)
    payload = response.json()
    return {"success": bool(payload.get("Success", True))}


def list_subscriptions(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """List an account's subscriptions; write the result to output_tsv_path, return a handle.

    Pages internally (page/pageSize, ZUORA_LIST_PAGE_SIZE_MAX per call) up to
    the effective row limit — the previous implementation passed no
    pagination parameters and silently returned at most 20 (Zuora's own
    pageSize default).
    """
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=LIST_ROW_LIMIT_CAP, verb="list_subscriptions",
    )
    account_id = _require_str(params, "account_id")
    output_tsv_path = _require_str(params, "output_tsv_path")
    resolved_path = _gate_and_check_parent(path_gate, output_tsv_path)
    subscriptions, truncated = _paginate_account_list(
        client, f"/v1/subscriptions/accounts/{account_id}", "subscriptions", effective_limit,
    )
    return _write_records_tsv(subscriptions, resolved_path, truncated=truncated)


def get_invoice(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one invoice by id."""
    invoice_id = _require_str(params, "id")
    response = client.get(f"/v1/invoices/{invoice_id}")
    _raise_for_status(response, is_query=False)
    return {"invoice": response.json()}


def list_invoices(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """List an account's invoices; write the result to output_tsv_path, return a handle.

    See this module's docstring: the vendor endpoint's own pagination support
    is unconfirmed (absent from Zuora's current OpenAPI bundle, unlike
    list_subscriptions' documented page/pageSize contract), so row_limit is
    applied as a post-fetch cap on Zuora's single-call response rather than a
    pre-fetch vendor-side ceiling.
    """
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=LIST_ROW_LIMIT_CAP, verb="list_invoices",
    )
    account_id = _require_str(params, "account_id")
    output_tsv_path = _require_str(params, "output_tsv_path")
    resolved_path = _gate_and_check_parent(path_gate, output_tsv_path)
    response = client.get(f"/v1/invoices/accounts/{account_id}")
    _raise_for_status(response, is_query=False)
    payload = response.json()
    invoices = payload.get("invoices")
    invoices = invoices if isinstance(invoices, list) else []
    truncated = len(invoices) >= effective_limit
    invoices = invoices[:effective_limit]
    return _write_records_tsv(invoices, resolved_path, truncated=truncated)


# ---------------------------------------------------------------------------
# ZOQL query + queryMore continuation (data_query / bulk_export)
# ---------------------------------------------------------------------------


def _write_zoql_result_tsv(
    client: Any, zoql: str, cap: int, path_gate: PathGate, output_tsv_path: str,
) -> dict[str, Any]:
    resolved_path = _gate_and_check_parent(path_gate, output_tsv_path)
    records, total_size, truncated = _run_zoql_query(client, zoql, cap)
    result = _write_records_tsv(records, resolved_path, truncated=truncated)
    result["total_size"] = total_size
    return result


def _run_zoql_query(client: Any, zoql: str, cap: int) -> tuple[list[dict[str, Any]], int, bool]:
    """Run a ZOQL query, following queryMore until done, cap, or the call-count breaker.

    Returns (records limited to cap, the vendor's own total-match count
    from the final page, truncated).
    """
    records, total_size, done, query_locator = _query_page(
        client, "/v1/action/query", {"queryString": zoql},
    )
    calls = 0
    while not done and len(records) < cap and query_locator is not None and calls < _MAX_QUERY_MORE_CALLS:
        page_records, total_size, done, query_locator = _query_page(
            client, "/v1/action/queryMore", {"queryLocator": query_locator},
        )
        if not page_records and not done:
            # Vendor stalled without signaling completion — stop rather than
            # spin; the truncated flag below still reflects the shortfall.
            break
        records = records + page_records
        calls += 1
    truncated = (not done) or len(records) > cap or total_size > len(records)
    return records[:cap], total_size, truncated


def _query_page(
    client: Any, path: str, body: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, bool, str | None]:
    response = client.post(path, json=body)
    _raise_for_status(response, is_query=True)
    payload = response.json()
    records = _as_record_list(payload.get("records"))
    total_size = _as_int(payload.get("size"), default=len(records))
    done = bool(payload.get("done", True))
    locator = payload.get("queryLocator")
    return records, total_size, done, (locator if isinstance(locator, str) and locator else None)


# ---------------------------------------------------------------------------
# Account-scoped list pagination (list_subscriptions)
# ---------------------------------------------------------------------------


def _paginate_account_list(
    client: Any, path: str, list_key: str, cap: int,
) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    page = 1
    while len(records) < cap:
        response = client.get(path, params={"page": page, "pageSize": ZUORA_LIST_PAGE_SIZE_MAX})
        _raise_for_status(response, is_query=False)
        payload = response.json()
        page_items = _as_record_list(payload.get(list_key))
        records.extend(page_items)
        if len(page_items) < ZUORA_LIST_PAGE_SIZE_MAX:
            # A short page is the vendor's own end-of-data signal.
            return records[:cap], len(records) > cap
        page += 1
    # The cap landed exactly on a full page boundary — more MAY exist; we
    # cannot tell without one further call, so report truncated rather than
    # silently implying completeness.
    return records[:cap], True


# ---------------------------------------------------------------------------
# Shared TSV writing + override-friction resolution
# ---------------------------------------------------------------------------


def _gate_and_check_parent(path_gate: PathGate, output_tsv_path: str) -> str:
    resolved_path = path_gate(output_tsv_path)
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(
            f"the parent directory of output_tsv_path does not exist ({parent_dir}); "
            "create it first — this verb writes one file, it does not create directories"
        )
    return resolved_path


def _write_records_tsv(
    records: list[dict[str, Any]], resolved_path: str, *, truncated: bool,
) -> dict[str, Any]:
    columns = _ordered_columns(records)
    row_lists = [[_cell_value(record.get(column)) for column in columns] for record in records]
    with open(resolved_path, "wb") as handle:
        handle.write(_to_tsv(columns, row_lists))
    return {
        "path": resolved_path,
        "columns": columns,
        "row_count": len(row_lists),
        "truncated": truncated,
    }


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


def _ordered_columns(records: list[dict[str, Any]]) -> list[str]:
    """Union of record keys in first-appearance order."""
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


def _as_record_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _raise_for_status(response: httpx.Response, *, is_query: bool) -> None:
    if response.status_code >= 300:
        raise ZuoraResponseError(response, is_query=is_query)


def _require_object_type(params: dict[str, Any]) -> str:
    object_type = _require_str(params, "type")
    if object_type not in SUPPORTED_OBJECT_TYPES:
        raise ValueError(
            f"'type' must be one of {sorted(SUPPORTED_OBJECT_TYPES)}, got {object_type!r}"
        )
    return object_type


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)
