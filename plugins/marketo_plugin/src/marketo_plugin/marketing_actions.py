"""Marketo verb implementations — pure functions over a built ``MarketoClient``.

Each function takes an already-built :class:`http_client.MarketoClient` and a
``params`` dict, returning a plain result dict. Invalid parameters raise
``ValueError`` (mapped to ``marketo.invalid_params``); ``success: false``
envelopes raise ``MarketoEnvelopeError`` (carrying the payload for the
plugin's classifier).

Batch verbs (create_or_update_leads, delete_leads, add/remove_leads_from_list)
return Marketo's own per-record ``result`` array UNCHANGED plus a computed
tally — a per-record ``status: "skipped"``/``"failed"`` entry is normal data,
not a plugin-level fault, because Marketo's overall ``success`` stays true for
those calls. ``create_or_update_leads`` first refuses a whole batch that names
any intended REST read-only field other than identifiers or documented
default-read echoes, before calling the write endpoint. Only a ``success:
false`` envelope (structural fault — bad batch shape, auth, access) raises
after a write is attempted.

Business-data limits + data-export migration (2026-08-02 —
workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md,
§7.1). ``describe_lead_fields``, ``get_leads``, ``list_activity_types``,
``get_activities``, ``list_campaigns``, and ``list_static_lists`` now ALWAYS
write to the caller-supplied ``output_tsv_path`` and return a handle only —
never records inline, at any size (the former blob-export/``INLINE_BYTE_CAP``
branches are deleted, not lowered; blob storage retires from this plugin
entirely).

Internal pagination is hidden by design (2026-08-03 hide-paging change,
operator ruling: "we need to deliver the results - the paging is an
implementation detail that should be hidden", design doc §5.4/§7.2 as
amended, ruled doc-wide). This
SUPERSEDES the original Tier-2 build's Pattern B (caller-driven external
loop) for ``get_leads``, ``get_activities``, ``list_campaigns``, and
``list_static_lists`` — the four verbs whose vendor per-call ceiling
(``MARKETO_LIST_PAGE_ROW_CAP`` = 300) sits below the effective row limit.
Each now carries the standard §5 ``acknowledge_default_limit_override``/
``row_limit`` pair (default ``DEFAULT_ROW_LIMIT`` = 500, hard cap
``MARKETO_LIST_ROW_LIMIT_CAP`` = 5,000, same numeric precedent as zuora's
``LIST_ROW_LIMIT_CAP``) governing the CUMULATIVE fetch across as many
internal 300-record vendor calls as it takes — ``_paginate_token_authoritative``
and ``_paginate_activities`` do that looping, never the caller.
``next_page_token``/``more_result`` are GONE from every one of these four
verbs' surface, input and output alike (§5.4: "no pagination token, cursor,
or 'call again to continue' parameter appears on the verb's surface as the
default path") — a ``truncated`` boolean replaces them, honest about whether
more data may exist beyond what was written. The 300/call VENDOR ceiling
itself is unchanged and un-raisable (nothing here asks Marketo for more than
300 in one call) and stays disclosed in each process description per §5.1's
source-discipline rule; what changed is that the CALLER no longer loops to
reach the 500/5,000-row policy ceiling.

Beyond the row_limit hard cap (5,000): no resumption exists, by design
(§5.4) — a caller who needs more re-invokes the verb with a narrower
filter/date-range (e.g. ``get_leads`` with a tighter ``filter_values`` slice,
``get_activities`` with a later ``since_datetime``), never by carrying a
token forward. This is why a lead count well past the 5,000 hard cap no
longer fits in one ``get_leads`` call even at the cap (~17 internal vendor
calls gets to 5,000) — its route is now several separate ``get_leads`` calls
against non-overlapping filters, not a single call plus caller-side paging.

``describe_lead_fields`` and ``list_activity_types`` are UNCHANGED by this
build: both are single, unpaginated vendor calls with no continuation
signal of any kind — nothing to hide.

No bulk-extract verb exists (v1 scope): Marketo's async Bulk Extract job API
(create -> enqueue -> poll status -> download file) is a materially different
control-flow shape from every other verb here and is explicitly deferred.

D0.3 sync-verb migration (2026-08-09 —
workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md).
Batch 1: ``get_leads``, ``get_activities``, ``list_campaigns``, and
``list_static_lists`` each split into a ``prepare_<verb>`` (param validation +
path resolution, no vendor call — runs on the plugin's ms-scale dispatch
path) and an ``execute_<verb>`` (the actual paginated vendor fetch + TSV
write — runs in the plugin's background worker, off the dispatch path
entirely, per the platform's deferred-completion shape). Batch 2:
``describe_lead_fields``, ``list_activity_types``, and ``get_api_usage`` — the
single-vendor-call reads — split the same way, minus the pagination (their
``prepare_<verb>`` does only param validation / path resolution; their
``execute_<verb>`` is the one vendor call). Every migrated verb's original
combined ``<verb>`` function survives unchanged as a synchronous
``execute_<verb>(client, prepare_<verb>(...))`` convenience wrapper —
``check_setup``'s read probes and any other direct in-process caller still
call it exactly as before; only the plugin's own dispatch handlers for these
seven verbs call prepare/execute separately.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from collections.abc import Callable
from typing import Any, Final

from .constants import (
    ACTIVITIES_PATH,
    ACTIVITY_PAGING_TOKEN_PATH,
    ACTIVITY_TYPES_PATH,
    API_USAGE_PATH,
    CHECK_SETUP_PROBES,
    CHECK_SETUP_UNVERIFIED_WRITE_VERBS,
    DEFAULT_LEAD_ACTION,
    DEFAULT_LEAD_LOOKUP_FIELD,
    DEFAULT_ROW_LIMIT,
    ERROR_AUTH_FAILED,
    ERROR_PARTITION_ACCESS_DENIED,
    ERROR_PERMISSION_DENIED,
    GET_LEADS_DEFAULT_FIELDS,
    LEAD_ACTIONS,
    LEAD_ID_FIELD,
    LEADS_DESCRIBE_PATH,
    MARKETO_LIST_PAGE_ROW_CAP,
    MARKETO_LIST_ROW_LIMIT_CAP,
    MAX_ACTIVITY_LEAD_IDS,
    MAX_ACTIVITY_TYPE_IDS,
    MAX_BATCH_RECORDS,
    MAX_FILTER_VALUES,
    MAX_MERGE_LOSING_LEADS,
    MAX_MERGE_LOSING_LEADS_CRM,
    MAX_TRIGGER_LEADS,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
)
from .errors import MarketoEnvelopeError, classify_marketo_envelope

# path_gate(output_tsv_path) -> realpath-resolved path to write, or raises
# ExportPathRefusedError. Injected by the plugin, which binds the operator's
# export_allowed_roots config (export_containment.assert_export_path_allowed).
PathGate = Callable[[str], str]

_ISO_8601_FRACTIONAL_SECONDS = re.compile(r"(T\d{2}:\d{2}:\d{2})\.\d+")

# Defense-in-depth circuit breaker on the internal-pagination loops below —
# sized off the requested cap (cap // MARKETO_LIST_PAGE_ROW_CAP calls are
# needed to reach it on a full-page run) plus this margin for short pages;
# never expected to bind in practice, matching zuora_plugin's
# _MAX_QUERY_MORE_CALLS discipline.
_PAGE_CALL_BREAKER_MARGIN: Final[int] = 8


def _resolve_effective_limit(params: dict[str, Any], *, verb: str) -> int:
    """Resolve the §5 override pair into a cumulative fetch ceiling.

    Absent (or ``acknowledge_default_limit_override`` not exactly ``True``)
    with no ``row_limit``: returns ``DEFAULT_ROW_LIMIT`` (500). Both must be
    given together — the override flag alone, or ``row_limit`` alone, fails
    loud rather than silently honoring half. A ``row_limit`` above
    ``MARKETO_LIST_ROW_LIMIT_CAP`` is refused, never silently clamped down.
    Mirrors zuora_plugin.billing_actions._resolve_effective_limit exactly —
    same platform-wide §5 mechanism, applied here for the first time.
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
        return DEFAULT_ROW_LIMIT
    if not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 1:
        raise ValueError(f"{verb}: '{PARAM_ROW_LIMIT}' must be a positive integer")
    if row_limit > MARKETO_LIST_ROW_LIMIT_CAP:
        raise ValueError(
            f"{verb}: '{PARAM_ROW_LIMIT}'={row_limit} exceeds the hard cap of "
            f"{MARKETO_LIST_ROW_LIMIT_CAP}; refusing rather than silently clamping"
        )
    return row_limit


