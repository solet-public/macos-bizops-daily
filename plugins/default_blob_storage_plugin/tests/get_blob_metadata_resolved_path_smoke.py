#!/usr/bin/env python3
"""Smoke test for ``get_blob_metadata``'s ``resolved_path`` field.

Regression under test: the blob-export fix converts an oversized connector
result into a ``result_blob_key`` pointer with no registered way to reach the
bytes — ``get_blob_metadata`` returned only ``{blob_id, metadata}``, so a
caller holding a export envelope's ``result_blob_key`` + ``namespace`` had no
path to read the file from a script. The fix adds ``resolved_path`` (reusing
``resolve_blob_path``, per the design) and fails LOUD when metadata exists
but the on-disk file is gone, rather than returning ``resolved_path: null``
silently.

Also exercises the refuted "last-write-wins" premise from the Lane C design
doc (workbench/2026-07-31_architect_four_item_dispatch_designs.md item
3.1): two ``store_blob`` calls using the exact connector call shape
(``{"filename": ..., "mime_type": ...}``, no ``original_name``) with an
IDENTICAL filename must land as two distinct blobs at two distinct on-disk
paths, since storage is keyed by the state-generated ``blob_id``, never by
the metadata ``filename`` field. This is the measured discriminator behind
dropping item 3.1 (see the lane doc / Coordinator-Dawn ruling 2026-08-02).

Uses the REAL ``FilesystemProvider`` against a hand-rolled in-memory state
service (the same faithful stand-in pattern as
``s3_blob_storage_plugin/tests/s3_blob_storage_smoke.py``'s
``InMemoryMetadataState`` — real read/write/update/delete contract, not a
mock of the logic under test) and a real temp directory for blob bytes.

Run:
    .venv/bin/python3 \
        plugins/default_blob_storage_plugin/tests/get_blob_metadata_resolved_path_smoke.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_blob_storage_plugin" / "src"))

from ananta.core.plugins.plugin_contracts import ActionResult  # noqa: E402

from default_blob_storage_plugin.providers.filesystem_provider import (  # noqa: E402
    FilesystemProvider,
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
        print(f"  FAIL  {label}  {msg}")


class InMemoryMetadataState:
    """Faithful in-memory stand-in for StateManagementInterface, scoped to the
    verbs FilesystemProvider's metadata layer actually calls.

    Mirrors the real envelope shapes byte-for-byte (generated_id nested under
    ``data.result.generated_id``; records flat under ``data.records``) so a
    stub-vs-production divergence cannot recur — same rationale as
    ``s3_blob_storage_plugin/tests/s3_blob_storage_smoke.py``'s twin.
    """

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._counter = 0

    def _table(self, table: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(table, [])

    @staticmethod
    def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        return all(row.get(key) == value for key, value in filters.items())

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        filters = dict(query.get("filters", {}))
        matched = [dict(row) for row in self._table(str(query["table"])) if self._matches(row, filters)]
        return {"action_status": "completed", "data": {"records": matched}, "error": None}

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        del namespace
        record = dict(data["record"])
        self._counter += 1
        generated_id = f"bmd-{self._counter:013d}"
        record["id"] = generated_id
        self._table(str(data["table"])).append(record)
        return {"action_status": "completed", "data": {"result": {"generated_id": generated_id}}, "error": None}

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        del namespace
        filters = dict(query.get("filters", {}))
        affected = 0
        for row in self._table(str(query["table"])):
            if self._matches(row, filters):
                row.update(updates)
                affected += 1
        return {"action_status": "completed", "data": {"result": {"updated": affected}}, "error": None}

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        table_name = str(query["table"])
        filters = dict(query.get("filters", {}))
        before = self._table(table_name)
        kept = [row for row in before if not self._matches(row, filters)]
        deleted = len(before) - len(kept)
        self._tables[table_name] = kept
        return {"action_status": "completed", "data": {"result": {"deleted": deleted}}, "error": None}


def _make_provider(app_home: Path) -> FilesystemProvider:
    provider = FilesystemProvider(app_home=str(app_home), config={})
    provider.set_state_service(InMemoryMetadataState())  # type: ignore[arg-type]
    return provider


def _data_of(result: ActionResult) -> dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _require_str_field(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"expected non-empty string field {key!r} in {data!r}")
    return value


def test_resolved_path_present_and_reads_back(tmp_dir: Path) -> None:
    """The core blob-export fix: resolved_path resolves to the real file."""
    provider = _make_provider(tmp_dir)
    content = b"x" * 4096
    store_result = provider.store_blob("marketo_plugin", content, {"filename": "get_leads_results.json", "mime_type": "application/json"})
    _assert("store_blob completed", store_result.get("action_status") == "completed", str(store_result))
    blob_id = _require_str_field(_data_of(store_result), "blob_id")

    meta_result = provider.get_blob_metadata("marketo_plugin", blob_id)
    _assert("get_blob_metadata completed", meta_result.get("action_status") == "completed", str(meta_result))
    data = _data_of(meta_result)
    resolved_path = data.get("resolved_path")
    _assert("resolved_path is present", isinstance(resolved_path, str) and bool(resolved_path), str(data))
    resolved = Path(_require_str_field(data, "resolved_path"))
    _assert("resolved_path points at a real file", resolved.is_file())
    _assert("resolved_path's bytes match what was stored", resolved.read_bytes() == content)


def test_missing_file_errors_loud_not_null(tmp_dir: Path) -> None:
    """Named mutation: metadata exists, file gone -> loud error, not resolved_path: null."""
    provider = _make_provider(tmp_dir)
    store_result = provider.store_blob("marketo_plugin", b"y" * 100, {"filename": "list_campaigns_results.json", "mime_type": "application/json"})
    blob_id = _require_str_field(_data_of(store_result), "blob_id")

    # Delete the on-disk file out from under the metadata record.
    provider._get_blob_path(blob_id).unlink()

    meta_result = provider.get_blob_metadata("marketo_plugin", blob_id)
    _assert(
        "missing-file get_blob_metadata is an error, not a success with resolved_path=null",
        meta_result.get("action_status") == "error",
        str(meta_result),
    )


def test_identical_filenames_do_not_collide(tmp_dir: Path) -> None:
    """3.1 refutation, pinned as a regression: same filename metadata, distinct
    blob_ids and distinct on-disk bytes -- storage is blob_id-keyed, never
    filename-keyed, so there is nothing here for 'unique export filenames' to fix."""
    provider = _make_provider(tmp_dir)
    metadata = {"filename": "get_leads_results.json", "mime_type": "application/json"}
    result_1 = provider.store_blob("marketo_plugin", b"first" * 10, dict(metadata))
    result_2 = provider.store_blob("marketo_plugin", b"second" * 10, dict(metadata))
    blob_id_1 = _require_str_field(_data_of(result_1), "blob_id")
    blob_id_2 = _require_str_field(_data_of(result_2), "blob_id")
    _assert("two exports with the same filename get distinct blob_ids", blob_id_1 != blob_id_2, f"{blob_id_1} vs {blob_id_2}")

    meta_1 = _data_of(provider.get_blob_metadata("marketo_plugin", blob_id_1))
    meta_2 = _data_of(provider.get_blob_metadata("marketo_plugin", blob_id_2))
    path_1 = _require_str_field(meta_1, "resolved_path")
    path_2 = _require_str_field(meta_2, "resolved_path")
    _assert("resolved_path differs across the two exports", path_1 != path_2)
    _assert("record #1 still readable and unclobbered", Path(path_1).read_bytes() == b"first" * 10)
    _assert("record #2 still readable and unclobbered", Path(path_2).read_bytes() == b"second" * 10)
    metadata_1 = meta_1.get("metadata") or {}
    metadata_2 = meta_2.get("metadata") or {}
    _assert(
        "neither record ever got an external_id (by-name retrieval is DOA for these, independent of this fix)",
        "external_id" not in metadata_1 and "external_id" not in metadata_2,
        f"{metadata_1} / {metadata_2}",
    )


def main() -> int:
    print("\ndefault_blob_storage_plugin get_blob_metadata resolved_path smoke tests")
    print("=" * 74)
    tmp_dir = Path(tempfile.mkdtemp(prefix="blob_resolved_path_smoke_"))
    try:
        test_resolved_path_present_and_reads_back(tmp_dir / "case1")
        test_missing_file_errors_loud_not_null(tmp_dir / "case2")
        test_identical_filenames_do_not_collide(tmp_dir / "case3")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All get_blob_metadata resolved_path smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
