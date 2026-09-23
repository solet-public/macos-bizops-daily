#!/usr/bin/env python3
"""Drive action smoke tests for g_suite_plugin (no pytest, no live Google).

Exercises the pure drive_actions functions against a faked Drive service and a
faked blob writer — no network, no credentials. Red-first: each check asserts
real behavior, so a regression in drive_actions fails here.

Exercises:
  1. list_files  — row shape + count + query passthrough
  2. list_files  — max clamp to the 100 cap
  3. download_file — metadata + get_media bytes -> blob writer -> file_blob_key
  4. download_file — Google-native doc rejected (points to export verb)
  5. download_file — missing id raises ValueError (gsuite.invalid_params path)
  6. upload_file — blob_key source resolved via attachment_loader -> media upload
  7. upload_file — local path source read from disk; mime inferred
  8. upload_file — both/neither of blob_key+path raises ValueError
  9. create_folder — folder mime type + optional parent passthrough
  10. share_file — permission body shape; invalid role rejected
  11. export_media_to_blob / resolve_export_mime — shared helper contract

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_drive.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.drive_actions import (  # noqa: E402
    create_folder,
    download_file,
    export_media_to_blob,
    list_files,
    resolve_export_mime,
    share_file,
    upload_file,
)
from g_suite_plugin.gmail_actions import OutgoingAttachment  # noqa: E402

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


def _fake_drive(
    list_value: dict[str, Any] | None = None,
    get_value: dict[str, Any] | None = None,
    media_value: bytes = b"",
) -> MagicMock:
    drive = MagicMock()
    files = drive.files.return_value
    files.list.return_value.execute.return_value = list_value or {"files": []}
    files.get.return_value.execute.return_value = get_value or {}
    files.get_media.return_value.execute.return_value = media_value
    return drive


def test_list_files_shape() -> None:
    drive = _fake_drive(
        list_value={
            "files": [
                {"id": "f1", "name": "a.pdf", "mimeType": "application/pdf",
                 "modifiedTime": "2026-07-01T00:00:00Z", "size": "1024"},
                {"id": "f2", "name": "b.png", "mimeType": "image/png",
                 "modifiedTime": "2026-07-02T00:00:00Z", "size": None},
            ]
        }
    )
    result = list_files(drive, {"query": "name contains 'x'"})
    _assert("list count is 2", result["count"] == 2)
    _assert("row id/name carried", result["files"][0]["id"] == "f1" and result["files"][0]["name"] == "a.pdf")
    _assert("size coerced to int", result["files"][0]["size"] == 1024)
    _assert("missing size -> None", result["files"][1]["size"] is None)
    kwargs = drive.files.return_value.list.call_args.kwargs
    _assert("query passed through as q", kwargs.get("q") == "name contains 'x'")


def test_list_files_default_is_500() -> None:
    """Business-data limits (2026-08-02): default raised from 25 to 500 (the
    general policy default; Drive's real single-call max is 1,000, reachable
    via the override below)."""
    drive = _fake_drive()
    list_files(drive, {})
    kwargs = drive.files.return_value.list.call_args.kwargs
    _assert("no max, no override -> pageSize 500", kwargs.get("pageSize") == 500)


def test_list_files_max_below_ceiling_honored() -> None:
    drive = _fake_drive()
    list_files(drive, {"max": 10})
    kwargs = drive.files.return_value.list.call_args.kwargs
    _assert("a smaller explicit max is honored", kwargs.get("pageSize") == 10)


def test_list_files_max_cannot_widen_without_override() -> None:
    drive = _fake_drive()
    list_files(drive, {"max": 5000})
    kwargs = drive.files.return_value.list.call_args.kwargs
    _assert("max never widens past the 500 default without override", kwargs.get("pageSize") == 500)


def test_list_files_override_reaches_above_default() -> None:
    """4-case set, case 2: a valid override raises the ceiling, not silently
    capped back to 500."""
    drive = _fake_drive()
    list_files(drive, {"acknowledge_default_limit_override": True, "row_limit": 800})
    kwargs = drive.files.return_value.list.call_args.kwargs
    _assert("override reaches above 500", kwargs.get("pageSize") == 800)


def test_list_files_override_pair_required_together() -> None:
    """4-case set, case 3: either half alone fails loud, names which was missing."""
    drive = _fake_drive()
    raised_flag_only = False
    try:
        list_files(drive, {"acknowledge_default_limit_override": True})
    except ValueError as exc:
        raised_flag_only = "row_limit" in str(exc)
    _assert("override flag alone fails loud, names row_limit", raised_flag_only)

    raised_limit_only = False
    try:
        list_files(drive, {"row_limit": 800})
    except ValueError as exc:
        raised_limit_only = "acknowledge_default_limit_override" in str(exc)
    _assert("row_limit alone fails loud, names the override flag", raised_limit_only)


def test_list_files_override_above_hard_cap_refused() -> None:
    """4-case set, case 4: above the hard cap is refused, names the cap, never clamped."""
    drive = _fake_drive()
    raised = False
    try:
        list_files(drive, {"acknowledge_default_limit_override": True, "row_limit": 1001})
    except ValueError as exc:
        raised = "1000" in str(exc)
    _assert("row_limit above the 1000 hard cap is refused, names the cap", raised)


def test_download_writes_blob() -> None:
    drive = _fake_drive(
        get_value={"name": "report.pdf", "mimeType": "application/pdf"},
        media_value=b"%PDF-1.7 bytes",
    )
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["content"] = content
        captured["filename"] = filename
        captured["mime"] = mime_type
        return "bl-download-1"

    result = download_file(drive, {"id": "f1"}, writer)
    _assert("returns file_blob_key", result["file_blob_key"] == "bl-download-1")
    _assert("returns name", result["name"] == "report.pdf")
    _assert("blob writer got the bytes", captured.get("content") == b"%PDF-1.7 bytes")
    _assert("blob writer got the mime", captured.get("mime") == "application/pdf")


def test_download_rejects_native_doc() -> None:
    drive = _fake_drive(
        get_value={"name": "Plan", "mimeType": "application/vnd.google-apps.document"}
    )
    raised = ""
    try:
        download_file(drive, {"id": "g1"}, _unused_writer)
    except ValueError as exc:
        raised = str(exc)
    _assert("native doc rejected", "export verb" in raised)
    _assert("get_media not called for native doc", not drive.files.return_value.get_media.called)


def test_download_requires_id() -> None:
    drive = _fake_drive()
    raised = False
    try:
        download_file(drive, {}, _unused_writer)
    except ValueError:
        raised = True
    _assert("missing id raises ValueError", raised)


def test_upload_file_from_blob_key() -> None:
    drive = MagicMock()
    drive.files.return_value.create.return_value.execute.return_value = {
        "id": "up1",
        "webViewLink": "https://drive.example/up1",
    }

    def loader(blob_id: str) -> OutgoingAttachment:
        _assert("loader receives blob id", blob_id == "blob-9")
        return OutgoingAttachment(filename="data.csv", mime_type="text/csv", content=b"x,y\n1,2\n")

    result = upload_file(drive, {"name": "data.csv", "blob_key": "blob-9"}, loader)
    _assert("returns id", result["id"] == "up1")
    _assert("returns web_view_link", result["web_view_link"] == "https://drive.example/up1")
    kwargs = drive.files.return_value.create.call_args.kwargs
    _assert("metadata name set", kwargs["body"]["name"] == "data.csv")
    _assert("no parents key when parent omitted", "parents" not in kwargs["body"])


def test_upload_file_requires_blob_key() -> None:
    drive = MagicMock()
    raised = False
    try:
        upload_file(drive, {"name": "x"}, _unused_loader)
    except ValueError:
        raised = True
    _assert("missing blob_key raises ValueError", raised)


def test_upload_file_ignores_local_path() -> None:
    # SECURITY regression (Codex review 2026-07-08): the removed local `path`
    # source was an arbitrary-local-file-read / corporate-Drive exfil primitive.
    # A `path` with no `blob_key` must NOT read the local file — it must fail the
    # required-blob_key check, and the attachment loader (the only blob/byte
    # source) must never be invoked, so no disk read happens.
    drive = MagicMock()
    raised = False
    try:
        upload_file(drive, {"name": "x", "path": "/etc/passwd"}, _unused_loader)
    except ValueError:
        raised = True
    _assert("path-only upload rejected, no local read", raised)
    _assert(
        "drive.create never called for path-only upload",
        not drive.files.return_value.create.called,
    )


def test_create_folder() -> None:
    drive = MagicMock()
    drive.files.return_value.create.return_value.execute.return_value = {"id": "folder-9"}
    result = create_folder(drive, {"name": "Reports", "parent": "root-1"})
    _assert("returns folder id", result["id"] == "folder-9")
    kwargs = drive.files.return_value.create.call_args.kwargs
    _assert("folder mime type set", kwargs["body"]["mimeType"] == "application/vnd.google-apps.folder")
    _assert("parent passed through", kwargs["body"]["parents"] == ["root-1"])


def test_create_folder_requires_name() -> None:
    drive = MagicMock()
    raised = False
    try:
        create_folder(drive, {})
    except ValueError:
        raised = True
    _assert("missing name raises ValueError", raised)


def test_share_file() -> None:
    drive = MagicMock()
    drive.permissions.return_value.create.return_value.execute.return_value = {"id": "perm-1"}
    result = share_file(drive, {"id": "f1", "email": "alice@example.com", "role": "writer"})
    _assert("returns ok", result["ok"] is True)
    _assert("returns permission_id", result["permission_id"] == "perm-1")
    kwargs = drive.permissions.return_value.create.call_args.kwargs
    _assert("no notification email sent", kwargs.get("sendNotificationEmail") is False)
    _assert("role passed through", kwargs["body"]["role"] == "writer")


def test_share_file_rejects_invalid_role() -> None:
    drive = MagicMock()
    raised = ""
    try:
        share_file(drive, {"id": "f1", "email": "alice@example.com", "role": "owner"})
    except ValueError as exc:
        raised = str(exc)
    _assert("invalid role raises ValueError", "role" in raised)
    _assert("permissions.create not called for invalid role", not drive.permissions.return_value.create.called)


def test_export_media_to_blob() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = b"exported bytes"
    captured: dict[str, Any] = {}

    def writer(content: bytes, filename: str, mime_type: str) -> str:
        captured["content"] = content
        captured["filename"] = filename
        return "bl-export-1"

    blob_key = export_media_to_blob(drive, "doc1", "application/pdf", "doc1.pdf", writer)
    _assert("returns blob key", blob_key == "bl-export-1")
    _assert("writer received export bytes", captured.get("content") == b"exported bytes")
    kwargs = drive.files.return_value.export_media.call_args.kwargs
    _assert("mimeType passed to export_media", kwargs.get("mimeType") == "application/pdf")


def test_export_media_to_blob_rejects_non_bytes() -> None:
    drive = MagicMock()
    drive.files.return_value.export_media.return_value.execute.return_value = "oops"
    raised = False
    try:
        export_media_to_blob(drive, "doc1", "application/pdf", "doc1.pdf", _unused_writer)
    except ValueError:
        raised = True
    _assert("non-bytes export raises ValueError", raised)


def test_resolve_export_mime() -> None:
    mime = resolve_export_mime("CSV", {"csv": "text/csv"})
    _assert("format lookup is case-insensitive", mime == "text/csv")
    raised = False
    try:
        resolve_export_mime("doc", {"csv": "text/csv"})
    except ValueError:
        raised = True
    _assert("unsupported format raises ValueError", raised)


def _unused_writer(content: bytes, filename: str, mime_type: str) -> str:  # pragma: no cover
    raise AssertionError("blob writer should not be called on the rejection paths")


def _unused_loader(blob_id: str) -> OutgoingAttachment:  # pragma: no cover
    raise AssertionError("attachment loader should not be called on the rejection/local-path paths")


# ---------------------------------------------------------------------------
# Blob export service resolution
# ---------------------------------------------------------------------------


def test_store_blob_resolves_service_at_point_of_use() -> None:
    """§20.1 regression: blob_storage_service is constructed AFTER plugin
    readiness, so readiness-time resolution cached None forever and every
    download/export hard-failed; the fix resolves lazily at first use."""
    from g_suite_plugin.plugin import GSuitePlugin

    plugin = GSuitePlugin()
    blob_service = MagicMock()
    blob_service.store_blob.return_value = {
        "action_status": "completed",
        "data": {"blob_id": "blob-drive-1"},
    }
    orch = MagicMock()
    orch.get_service.return_value = blob_service
    plugin.orchestrator_ref = orch
    blob_id = plugin._store_blob(b"x" * 64, "download.pdf", "application/pdf")
    _assert("download blob store succeeds via point-of-use resolution", blob_id == "blob-drive-1")
    plugin._store_blob(b"y", "again.pdf", "application/pdf")
    _assert(
        "one get_service call across two stores (cached)",
        orch.get_service.call_count == 1,
        str(orch.get_service.call_count),
    )


def test_store_blob_unavailable_error_is_self_describing() -> None:
    from g_suite_plugin.oauth.token_store import TokenStoreError
    from g_suite_plugin.plugin import GSuitePlugin

    plugin = GSuitePlugin()
    orch = MagicMock()
    orch.get_service.return_value = None
    plugin.orchestrator_ref = orch
    raised: TokenStoreError | None = None
    try:
        plugin._store_blob(b"z" * 98765, "download.pdf", "application/pdf")
    except TokenStoreError as exc:
        raised = exc
    _assert("unavailable blob storage raises the typed error", raised is not None)
    message = str(raised)
    _assert("error names the observed payload size", "98765" in message, message)
    _assert("error names the filename", "download.pdf" in message, message)


def main() -> int:
    print("\ng_suite_plugin Drive smoke tests")
    print("=" * 40)
    test_list_files_shape()
    test_list_files_default_is_500()
    test_list_files_max_below_ceiling_honored()
    test_list_files_max_cannot_widen_without_override()
    test_list_files_override_reaches_above_default()
    test_list_files_override_pair_required_together()
    test_list_files_override_above_hard_cap_refused()
    test_download_writes_blob()
    test_download_rejects_native_doc()
    test_download_requires_id()
    test_upload_file_from_blob_key()
    test_upload_file_requires_blob_key()
    test_upload_file_ignores_local_path()
    test_create_folder()
    test_create_folder_requires_name()
    test_share_file()
    test_share_file_rejects_invalid_role()
    test_export_media_to_blob()
    test_export_media_to_blob_rejects_non_bytes()
    test_resolve_export_mime()
    test_store_blob_resolves_service_at_point_of_use()
    test_store_blob_unavailable_error_is_self_describing()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Drive smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
