"""Sheets verb implementations — pure functions over a built Sheets service.

Same shape as gmail_actions/drive_actions: take an already-built service client
plus a ``params`` dict, return plain result dicts. ``export_spreadsheet`` takes
the **Drive** service, not the Sheets service — Google-native docs are exported
via Drive's ``export_media``, not the product API (see
``drive_actions.export_media_to_blob``).

Invalid parameters raise ``ValueError`` (-> gsuite.invalid_params); Google API
errors propagate to the plugin's ``HttpError`` classifier; a malformed
*successful* API response raises ``RuntimeError`` (-> gsuite.api_error).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .constants import (
    BLOB_NAMESPACE,
    ERROR_RESULT_TOO_LARGE,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
    SHEETS_DEFAULT_EXPORT_FORMAT,
    SHEETS_DEFAULT_ROW_LIMIT,
    SHEETS_EXPORT_MIME_BY_FORMAT,
    SHEETS_ROW_LIMIT_CAP,
    SHEETS_TAB_FILE_DELIMITERS,
    SHEETS_VALUE_INPUT_OPTION_USER_ENTERED,
)
from .drive_actions import BlobWriter, export_media_to_blob, resolve_export_mime


class ResultTooLargeError(RuntimeError):
    """Raised when get_values' returned grid exceeds the effective row limit.

    Business-data limits (2026-08-02, resource guard): Sheets' values.get has
    no server-side per-call size parameter to push a limit into — it returns
    whatever the requested A1 range contains. Fail LOUD over the effective
    limit rather than silently truncate the grid. This does NOT reduce the
    underlying vendor call's size; narrowing the requested range is still the
    caller's job for that (disclosed in the process description).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_RESULT_TOO_LARGE


def create_spreadsheet(sheets: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create a new spreadsheet with the given title; return its id."""
    title = _require_str(params, "title")
    created = sheets.spreadsheets().create(body={"properties": {"title": title}}).execute()
    return {"id": _as_str(created.get("spreadsheetId"))}


def get_values(sheets: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Read a cell range (A1 notation) as a 2D grid of values.

    Business-data limits (2026-08-02, resource guard, not the data-export requirement):
    defaults to SHEETS_DEFAULT_ROW_LIMIT (500) rows; acknowledge_default_
    limit_override=true + an explicit row_limit (up to SHEETS_ROW_LIMIT_CAP,
    1000, OURS-ARBITRARY -- no vendor citation exists for a values.get row
    ceiling) may raise it. Enforced POST-FETCH (the vendor call itself is not
    narrowed) -- a grid over the effective limit raises ResultTooLargeError,
    never a silent truncation.
    """
    spreadsheet_id = _require_str(params, "id")
    cell_range = _require_str(params, "range")
    effective_limit = _resolve_effective_limit(params, verb="sheets_get_values")
    response = (
        sheets.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=cell_range).execute()
    )
    values = response.get("values") or []
    if len(values) > effective_limit:
        raise ResultTooLargeError(
            f"sheets_get_values: the range returned {len(values)} rows, over the effective "
            f"limit of {effective_limit}; narrow the requested range, or pass "
            f"{PARAM_ACKNOWLEDGE_OVERRIDE}=true with an explicit {PARAM_ROW_LIMIT} (up to "
            f"{SHEETS_ROW_LIMIT_CAP}) to raise the limit"
        )
    return {"values": values}


def _resolve_effective_limit(params: dict[str, Any], *, verb: str) -> int:
    """Resolve the effective row limit from the §5 override pair.

    Absent (or ``acknowledge_default_limit_override`` not exactly ``True``)
    with no ``row_limit``: returns SHEETS_DEFAULT_ROW_LIMIT. Both must be
    given together — the override flag alone, or ``row_limit`` alone, fails
    loud rather than silently honoring half. A ``row_limit`` above
    SHEETS_ROW_LIMIT_CAP is refused, never silently clamped down.
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
        return SHEETS_DEFAULT_ROW_LIMIT
    if not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 1:
        raise ValueError(f"{verb}: '{PARAM_ROW_LIMIT}' must be a positive integer")
    if row_limit > SHEETS_ROW_LIMIT_CAP:
        raise ValueError(
            f"{verb}: '{PARAM_ROW_LIMIT}'={row_limit} exceeds the hard cap of "
            f"{SHEETS_ROW_LIMIT_CAP}; refusing rather than silently clamping"
        )
    return row_limit


def update_values(sheets: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Overwrite a cell range (A1 notation) with a 2D grid of values."""
    spreadsheet_id = _require_str(params, "id")
    cell_range = _require_str(params, "range")
    values = _require_grid(params, "values")
    response = (
        sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption=SHEETS_VALUE_INPUT_OPTION_USER_ENTERED,
            body={"values": values},
        )
        .execute()
    )
    return {"updated_cells": _as_int(response.get("updatedCells"))}


def append_values(sheets: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Append a 2D grid of values after the last row of a range's table."""
    spreadsheet_id = _require_str(params, "id")
    cell_range = _require_str(params, "range")
    values = _require_grid(params, "values")
    response = (
        sheets.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption=SHEETS_VALUE_INPUT_OPTION_USER_ENTERED,
            body={"values": values},
        )
        .execute()
    )
    updates = response.get("updates") or {}
    return {"updated_cells": _as_int(updates.get("updatedCells"))}


