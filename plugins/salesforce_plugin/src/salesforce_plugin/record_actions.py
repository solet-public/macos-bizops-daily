"""Record verb implementations — pure functions over a ``SalesforceCliExecutor``.

Each function takes the executor and a ``params`` dict, returning a plain
result dict. Invalid parameters raise ``ValueError`` (mapped to
``sf.invalid_params``); CLI/REST faults propagate as ``SalesforceServiceError``
/``SalesforceCliCallError`` to the plugin's classifier
(``errors.classify_salesforce_error``).

Two different CLI surfaces are in play, split by a proven correctness
constraint (verified by reading the sf CLI's own source,
``@salesforce/plugin-data/lib/dataUtils.js::stringToDictionary``):

- **ID-based verbs** (``get_record``, ``describe_sobject``,
  ``delete_record``) use the STABLE `sf data get/delete record --record-id`
  and `sf sobject describe` commands — no field-value mini-language involved,
  so the stable, non-beta surface is the safer choice.
- **Value-carrying verbs** (``create_record``, ``update_record``) use the
  BETA `sf api request rest` command with a JSON body file instead of the
  stable `sf data create/update record --values` command. `--values` parses
  through `stringToDictionary`/`parseKeyValueSequence`, which silently
  coerces any field value that case-insensitively equals "true"/"false" into
  a boolean, and attempts a bare `JSON.parse` on any value containing both
  `{` and `}` — both are genuine data-corruption hazards for ordinary
  business data (e.g. an Account named "True Value Hardware"), not an
  escaping inconvenience a caller can quote around. A JSON body file has no
  such ambiguity.
- **list_sobjects** also uses `api request rest` (GET the global describe
  endpoint) because the stable `sf sobject list` command returns bare object
  names with no `label` field — `api request rest` is the only CLI surface
  that reproduces the `{name, label}` shape this verb has always returned.

``describe_sobject`` trims the (often huge) describe payload to name/type/
label/nillable/updateable per field — the full metadata blob is not returned
inline.
"""

from __future__ import annotations

from typing import Any

from .client import SalesforceCliExecutor


def get_record(executor: SalesforceCliExecutor, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one record by sobject + id, optionally trimmed to specific fields."""
    sobject = _require_str(params, "sobject")
    record_id = _require_str(params, "id")
    fields = _as_str_list(params.get("fields"))
    result = executor.run_json(["data", "get", "record", "--sobject", sobject, "--record-id", record_id])
    record = _strip_attributes(result)
    if fields:
        keep = set(fields) | {"Id"}
        record = {k: v for k, v in record.items() if k in keep}
    return {"record": record}


def describe_sobject(executor: SalesforceCliExecutor, params: dict[str, Any]) -> dict[str, Any]:
    """Describe an sobject's fields (trimmed: name/type/label/nillable/updateable)."""
    sobject = _require_str(params, "sobject")
    described = executor.run_json(["sobject", "describe", "--sobject", sobject])
    raw_fields = described.get("fields") if isinstance(described, dict) else None
    fields = raw_fields if isinstance(raw_fields, list) else []
    return {
        "fields": [
            {
                "name": _as_str(f.get("name")),
                "type": _as_str(f.get("type")),
                "label": _as_str(f.get("label")),
                "nillable": bool(f.get("nillable")),
                "updateable": bool(f.get("updateable")),
            }
            for f in fields
            if isinstance(f, dict)
        ]
    }


def list_sobjects(executor: SalesforceCliExecutor, _params: dict[str, Any]) -> dict[str, Any]:
    """List the org's sobjects (name + label only), via the global describe REST endpoint."""
    described = executor.run_rest("GET", f"services/data/v{executor.api_version}/sobjects/")
    raw = described.get("sobjects") if isinstance(described, dict) else None
    rows = raw if isinstance(raw, list) else []
    return {
        "sobjects": [
            {"name": _as_str(row.get("name")), "label": _as_str(row.get("label"))}
            for row in rows
            if isinstance(row, dict)
        ]
    }


def create_record(executor: SalesforceCliExecutor, params: dict[str, Any]) -> dict[str, Any]:
    """Create a record. Returns the new record's id and success flag."""
    sobject = _require_str(params, "sobject")
    fields = _require_fields(params)
    result = executor.run_rest("POST", f"services/data/v{executor.api_version}/sobjects/{sobject}", body=fields)
    return {
        "id": _as_str(result.get("id")) if isinstance(result, dict) else "",
        "success": bool(result.get("success")) if isinstance(result, dict) else False,
    }


def update_record(executor: SalesforceCliExecutor, params: dict[str, Any]) -> dict[str, Any]:
    """Apply a non-empty ``fields`` object to an existing record by sobject + id."""
    sobject = _require_str(params, "sobject")
    record_id = _require_str(params, "id")
    fields = _require_fields(params)
    executor.run_rest(
        "PATCH", f"services/data/v{executor.api_version}/sobjects/{sobject}/{record_id}", body=fields
    )
    return {"success": True}


def delete_record(executor: SalesforceCliExecutor, params: dict[str, Any]) -> dict[str, Any]:
    """Delete a record by sobject + id (explicit target — RATIFY-2 acceptable-loss class)."""
    sobject = _require_str(params, "sobject")
    record_id = _require_str(params, "id")
    executor.run_json(["data", "delete", "record", "--sobject", sobject, "--record-id", record_id])
    return {"success": True}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_fields(params: dict[str, Any]) -> dict[str, Any]:
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("'fields' is required and must be a non-empty object")
    return fields


def _strip_attributes(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {k: v for k, v in record.items() if k != "attributes"}


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value
