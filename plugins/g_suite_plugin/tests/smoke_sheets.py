#!/usr/bin/env python3
"""Sheets action smoke tests for g_suite_plugin (no pytest, no live Google).

Exercises the pure sheets_actions functions against a faked Sheets/Drive
service and a faked blob writer — no network, no credentials. Red-first: each
check asserts real behavior, so a regression in sheets_actions fails here.

Exercises:
  1. create_spreadsheet — returns id, title passed through
  2. get_values         — returns the values grid; missing values -> []
  3. update_values      — valueInputOption + range/body passthrough; count returned
  4. update_values      — non-grid values raises ValueError
  5. append_values      — range/body passthrough; count returned from nested updates
  6. export_spreadsheet — default csv mime, custom xlsx mime, non-bytes rejected
  7. batch_update       — requests passthrough, replies returned; bad requests rejected
  8. create_spreadsheet_from_files — csv+tsv tabs, quoting, cell totals,
     per-tab {name, sheet_id} pairs echoed from the create response; every
     validation rejection (dup names, extension, relative path, missing,
     empty); a create response missing a tab's sheetId raises RuntimeError

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_sheets.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.sheets_actions import (  # noqa: E402
    ResultTooLargeError,
    append_values,
    batch_update,
    create_spreadsheet,
    create_spreadsheet_from_files,
    export_spreadsheet,
    get_values,
    update_values,
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


def _fake_sheets(
    create_value: dict[str, Any] | None = None,
    get_value: dict[str, Any] | None = None,
    update_value: dict[str, Any] | None = None,
    append_value: dict[str, Any] | None = None,
    batch_value: dict[str, Any] | None = None,
) -> MagicMock:
    sheets = MagicMock()
    spreadsheets = sheets.spreadsheets.return_value
    spreadsheets.create.return_value.execute.return_value = create_value or {}
    spreadsheets.batchUpdate.return_value.execute.return_value = batch_value or {}
    values = spreadsheets.values.return_value
    values.get.return_value.execute.return_value = get_value or {}
    values.update.return_value.execute.return_value = update_value or {}
    values.append.return_value.execute.return_value = append_value or {}
    return sheets


def test_create_spreadsheet() -> None:
    sheets = _fake_sheets(create_value={"spreadsheetId": "sp1"})
    result = create_spreadsheet(sheets, {"title": "Budget"})
    _assert("returns spreadsheet id", result["id"] == "sp1")
    kwargs = sheets.spreadsheets.return_value.create.call_args.kwargs
    _assert("title passed through", kwargs["body"]["properties"]["title"] == "Budget")


def test_get_values_shape() -> None:
    sheets = _fake_sheets(get_value={"values": [["a", "b"], ["1", "2"]]})
    result = get_values(sheets, {"id": "sp1", "range": "Sheet1!A1:B2"})
    _assert("values grid returned", result["values"] == [["a", "b"], ["1", "2"]])
    kwargs = sheets.spreadsheets.return_value.values.return_value.get.call_args.kwargs
    _assert("range passed through", kwargs.get("range") == "Sheet1!A1:B2")


def test_get_values_missing_defaults_empty() -> None:
    sheets = _fake_sheets(get_value={})
    result = get_values(sheets, {"id": "sp1", "range": "Sheet1!A1:B2"})
    _assert("missing values -> empty list", result["values"] == [])


def _grid(n_rows: int) -> list[list[str]]:
    return [[f"r{i}c0", f"r{i}c1"] for i in range(n_rows)]


def test_get_values_under_default_returns_inline() -> None:
    """Business-data limits (2026-08-02, resource guard): a grid within the
    500-row default returns normally, no error."""
    sheets = _fake_sheets(get_value={"values": _grid(10)})
    result = get_values(sheets, {"id": "sp1", "range": "Sheet1!A1:B10"})
    _assert("under-default grid returns inline", len(result["values"]) == 10)


def test_get_values_over_default_fails_loud() -> None:
    """4-case set, case 1: over the default, fail loud (gsuite.result_too_large
    via ResultTooLargeError) -- never a silent truncation of the grid."""
    sheets = _fake_sheets(get_value={"values": _grid(501)})
    raised = False
    try:
        get_values(sheets, {"id": "sp1", "range": "Sheet1!A1:Z100000"})
    except ResultTooLargeError as exc:
        raised = True
        _assert("error names the observed row count", "501" in str(exc), str(exc))
        _assert("error names the override mechanism", "acknowledge_default_limit_override" in str(exc), str(exc))
    _assert("501 rows (over the 500 default) fails loud, never truncated", raised)


def test_get_values_override_reaches_above_default() -> None:
    """4-case set, case 2: a valid override raises the effective limit, not
    silently capped back to 500."""
    sheets = _fake_sheets(get_value={"values": _grid(800)})
    result = get_values(
        sheets,
        {"id": "sp1", "range": "Sheet1!A1:Z100000", "acknowledge_default_limit_override": True, "row_limit": 900},
    )
    _assert("800 rows under an 900 override limit returns inline", len(result["values"]) == 800)


def test_get_values_override_pair_required_together() -> None:
    """4-case set, case 3: either half alone fails loud, names which was missing."""
    sheets = _fake_sheets(get_value={"values": _grid(5)})
    raised_flag_only = False
    try:
        get_values(sheets, {"id": "sp1", "range": "Sheet1!A1:B5", "acknowledge_default_limit_override": True})
    except ValueError as exc:
        raised_flag_only = "row_limit" in str(exc)
    _assert("override flag alone fails loud, names row_limit", raised_flag_only)

    raised_limit_only = False
    try:
        get_values(sheets, {"id": "sp1", "range": "Sheet1!A1:B5", "row_limit": 900})
    except ValueError as exc:
        raised_limit_only = "acknowledge_default_limit_override" in str(exc)
    _assert("row_limit alone fails loud, names the override flag", raised_limit_only)


def test_get_values_override_above_hard_cap_refused() -> None:
    """4-case set, case 4: above the hard cap is refused, names the cap, never clamped."""
    sheets = _fake_sheets(get_value={"values": _grid(5)})
    raised = False
    try:
        get_values(
            sheets,
            {
                "id": "sp1",
                "range": "Sheet1!A1:B5",
                "acknowledge_default_limit_override": True,
                "row_limit": 1001,
            },
        )
    except ValueError as exc:
        raised = "1000" in str(exc)
    _assert("row_limit above the 1000 hard cap is refused, names the cap", raised)


def test_get_values_does_not_narrow_the_vendor_call() -> None:
    """Disclosed limitation: the post-fetch check does not reduce the underlying
    vendor request size -- the fake vendor is instructed to return exactly what
    was asked, so the SAME range/spreadsheet_id reach the API regardless of the
    effective limit (narrowing the requested A1 range is still the caller's
    job for that)."""
    sheets = _fake_sheets(get_value={"values": _grid(5)})
    get_values(sheets, {"id": "sp1", "range": "Sheet1!A1:Z999999"})
    kwargs = sheets.spreadsheets.return_value.values.return_value.get.call_args.kwargs
    _assert("the full requested range reaches the vendor call, unmodified", kwargs.get("range") == "Sheet1!A1:Z999999")


def test_update_values() -> None:
    sheets = _fake_sheets(update_value={"updatedCells": 4})
    result = update_values(sheets, {"id": "sp1", "range": "Sheet1!A1:B2", "values": [["x", "y"]]})
    _assert("updated_cells returned", result["updated_cells"] == 4)
    kwargs = sheets.spreadsheets.return_value.values.return_value.update.call_args.kwargs
    _assert("valueInputOption is USER_ENTERED", kwargs.get("valueInputOption") == "USER_ENTERED")
    _assert("values body passed through", kwargs["body"]["values"] == [["x", "y"]])


def test_update_values_rejects_non_grid() -> None:
    sheets = _fake_sheets()
    raised = False
    try:
        update_values(sheets, {"id": "sp1", "range": "Sheet1!A1", "values": ["not", "a", "grid"]})
    except ValueError:
        raised = True
    _assert("non-grid values raises ValueError", raised)


def test_append_values() -> None:
    sheets = _fake_sheets(append_value={"updates": {"updatedCells": 6}})
    result = append_values(sheets, {"id": "sp1", "range": "Sheet1!A1", "values": [["p", "q"]]})
    _assert("updated_cells from nested updates", result["updated_cells"] == 6)
    kwargs = sheets.spreadsheets.return_value.values.return_value.append.call_args.kwargs
    _assert("values body passed through", kwargs["body"]["values"] == [["p", "q"]])


def test_export_spreadsheet_default_csv() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = b"a,b\n1,2\n"
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["content"] = content
        captured["mime"] = mime_type
        return "bl-sheet-1"

    result = export_spreadsheet(drive, {"id": "sp1"}, writer)
    _assert("returns sheet_blob_key", result["sheet_blob_key"] == "bl-sheet-1")
    _assert("default mime is text/csv", captured.get("mime") == "text/csv")
    kwargs = drive.files.return_value.export_media.call_args.kwargs
    _assert("mimeType passed to export_media", kwargs.get("mimeType") == "text/csv")


def test_export_spreadsheet_xlsx() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = b"PK\x03\x04"
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["mime"] = mime_type
        return "bl-sheet-2"

    export_spreadsheet(drive, {"id": "sp1", "format": "xlsx"}, writer)
    _assert(
        "xlsx mime resolved",
        captured.get("mime")
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def test_export_spreadsheet_rejects_unsupported_format() -> None:
    drive = MagicMock()
    raised = ""
    try:
        export_spreadsheet(drive, {"id": "sp1", "format": "pdf"}, _unused_writer)
    except ValueError as exc:
        raised = str(exc)
    _assert("unsupported format raises ValueError", "unsupported export format" in raised)


def test_export_spreadsheet_rejects_non_bytes() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = {"not": "bytes"}
    raised = False
    try:
        export_spreadsheet(drive, {"id": "sp1"}, _unused_writer)
    except ValueError:
        raised = True
    _assert("non-bytes export content raises ValueError", raised)


def test_batch_update() -> None:
    sheets = _fake_sheets(batch_value={"replies": [{"addSheet": {"properties": {"sheetId": 7}}}]})
    requests = [{"addSheet": {"properties": {"title": "Tab2"}}}]
    result = batch_update(sheets, {"id": "sp1", "requests": requests})
    _assert("replies returned", result["replies"] == [{"addSheet": {"properties": {"sheetId": 7}}}])
    kwargs = sheets.spreadsheets.return_value.batchUpdate.call_args.kwargs
    _assert("spreadsheetId passed through", kwargs.get("spreadsheetId") == "sp1")
    _assert("requests body passed through verbatim", kwargs["body"]["requests"] == requests)


def test_batch_update_missing_replies_defaults_empty() -> None:
    sheets = _fake_sheets(batch_value={})
    result = batch_update(sheets, {"id": "sp1", "requests": [{"x": {}}]})
    _assert("missing replies -> empty list", result["replies"] == [])


def test_batch_update_rejects_bad_requests() -> None:
    sheets = _fake_sheets()
    for label, bad in (
        ("empty requests raises ValueError", []),
        ("non-dict request entries raise ValueError", ["not-a-dict"]),
    ):
        raised = False
        try:
            batch_update(sheets, {"id": "sp1", "requests": bad})
        except ValueError:
            raised = True
        _assert(label, raised)


def _tab_files(tmp: Path) -> tuple[str, str]:
    csv_path = tmp / "alpha.csv"
    csv_path.write_text("h1,h2\n1,2\n", encoding="utf-8")
    tsv_path = tmp / "beta.tsv"
    tsv_path.write_text("h1\th2\n3\t4\n", encoding="utf-8")
    return str(csv_path), str(tsv_path)


def test_create_from_files() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        csv_path, tsv_path = _tab_files(Path(tmp))
        sheets = _fake_sheets(
            create_value={
                "spreadsheetId": "sp9",
                "sheets": [
                    {"properties": {"title": "Tab1", "sheetId": 0}},
                    {"properties": {"title": "O'Brien", "sheetId": 411}},
                ],
            },
            update_value={"updatedCells": 4},
        )
        result = create_spreadsheet_from_files(
            sheets,
            {
                "title": "Multi",
                "tabs": [
                    {"name": "Tab1", "file_path": csv_path},
                    {"name": "O'Brien", "file_path": tsv_path},
                ],
            },
        )
        _assert("id returned", result["id"] == "sp9")
        _assert(
            "ordered {name, sheet_id} tabs returned (sheetId 0 kept, not coerced)",
            result["tabs"]
            == [{"name": "Tab1", "sheet_id": 0}, {"name": "O'Brien", "sheet_id": 411}],
        )
        _assert("updated_cells summed across tabs", result["updated_cells"] == 8)
        create_kwargs = sheets.spreadsheets.return_value.create.call_args.kwargs
        _assert(
            "create body carries both tabs in order",
            [s["properties"]["title"] for s in create_kwargs["body"]["sheets"]]
            == ["Tab1", "O'Brien"],
        )
        update_calls = sheets.spreadsheets.return_value.values.return_value.update.call_args_list
        ranges = [c.kwargs.get("range") for c in update_calls]
        _assert("tab ranges quoted (apostrophe doubled)", ranges == ["'Tab1'!A1", "'O''Brien'!A1"])
        _assert(
            "csv rows parsed into first tab grid",
            update_calls[0].kwargs["body"]["values"] == [["h1", "h2"], ["1", "2"]],
        )
        _assert(
            "tsv rows parsed into second tab grid",
            update_calls[1].kwargs["body"]["values"] == [["h1", "h2"], ["3", "4"]],
        )
        _assert(
            "USER_ENTERED input option on tab writes",
            all(c.kwargs.get("valueInputOption") == "USER_ENTERED" for c in update_calls),
        )


def test_create_from_files_rejections() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        csv_path, _ = _tab_files(Path(tmp))
        empty_path = Path(tmp) / "empty.csv"
        empty_path.write_text("", encoding="utf-8")
        txt_path = Path(tmp) / "wrong.txt"
        txt_path.write_text("a,b\n", encoding="utf-8")
        cases = (
            (
                "duplicate tab names raise ValueError",
                [{"name": "T", "file_path": csv_path}, {"name": "T", "file_path": csv_path}],
            ),
            ("unsupported extension raises ValueError", [{"name": "T", "file_path": str(txt_path)}]),
            ("relative path raises ValueError", [{"name": "T", "file_path": "rel/data.csv"}]),
            (
                "missing file raises ValueError",
                [{"name": "T", "file_path": str(Path(tmp) / "nope.csv")}],
            ),
            ("empty file raises ValueError", [{"name": "T", "file_path": str(empty_path)}]),
            ("empty tabs list raises ValueError", []),
        )
        for label, tabs in cases:
            sheets = _fake_sheets()
            raised = False
            try:
                create_spreadsheet_from_files(sheets, {"title": "Multi", "tabs": tabs})
            except ValueError:
                raised = True
            _assert(label, raised)
            _assert(
                label + " (nothing created remotely)",
                not sheets.spreadsheets.return_value.create.called,
            )


def test_create_from_files_missing_sheet_id_echo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        csv_path, _ = _tab_files(Path(tmp))
        sheets = _fake_sheets(
            create_value={"spreadsheetId": "sp9", "sheets": [{"properties": {"title": "Tab1"}}]},
            update_value={"updatedCells": 4},
        )
        raised = ""
        try:
            create_spreadsheet_from_files(
                sheets, {"title": "Multi", "tabs": [{"name": "Tab1", "file_path": csv_path}]}
            )
        except RuntimeError as exc:
            raised = str(exc)
        _assert(
            "create response missing a tab's sheetId raises RuntimeError",
            "did not echo a sheetId" in raised,
            f"raised={raised!r}",
        )


def _unused_writer(content: bytes, filename: str, mime_type: str) -> str:  # pragma: no cover
    raise AssertionError("blob writer should not be called on the rejection paths")


def main() -> int:
    print("\ng_suite_plugin Sheets smoke tests")
    print("=" * 40)
    test_create_spreadsheet()
    test_get_values_shape()
    test_get_values_missing_defaults_empty()
    test_get_values_under_default_returns_inline()
    test_get_values_over_default_fails_loud()
    test_get_values_override_reaches_above_default()
    test_get_values_override_pair_required_together()
    test_get_values_override_above_hard_cap_refused()
    test_get_values_does_not_narrow_the_vendor_call()
    test_update_values()
    test_update_values_rejects_non_grid()
    test_append_values()
    test_export_spreadsheet_default_csv()
    test_export_spreadsheet_xlsx()
    test_export_spreadsheet_rejects_unsupported_format()
    test_export_spreadsheet_rejects_non_bytes()
    test_batch_update()
    test_batch_update_missing_replies_defaults_empty()
    test_batch_update_rejects_bad_requests()
    test_create_from_files()
    test_create_from_files_rejections()
    test_create_from_files_missing_sheet_id_echo()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Sheets smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