def prepare_describe_lead_fields(params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Validate describe_lead_fields params and resolve the export path — no vendor call."""
    output_tsv_path = _require_str(params, "output_tsv_path")
    return {"resolved_path": _gate_and_check_parent(path_gate, output_tsv_path)}


def execute_describe_lead_fields(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Fetch the full lead field metadata list; write it to the prepared path, return a handle.

    ``searchableFields`` is absent on some instances (the v1
    describe endpoint can omit the top-level ``searchableFields`` key
    entirely). Absence is not evidence the instance has no
    searchable fields, so it is carried through as ``None`` rather than
    failing the whole call, matching ``_read_only_rest_field_names``'s
    missing-metadata discipline below. A present-but-malformed value still
    fails loudly, since that signals real response corruption rather than a
    field the response simply does not carry.
    """
    payload = client.get_json(LEADS_DESCRIBE_PATH)
    fields = payload.get("result") or []
    searchable_fields = payload.get("searchableFields")
    if searchable_fields is not None and not isinstance(searchable_fields, list):
        raise ValueError("Marketo describe response returned a malformed searchableFields")
    result = _write_records_tsv(fields if isinstance(fields, list) else [], request["resolved_path"])
    result["searchable_fields"] = searchable_fields
    return result


def describe_lead_fields(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Synchronous prepare+execute convenience wrapper.

    Used by check_setup's read probe and any direct in-process caller.
    """
    return execute_describe_lead_fields(client, prepare_describe_lead_fields(params, path_gate))


def prepare_list_activity_types(params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Validate list_activity_types params and resolve the export path — no vendor call."""
    output_tsv_path = _require_str(params, "output_tsv_path")
    return {"resolved_path": _gate_and_check_parent(path_gate, output_tsv_path)}


def execute_list_activity_types(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Fetch the instance's authoritative activity type catalog; write it to the prepared path."""
    payload = client.get_json(ACTIVITY_TYPES_PATH)
    records = payload.get("result") or []
    return _write_records_tsv(records if isinstance(records, list) else [], request["resolved_path"])


def list_activity_types(
    client: Any,
    params: dict[str, Any],
    path_gate: PathGate,
) -> dict[str, Any]:
    """Synchronous prepare+execute convenience wrapper.

    Used by check_setup's read probe and any direct in-process caller.
    """
    return execute_list_activity_types(client, prepare_list_activity_types(params, path_gate))


def prepare_get_api_usage(_params: dict[str, Any]) -> dict[str, Any]:
    """No params to validate and no export path to resolve — no vendor call either way."""
    return {}


def execute_get_api_usage(client: Any, _request: dict[str, Any]) -> dict[str, Any]:
    """Return current-day REST API call totals and per-user usage."""
    payload = client.get_json(API_USAGE_PATH)
    records = payload.get("result") or []
    if not isinstance(records, list):
        raise ValueError("Marketo API usage response result must be a list")
    if not records:
        return {
            "records": [],
            "row_count": 0,
            "date": None,
            "calls_today": None,
            "users": [],
        }
    current = records[0]
    if not isinstance(current, dict):
        raise ValueError("Marketo API usage response contains a non-object record")
    date = current.get("date")
    total = current.get("total")
    users = current.get("users")
    if not isinstance(date, str) or not date:
        raise ValueError("Marketo API usage response omitted date")
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError("Marketo API usage response omitted integer total")
    if not isinstance(users, list):
        raise ValueError("Marketo API usage response omitted users")
    return {
        "records": records,
        "row_count": len(records),
        "date": date,
        "calls_today": total,
        "users": users,
    }


def get_api_usage(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Synchronous prepare+execute convenience wrapper.

    Used by check_setup's read probe and any direct in-process caller.
    """
    return execute_get_api_usage(client, prepare_get_api_usage(params))


def prepare_get_leads(params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Validate get_leads params and resolve the export path — no vendor call.

    D0.3 dispatch/worker split: this is the ms-scale piece that runs on the
    dispatch path; :func:`execute_get_leads` does the actual paginated fetch
    in the background worker.
    """
    output_tsv_path = _require_str(params, "output_tsv_path")
    resolved_path = _gate_and_check_parent(path_gate, output_tsv_path)
    filter_type = _require_str(params, "filter_type")
    filter_values = _require_list(params, "filter_values", max_len=MAX_FILTER_VALUES)
    effective_limit = _resolve_effective_limit(params, verb="get_leads")
    fields = params.get("fields")
    fields_out = [str(f) for f in fields] if isinstance(fields, list) and fields else None
    return {
        "resolved_path": resolved_path,
        "filter_type": filter_type,
        "filter_values": [str(v) for v in filter_values],
        "fields": fields_out,
        "effective_limit": effective_limit,
    }


def execute_get_leads(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Run the paginated leads.json fetch + TSV write from a prepared request.

    Pages internally across Marketo's MARKETO_LIST_PAGE_ROW_CAP (300)
    per-call ceiling up to the effective row limit (§5: 500 default,
    acknowledge_default_limit_override + row_limit raises it to
    MARKETO_LIST_ROW_LIMIT_CAP). No pagination token or continuation
    parameter exists on this verb — hidden by the 2026-08-03 hide-paging change. Beyond the
    hard cap: no resumption; re-invoke with a narrower filter_values slice.
    """
    query: dict[str, Any] = {
        "filterType": request["filter_type"],
        "filterValues": ",".join(request["filter_values"]),
    }
    fields = request.get("fields")
    if fields:
        query["fields"] = ",".join(fields)
    leads, truncated = _paginate_token_authoritative(client, "/rest/v1/leads.json", query, request["effective_limit"])
    result = _write_records_tsv(leads, request["resolved_path"])
    result["truncated"] = truncated
    return result


def get_leads(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Synchronous prepare+execute convenience wrapper.

    Used by check_setup's read probe and any direct in-process caller. The
    plugin's dispatch handler calls :func:`prepare_get_leads` /
    :func:`execute_get_leads` separately, split across the D0.3 dispatch
    (ms-scale) / background-worker (real vendor I/O) boundary.
    """
    return execute_get_leads(client, prepare_get_leads(params, path_gate))


def prepare_get_activities(params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Validate get_activities params and resolve the export path — no vendor call.

    D0.3 dispatch/worker split: this is the ms-scale piece that runs on the
    dispatch path. Unlike :func:`prepare_get_leads`, the ORIGINAL synchronous
    flow minted the activity paging token (a vendor round-trip) as part of
    query-building — that token mint moves entirely into
    :func:`execute_get_activities` here, since dispatch-time validation must
    do zero vendor I/O, not just avoid the pagination loop.
    """
    output_tsv_path = _require_str(params, "output_tsv_path")
    resolved_path = _gate_and_check_parent(path_gate, output_tsv_path)
    since_datetime = params.get("since_datetime")
    if not (isinstance(since_datetime, str) and since_datetime.strip()):
        raise ValueError("'get_activities' requires 'since_datetime' (ISO-8601)")
    type_ids = _require_list(params, "activity_type_ids", max_len=MAX_ACTIVITY_TYPE_IDS)
    lead_ids_out: list[str] | None = None
    if params.get("lead_ids") is not None:
        lead_ids = _require_list(params, "lead_ids", max_len=MAX_ACTIVITY_LEAD_IDS)
        lead_ids_out = [str(lead_id) for lead_id in lead_ids]
    effective_limit = _resolve_effective_limit(params, verb="get_activities")
    return {
        "resolved_path": resolved_path,
        "since_datetime": since_datetime.strip(),
        "activity_type_ids": [str(type_id) for type_id in type_ids],
        "lead_ids": lead_ids_out,
        "effective_limit": effective_limit,
    }


def execute_get_activities(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Mint the paging token and run the paginated activity-log fetch + TSV write.

    This is the read that lets a caller verify what a destructive write caused
    (e.g. whether a ``merge_leads`` call resulted in a Send Email / Send Alert
    activity). ``since_datetime`` (ISO-8601) mints a paging token and pages
    internally from that instant up to the effective row limit (§5: 500
    default, acknowledge_default_limit_override + row_limit raises it to
    MARKETO_LIST_ROW_LIMIT_CAP) — no pagination token or continuation
    parameter exists on this verb, hidden by the 2026-08-03 hide-paging change. Beyond the
    hard cap: no resumption; re-invoke with a later since_datetime.

    HONEST SCOPE — this verb is an AFTER-THE-FACT audit, not a pre-flight
    guarantee. It answers "did anything notify a human since T", and it makes a
    dry run on one sacrificial record possible. It CANNOT prove that a future
    merge will stay silent: that depends on which trigger campaigns are active
    and how their filters match, which no activity read can predict. Do not let
    a clean result here be reported as "merges are safe".

    PAGING — ``moreResult`` is the ONLY USABLE continuation signal here
    (:func:`_paginate_activities` drives the internal loop on it), which is
    not the same as a verified reliable one. Adobe documents that this
    endpoint ALWAYS returns ``nextPageToken`` (Developer Guide → REST → Lead
    Database → Activities), so token presence cannot terminate the loop — the
    exact inverse of :func:`_paginate_token_authoritative`'s verbs, which
    ignore the vendor flag because there the token IS the honest signal. Adobe
    also states the endpoint "can return fewer than 300 activity items while
    setting ``moreResult`` to true", so a SHORT page — including an empty one
    — does not mean the end of the data, and the internal loop does not treat
    it as one.

    ⚠ UNMEASURED, and this is the open risk: the flag's reliability on THIS
    endpoint has never been checked against a live instance. The only live
    measurement of ``moreResult`` anywhere found it VIOLATED on
    ``list_campaigns`` — a full 300-record page reporting false while carrying
    a usable token (field-verified against a live instance). Before this
    build, that was survivable because the CALLER still held a token and
    could keep going; now that the token is hidden, an under-reporting flag
    means this verb returns incomplete data with ``truncated: false`` and
    nothing for a caller to notice with — the ``truncated`` this code computes
    is only as honest as Marketo's own flag. The hermetic smokes assert that
    this code implements the documented rule; they cannot and do not show
    that Marketo honours it.
    """
    query: dict[str, Any] = {
        "nextPageToken": _mint_activity_paging_token(client, request["since_datetime"]),
        "activityTypeIds": ",".join(request["activity_type_ids"]),
    }
    if request.get("lead_ids"):
        query["leadIds"] = ",".join(request["lead_ids"])
    activities, _last_token, truncated = _paginate_activities(client, query, request["effective_limit"])
    result = _write_records_tsv(activities, request["resolved_path"])
    result["truncated"] = truncated
    return result


def get_activities(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Synchronous prepare+execute convenience wrapper.

    Used by any direct in-process caller. The plugin's dispatch handler calls
    :func:`prepare_get_activities` / :func:`execute_get_activities`
    separately, split across the D0.3 dispatch (ms-scale) / background-worker
    (real vendor I/O, including the paging-token mint) boundary.
    """
    return execute_get_activities(client, prepare_get_activities(params, path_gate))


def _mint_activity_paging_token(client: Any, since_datetime: str) -> str:
    """Exchange a whole-second ISO-8601 instant for an activity paging token."""
    wire_since_datetime = _ISO_8601_FRACTIONAL_SECONDS.sub(
        r"\1",
        since_datetime,
        count=1,
    )
    payload = client.get_json(
        ACTIVITY_PAGING_TOKEN_PATH,
        params={"sinceDatetime": wire_since_datetime},
    )
    token = payload.get("nextPageToken")
    if not isinstance(token, str) or not token:
        raise ValueError(
            f"Marketo returned no nextPageToken for since_datetime={since_datetime!r} "
            "(it must be ISO-8601, e.g. 2026-07-28T00:00:00-07:00)"
        )
    return token


def create_or_update_leads(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create/update leads after refusing intended REST read-only fields."""
    action = params.get("action") or DEFAULT_LEAD_ACTION
    if action not in LEAD_ACTIONS:
        raise ValueError(f"'action' must be one of {sorted(LEAD_ACTIONS)}, got {action!r}")
    records = _require_lead_records(params)
    lookup_field = _optional_lookup_field(params)
    _refuse_read_only_lead_fields(
        client,
        records,
        lookup_field or DEFAULT_LEAD_LOOKUP_FIELD,
    )
    body: dict[str, Any] = {"action": action, "input": records}
    if lookup_field is not None:
        body["lookupField"] = lookup_field
    payload = client.post_json("/rest/v1/leads.json", json=body)
    return _batch_envelope(payload)


def delete_leads(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Delete up to MAX_BATCH_RECORDS leads by id."""
    lead_ids = _require_list(params, "lead_ids", max_len=MAX_BATCH_RECORDS)
    body = {"input": [{"id": lead_id} for lead_id in lead_ids]}
    payload = client.post_json("/rest/v1/leads/delete.json", json=body)
    return _batch_envelope(payload)


def merge_leads(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Merge up to 25 losing leads into one winning lead.

    ``merge_in_crm=True`` also merges the natively-synced CRM records — Marketo
    itself restricts a CRM merge to exactly ONE losing lead per call (not 25),
    so that cap is enforced here rather than left to a server-side 1080.
    Read-only fields retain the winner's value even when it is empty; they are
    not populated from a losing record under the general merge-precedence rule.
    """
    winning_lead_id = _require_str(params, "winning_lead_id")
    merge_in_crm = params.get("merge_in_crm")
    if merge_in_crm is not None and not isinstance(merge_in_crm, bool):
        raise ValueError("'merge_in_crm' must be a boolean if provided")
    cap = MAX_MERGE_LOSING_LEADS_CRM if merge_in_crm else MAX_MERGE_LOSING_LEADS
    losing_lead_ids = _require_list(params, "losing_lead_ids", max_len=cap)
    query: dict[str, Any] = {"leadIds": ",".join(str(lead_id) for lead_id in losing_lead_ids)}
    if merge_in_crm is not None:
        query["mergeInCRM"] = "true" if merge_in_crm else "false"
    payload = client.post_json(f"/rest/v1/leads/{winning_lead_id}/merge.json", params=query)
    return {"success": bool(payload.get("success", True)), "request_id": payload.get("requestId")}


def prepare_list_campaigns(params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Validate list_campaigns params and resolve the export path — no vendor call.

    D0.3 dispatch/worker split: this is the ms-scale piece that runs on the
    dispatch path; :func:`execute_list_campaigns` does the actual paginated
    fetch in the background worker.
    """
    output_tsv_path = _require_str(params, "output_tsv_path")
    resolved_path = _gate_and_check_parent(path_gate, output_tsv_path)
    effective_limit = _resolve_effective_limit(params, verb="list_campaigns")
    names = params.get("names")
    program_names = params.get("program_names")
    return {
        "resolved_path": resolved_path,
        "names": [str(n) for n in names] if isinstance(names, list) and names else None,
        "program_names": [str(n) for n in program_names] if isinstance(program_names, list) and program_names else None,
        "effective_limit": effective_limit,
    }


def execute_list_campaigns(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Run the paginated campaigns.json fetch + TSV write from a prepared request.

    Marketo caps a single page at 300 campaigns; this verb previously
    DISCARDED the response's own paging fields, so an instance with more than
    300 campaigns silently returned an arbitrary 300-campaign slice — and
    repeat calls could return DIFFERENT slices, which reads as data rather
    than as truncation. Now pages internally up to the effective row limit
    (§5: 500 default, acknowledge_default_limit_override + row_limit raises
    it to MARKETO_LIST_ROW_LIMIT_CAP) — no pagination token or continuation
    parameter exists on this verb, hidden by the 2026-08-03 hide-paging change. Beyond the
    hard cap: no resumption; re-invoke with a narrower names/program_names
    filter.
    """
    query: dict[str, Any] = {}
    if request.get("names"):
        query["name"] = request["names"]
    if request.get("program_names"):
        query["programName"] = request["program_names"]
    campaigns, truncated = _paginate_token_authoritative(client, "/rest/v1/campaigns.json", query, request["effective_limit"])
    result = _write_records_tsv(campaigns, request["resolved_path"])
    result["truncated"] = truncated
    return result


def list_campaigns(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Synchronous prepare+execute convenience wrapper.

    Used by check_setup's read probe and any direct in-process caller. The
    plugin's dispatch handler calls :func:`prepare_list_campaigns` /
    :func:`execute_list_campaigns` separately, split across the D0.3 dispatch
    (ms-scale) / background-worker (real vendor I/O) boundary.
    """
    return execute_list_campaigns(client, prepare_list_campaigns(params, path_gate))


def trigger_campaign(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Trigger a campaign (Request Campaign) for up to MAX_TRIGGER_LEADS leads, with optional tokens."""
    campaign_id = _require_str(params, "campaign_id")
    lead_ids = _require_list(params, "lead_ids", max_len=MAX_TRIGGER_LEADS)
    input_body: dict[str, Any] = {"leads": [{"id": lead_id} for lead_id in lead_ids]}
    tokens = params.get("tokens")
    if isinstance(tokens, list) and tokens:
        for token in tokens:
            if not isinstance(token, dict) or "name" not in token or "value" not in token:
                raise ValueError("every entry in 'tokens' must be an object with 'name' and 'value'")
        input_body["tokens"] = tokens
    payload = client.post_json(f"/rest/v1/campaigns/{campaign_id}/trigger.json", json={"input": input_body})
    return {"success": bool(payload.get("success", True)), "request_id": payload.get("requestId")}


def prepare_list_static_lists(params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Validate list_static_lists params and resolve the export path — no vendor call.

    D0.3 dispatch/worker split: this is the ms-scale piece that runs on the
    dispatch path; :func:`execute_list_static_lists` does the actual
    paginated fetch in the background worker.
    """
    output_tsv_path = _require_str(params, "output_tsv_path")
    resolved_path = _gate_and_check_parent(path_gate, output_tsv_path)
    effective_limit = _resolve_effective_limit(params, verb="list_static_lists")
    names = params.get("names")
    return {
        "resolved_path": resolved_path,
        "names": [str(n) for n in names] if isinstance(names, list) and names else None,
        "effective_limit": effective_limit,
    }


def execute_list_static_lists(client: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Run the paginated lists.json fetch + TSV write from a prepared request.

    Same page-cap exposure as :func:`list_campaigns` — it is a class of
    defect, not one verb. Pages internally up to the effective row limit (§5:
    500 default, acknowledge_default_limit_override + row_limit raises it to
    MARKETO_LIST_ROW_LIMIT_CAP) — no pagination token or continuation
    parameter exists on this verb, hidden by the 2026-08-03 hide-paging change. Beyond the
    hard cap: no resumption; re-invoke with a narrower names filter.
    """
    query: dict[str, Any] = {}
    if request.get("names"):
        query["name"] = request["names"]
    lists_, truncated = _paginate_token_authoritative(client, "/rest/v1/lists.json", query, request["effective_limit"])
    result = _write_records_tsv(lists_, request["resolved_path"])
    result["truncated"] = truncated
    return result


def list_static_lists(client: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Synchronous prepare+execute convenience wrapper.

    Used by check_setup's read probe and any direct in-process caller. The
    plugin's dispatch handler calls :func:`prepare_list_static_lists` /
    :func:`execute_list_static_lists` separately, split across the D0.3
    dispatch (ms-scale) / background-worker (real vendor I/O) boundary.
    """
    return execute_list_static_lists(client, prepare_list_static_lists(params, path_gate))


def add_leads_to_list(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Add up to MAX_BATCH_RECORDS leads to a static list by id."""
    list_id = _require_str(params, "list_id")
    lead_ids = _require_list(params, "lead_ids", max_len=MAX_BATCH_RECORDS)
    body = {"input": [{"id": lead_id} for lead_id in lead_ids]}
    payload = client.post_json(f"/rest/v1/lists/{list_id}/leads.json", json=body)
    return _batch_envelope(payload)


def remove_leads_from_list(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Remove up to MAX_BATCH_RECORDS leads from a static list by id (DELETE-with-body)."""
    list_id = _require_str(params, "list_id")
    lead_ids = _require_list(params, "lead_ids", max_len=MAX_BATCH_RECORDS)
    body = {"input": [{"id": lead_id} for lead_id in lead_ids]}
    payload = client.delete_json(f"/rest/v1/lists/{list_id}/leads.json", json=body)
    return _batch_envelope(payload)


def check_setup(client: Any) -> dict[str, Any]:
    """Probe the configured API user's READ-ONLY capabilities; report gaps by name.

    Runs the six safe, side-effect-free read probes in :data:`CHECK_SETUP_PROBES`
    (lead describe/query, activity-type listing, API usage, campaign listing,
    and static-list listing) and classifies any failure via
    :func:`errors.classify_marketo_envelope`. Deliberately does NOT probe any
    write/execute verb (create_or_update_leads, delete_leads, add/remove_leads_
    from_list, trigger_campaign) — there is no way to test those permissions
    without performing the action itself, so ``reads_verified`` is a PARTIAL
    guarantee: it names ``writes_unverified`` explicitly rather than implying
    the whole setup is confirmed ready. The five data-export-migrated probes
    write to a throwaway tempfile via a passthrough gate, never the operator's
    real workspace — this is a permission probe, not an export, and must not
    depend on export_allowed_roots being configured.
    """
    checks: list[dict[str, Any]] = []
    for verb, label, permission in CHECK_SETUP_PROBES:
        check: dict[str, Any] = {"capability": label, "verb": verb}
        try:
            _run_read_probe(client, verb)
            check["status"] = "ok"
        except MarketoEnvelopeError as exc:
            code, message = classify_marketo_envelope(exc)
            check["status"] = "failed"
            check["error_code"] = code
            check["error_message"] = message
            check["guidance"] = _permission_guidance(code, permission)
        checks.append(check)
    return {
        "reads_verified": all(c["status"] == "ok" for c in checks),
        "checks": checks,
        "writes_unverified": list(CHECK_SETUP_UNVERIFIED_WRITE_VERBS),
        "writes_unverified_note": (
            "These verbs perform a write/execute action, so their Access API "
            "permission cannot be probed without actually running them. A "
            "missing permission surfaces as marketo.permission_denied on "
            "first real use — re-run check_setup after fixing it to confirm "
            "the reads still pass, then retry the write."
        ),
    }


def _run_read_probe(client: Any, verb: str) -> None:
    """Invoke one read-only probe verb; raises MarketoEnvelopeError on failure.

    The five TSV-writing verbs probe through a throwaway tempfile (cleaned up
    unconditionally) with a passthrough gate — this is a permission check, not
    an export, so it must not touch the operator's real export_allowed_roots
    workspace or depend on it being configured at all.

    get_leads/list_campaigns/list_static_lists explicitly force
    row_limit=1 via the §5 override — a cheap probe should stay a single
    minimal vendor call, never the now-internal-looping default of
    DEFAULT_ROW_LIMIT (a quota-safety fix riding the 2026-08-03 hide-paging
    change: before internal paging existed these three were already
    single-call by construction; now they are not unless bounded here).
    """
    if verb == "get_api_usage":
        get_api_usage(client, {})
        return
    fd, tmp_path = tempfile.mkstemp(suffix=".tsv", prefix="marketo_check_setup_")
    os.close(fd)
    probe_row_limit = {PARAM_ACKNOWLEDGE_OVERRIDE: True, PARAM_ROW_LIMIT: 1}
    try:
        if verb == "describe_lead_fields":
            describe_lead_fields(client, {"output_tsv_path": tmp_path}, _passthrough_path_gate)
        elif verb == "get_leads":
            get_leads(
                client,
                {
                    "filter_type": "id",
                    "filter_values": ["0"],
                    "output_tsv_path": tmp_path,
                    **probe_row_limit,
                },
                _passthrough_path_gate,
            )
        elif verb == "list_activity_types":
            list_activity_types(client, {"output_tsv_path": tmp_path}, _passthrough_path_gate)
        elif verb == "list_campaigns":
            list_campaigns(client, {"output_tsv_path": tmp_path, **probe_row_limit}, _passthrough_path_gate)
        elif verb == "list_static_lists":
            list_static_lists(client, {"output_tsv_path": tmp_path, **probe_row_limit}, _passthrough_path_gate)
        else:  # pragma: no cover — CHECK_SETUP_PROBES is the only caller-controlled source
            raise ValueError(f"unknown check_setup probe verb {verb!r}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _passthrough_path_gate(path: str) -> str:
    """check_setup's probe gate — no containment, a throwaway tempfile only."""
    return path


def _permission_guidance(error_code: str, permission: str | None) -> str:
    """Human remediation text for one failed check_setup probe."""
    if error_code == ERROR_PERMISSION_DENIED:
        if permission is not None:
            return (
                f"Add '{permission}' under Admin > Users & Roles > Roles > "
                "<the API role> > Access API, then re-run check_setup."
            )
        return (
            "The Access API Role is missing a permission for this capability "
            "(the exact checkbox is unconfirmed for this operation) — open "
            "Admin > Users & Roles > Roles > <the API role> > Access API and "
            "compare against what similar read verbs need, then re-run "
            "check_setup."
        )
    if error_code == ERROR_PARTITION_ACCESS_DENIED:
        return (
            "The API user isn't assigned to the workspace/partition this "
            "request targets — check Admin > Users & Roles > Users (the "
            "API-only user's partition assignment), NOT the Access API Role "
            "permissions."
        )
    if error_code == ERROR_AUTH_FAILED:
        return (
            "The OAuth client_id/client_secret were rejected — verify the "
            "LaunchPoint custom service's credentials in the marketo_instance "
            "address-book entry; this is a credential problem, not a Role "
            "permission gap."
        )
    return "Unexpected error — inspect error_message for detail."


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _paginate_token_authoritative(
    client: Any,
    path: str,
    base_query: dict[str, Any],
    cap: int,
) -> tuple[list[Any], bool]:
    """Page an endpoint whose ``nextPageToken`` presence is the authoritative
    continuation signal (Marketo's own ``moreResult`` can be false on a full
    page that still carries a usable token, so token presence wins, never the
    flag). Used by ``get_leads``, ``list_campaigns``, and ``list_static_lists``.

    Loops internally until the token is absent, ``cap`` is reached, or a
    generous call-count breaker trips (defense against a future contract
    change; sized off ``cap`` itself, never expected to bind in practice).
    ``base_query`` is reused unmodified as the first call's query and as the
    template for every continuation call (only ``nextPageToken`` changes
    between calls). Returns (records limited to cap, truncated) — truncated
    is True whenever more data may exist beyond what was returned: cap was
    reached with a token still present, a fetched page pushed past cap before
    a token-absent confirmation, or the breaker tripped.
    """
    records: list[Any] = []
    query = dict(base_query)
    max_calls = cap // MARKETO_LIST_PAGE_ROW_CAP + _PAGE_CALL_BREAKER_MARGIN
    calls = 0
    while calls < max_calls:
        payload = client.get_json(path, params=query or None)
        page = payload.get("result") or []
        records.extend(page if isinstance(page, list) else [])
        calls += 1
        next_token = payload.get("nextPageToken")
        has_more = isinstance(next_token, str) and bool(next_token)
        if len(records) >= cap:
            return records[:cap], has_more or len(records) > cap
        if not has_more:
            return records, False
        query = dict(base_query)
        query["nextPageToken"] = next_token
    return records[:cap], True


def _paginate_activities(
    client: Any,
    initial_query: dict[str, Any],
    cap: int,
) -> tuple[list[Any], str | None, bool]:
    """Page the activity log internally while ``moreResult`` is true — the
    ONLY usable continuation signal here (Adobe's endpoint ALWAYS returns a
    token, so token presence can never terminate the loop; the inverse of
    :func:`_paginate_token_authoritative`'s verbs). Adobe also documents that
    a page can return FEWER than 300 items — including zero — while still
    setting ``moreResult`` true, so, deliberately unlike a stall-detecting
    exemplar, an empty page is NOT treated as an early-stop signal here; only
    the call-count breaker below bounds a run that never sets ``moreResult``
    false. The flag's reliability on this endpoint is documented but
    UNMEASURED (see ``get_activities``' own docstring), so this loop is
    written to degrade to an honest ``truncated=True`` rather than spin or
    crash if the vendor's signal turns out to misbehave.

    Returns (records limited to cap, the last-seen next_page_token,
    truncated). truncated is True whenever the loop stopped for any reason
    other than the vendor's own ``moreResult: false`` — cap reached, the
    breaker tripped, or the vendor claimed ``moreResult: true`` with no
    usable token to continue on (a documented-impossible but
    unmeasured-reliability edge).
    """
    records: list[Any] = []
    query = dict(initial_query)
    last_token = query.get("nextPageToken")
    more_result = True
    max_calls = cap // MARKETO_LIST_PAGE_ROW_CAP + _PAGE_CALL_BREAKER_MARGIN
    calls = 0
    while more_result and len(records) < cap and calls < max_calls:
        page_records, next_token, more_result = _fetch_activity_page(client, query)
        records.extend(page_records)
        calls += 1
        if next_token is None and more_result:
            # Vendor claims more data exists but gave no token to fetch it
            # with — cannot continue; stop rather than loop forever, and let
            # the truncated computation below report the shortfall honestly.
            break
        if next_token is not None:
            last_token = next_token
        query = dict(initial_query)
        query["nextPageToken"] = last_token
    truncated = more_result or len(records) > cap or calls >= max_calls
    return records[:cap], last_token, truncated


def _fetch_activity_page(client: Any, query: dict[str, Any]) -> tuple[list[Any], str | None, bool]:
    """One activities.json call: (page records, its nextPageToken if usable, moreResult)."""
    payload = client.get_json(ACTIVITIES_PATH, params=query)
    page = payload.get("result") or []
    records = page if isinstance(page, list) else []
    next_token = payload.get("nextPageToken")
    next_token = next_token if isinstance(next_token, str) and next_token else None
    return records, next_token, bool(payload.get("moreResult", False))


def _gate_and_check_parent(path_gate: PathGate, output_tsv_path: str) -> str:
    resolved_path = path_gate(output_tsv_path)
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(
            f"the parent directory of output_tsv_path does not exist ({parent_dir}); "
            "create it first — this verb writes one file, it does not create directories"
        )
    return resolved_path


def _write_records_tsv(records: list[Any], resolved_path: str) -> dict[str, Any]:
    """Write records to resolved_path via tempfile + atomic rename.

    D0.3 section 7 named constraint: a direct ``open(resolved_path, "wb")``
    was safe only under the plugin's prior full seriality — a reader hitting
    an overlapping path mid-write, or two concurrent background-worker jobs
    targeting the same path, becomes possible once the migration's worker
    thread runs jobs off the dispatch path. ``os.replace`` is atomic on the
    same filesystem, so a reader always sees either the old file or the
    complete new one, never a partial write.
    """
    dict_records = [r for r in records if isinstance(r, dict)]
    columns = _ordered_columns(dict_records)
    row_lists = [[_cell_value(record.get(column)) for column in columns] for record in dict_records]
    payload = _to_tsv(columns, row_lists)
    parent_dir = os.path.dirname(resolved_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=parent_dir, prefix=".marketo_tsv_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_path, resolved_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return {"path": resolved_path, "columns": columns, "row_count": len(row_lists)}


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


def _batch_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("result") or []
    results = results if isinstance(results, list) else []
    tallies: dict[str, int] = {}
    for entry in results:
        status = entry.get("status", "unknown") if isinstance(entry, dict) else "unknown"
        tallies[status] = tallies.get(status, 0) + 1
    return {"results": results, "row_count": len(results), "tallies": tallies}


def _require_lead_records(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated lead-record objects with their value types preserved."""
    records = _require_list(params, "records", max_len=MAX_BATCH_RECORDS)
    if any(not isinstance(record, dict) or not record for record in records):
        raise ValueError("every entry in 'records' must be a non-empty object")
    return records


def _optional_lookup_field(params: dict[str, Any]) -> str | None:
    """Validate the optional lookup field; omission selects Marketo's email default."""
    lookup_field = params.get("lookup_field")
    if lookup_field is None:
        return None
    if isinstance(lookup_field, str) and (normalized := lookup_field.strip()):
        return normalized
    raise ValueError("'lookup_field' must be a non-empty string if provided")


def _refuse_read_only_lead_fields(
    client: Any,
    records: list[dict[str, Any]],
    effective_lookup_field: str,
) -> None:
    """Refuse intended read-only writes while allowing keys and default-read echoes."""
    read_only_fields = _read_only_rest_field_names(client)
    key_fields = {effective_lookup_field, LEAD_ID_FIELD}
    echoed_read_only_fields = read_only_fields & (GET_LEADS_DEFAULT_FIELDS - key_fields)
    excluded_fields = key_fields | echoed_read_only_fields
    offending_records: dict[str, list[int]] = {}
    for record_number, record in enumerate(records, start=1):
        for field_name in record:
            if field_name in read_only_fields and field_name not in excluded_fields:
                offending_records.setdefault(field_name, []).append(record_number)
    if not offending_records:
        return
    details = "; ".join(
        f"{field_name} (records {', '.join(str(number) for number in offending_records[field_name])})"
        for field_name in sorted(offending_records)
    )
    raise ValueError(
        "Marketo REST read-only fields cannot be written: "
        f"{details}. Remove every listed field and retry; no write was attempted."
    )


def _read_only_rest_field_names(client: Any) -> set[str]:
    """Read one describe page and return fields explicitly marked REST read-only.

    Missing ``rest`` or ``rest.readOnly`` metadata is deliberately treated as
    writable: absence is not evidence that Marketo will refuse the field.
    Present-but-malformed metadata still fails loudly.
    """
    payload = client.get_json(LEADS_DESCRIBE_PATH)
    fields = payload.get("result")
    if not isinstance(fields, list):
        raise ValueError("Marketo describe response omitted the lead field list")
    read_only_fields: set[str] = set()
    for entry in fields:
        field_name = _read_only_rest_field_name(entry)
        if field_name is not None:
            read_only_fields.add(field_name)
    return read_only_fields


def _read_only_rest_field_name(entry: Any) -> str | None:
    """Validate one describe entry and return its read-only REST name, if any."""
    if not isinstance(entry, dict):
        raise ValueError("Marketo describe response contained a non-object field entry")
    rest = entry.get("rest")
    if rest is None:
        return None
    if not isinstance(rest, dict):
        raise ValueError("Marketo describe response contained malformed REST field metadata")
    read_only = rest.get("readOnly")
    if read_only is None:
        return None
    if not isinstance(read_only, bool):
        raise ValueError("Marketo describe response contained a non-boolean rest.readOnly marker")
    if not read_only:
        return None
    field_name = rest.get("name")
    if not isinstance(field_name, str) or not field_name:
        raise ValueError("Marketo describe response marked an unnamed REST field read-only")
    return field_name


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value)
    raise ValueError(f"'{key}' is required and must be a non-empty string")


def _require_list(params: dict[str, Any], key: str, *, max_len: int) -> list[Any]:
    value = params.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty list")
    if len(value) > max_len:
        raise ValueError(f"'{key}' has {len(value)} entries; the Marketo API caps this call at {max_len}")
    return value