def batch_update(sheets: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Apply a list of raw Sheets API batchUpdate request objects."""
    spreadsheet_id = _require_str(params, "id")
    requests = _require_requests(params)
    response = (
        sheets.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
        .execute()
    )
    return {"replies": response.get("replies") or []}


def create_spreadsheet_from_files(sheets: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create a spreadsheet with one tab per entry, values loaded from csv/tsv files.

    Every file is parsed BEFORE the spreadsheet is created, so a bad path or
    extension fails without leaving a half-built document behind.
    """
    title = _require_str(params, "title")
    tabs = _require_tabs(params)
    grids = [(name, _read_delimited_file(path)) for name, path in tabs]

    body = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": name}} for name, _ in grids],
    }
    created = sheets.spreadsheets().create(body=body).execute()
    spreadsheet_id = _as_str(created.get("spreadsheetId"))
    sheet_ids = _sheet_ids_by_title(created)

    total_cells = 0
    for name, grid in grids:
        response = (
            sheets.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{_quote_tab_name(name)}!A1",
                valueInputOption=SHEETS_VALUE_INPUT_OPTION_USER_ENTERED,
                body={"values": grid},
            )
            .execute()
        )
        total_cells += _as_int(response.get("updatedCells"))
    return {
        "id": spreadsheet_id,
        "tabs": [
            {"name": name, "sheet_id": _require_sheet_id(sheet_ids, name)} for name, _ in grids
        ],
        "updated_cells": total_cells,
    }


def _sheet_ids_by_title(created: dict[str, Any]) -> dict[str, int]:
    """Map tab title -> Google-assigned sheetId from a spreadsheets.create response."""
    mapping: dict[str, int] = {}
    for sheet in created.get("sheets") or []:
        properties = sheet.get("properties") or {}
        title = properties.get("title")
        sheet_id = properties.get("sheetId")
        if isinstance(title, str) and isinstance(sheet_id, int) and not isinstance(sheet_id, bool):
            mapping[title] = sheet_id
    return mapping


def _require_sheet_id(sheet_ids: dict[str, int], name: str) -> int:
    # 0 is a real sheetId (the first tab), so a missing echo must raise, never coerce.
    sheet_id = sheet_ids.get(name)
    if sheet_id is None:
        raise RuntimeError(f"create response did not echo a sheetId for tab {name!r}")
    return sheet_id


def _require_tabs(params: dict[str, Any]) -> list[tuple[str, str]]:
    value = params.get("tabs")
    if not isinstance(value, list) or not value:
        raise ValueError("'tabs' is required and must be a non-empty list of {name, file_path}")
    pairs = [_require_tab_entry(entry) for entry in value]
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise ValueError(f"tab names must be unique, got: {names}")
    return pairs


def _require_tab_entry(entry: Any) -> tuple[str, str]:
    if not isinstance(entry, dict):
        raise ValueError("each tab entry must be a dict with 'name' and 'file_path'")
    name = entry.get("name")
    file_path = entry.get("file_path")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("each tab entry needs a non-empty string 'name'")
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("each tab entry needs a non-empty string 'file_path'")
    return (name, file_path)


def _read_delimited_file(file_path: str) -> list[list[str]]:
    path = Path(file_path)
    if not path.is_absolute():
        raise ValueError(f"tab file_path must be absolute: {file_path!r}")
    delimiter = SHEETS_TAB_FILE_DELIMITERS.get(path.suffix.lower())
    if delimiter is None:
        supported = ", ".join(sorted(SHEETS_TAB_FILE_DELIMITERS))
        raise ValueError(f"unsupported tab file extension {path.suffix!r} (supported: {supported})")
    if not path.exists():
        raise ValueError(f"tab file not found: {file_path!r}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        grid = list(csv.reader(handle, delimiter=delimiter))
    if not grid:
        raise ValueError(f"tab file is empty: {file_path!r}")
    return grid


def _quote_tab_name(name: str) -> str:
    """Quote a tab name for A1 notation (embedded single quotes are doubled)."""
    return "'" + name.replace("'", "''") + "'"


def _require_requests(params: dict[str, Any], key: str = "requests") -> list[dict[str, Any]]:
    value = params.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty list of request objects")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"'{key}' must be a list of request objects (dicts)")
    return value


def export_spreadsheet(drive: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Export a spreadsheet to csv/xlsx via Drive's export_media; return sheet_blob_key."""
    spreadsheet_id = _require_str(params, "id")
    fmt = _as_str(params.get("format")) or SHEETS_DEFAULT_EXPORT_FORMAT
    mime = resolve_export_mime(fmt, SHEETS_EXPORT_MIME_BY_FORMAT)
    filename = f"{spreadsheet_id}.{fmt}"
    blob_key = export_media_to_blob(drive, spreadsheet_id, mime, filename, blob_writer)
    return {"sheet_blob_key": blob_key, "namespace": BLOB_NAMESPACE, "filename": filename}


def _require_grid(params: dict[str, Any], key: str) -> list[list[Any]]:
    value = params.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty list of rows")
    if not all(isinstance(row, list) for row in value):
        raise ValueError(f"'{key}' must be a list of rows (each row a list of cell values)")
    return value


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value
