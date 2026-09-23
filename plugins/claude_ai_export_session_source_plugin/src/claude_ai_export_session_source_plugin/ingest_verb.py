"""Shared body of the ``ingest_export`` EDGE verb (claude_ai_export side).

Mechanical clone of the chatgpt_export sibling adapted to the
claude_ai vendor seam triple. Replaces the retired HTTP route at
``POST /api/v1/ledger/claude_ai_export/upload`` per design
``workbench/2026-06-14_unified_url_walker_design.md`` §3 + §4.

Two input shapes (XOR — exactly one MUST be set):

* ``file_path`` — absolute path on the solet-side filesystem.
* ``content_bytes`` — base64-encoded ZIP bytes inline (100 MiB decoded cap).

Internal seams reused from the retired ``routes.py`` ``_persist_and_register``:

1. ``blob_storage_service.store_blob_from_file(...)`` OR
   ``blob_storage_service.store_blob(...)`` — returns ``blob_id``.
2. ``session_ledger_service.register_claude_ai_export_source(blob_id, account_label)``
   — idempotent on the blob id (A2); opens NO batch (PUSHED source); returns
   ``{"source_id"}`` only.
3. ``session_ledger_service.ingest_raw_chunk(source_kind, chunk_text, source_id)``
   — dispatches the ZIP through ``importer.dispatch_pushed`` bound to the
   registered ``source_id`` (the plugin is ``PushedSourceMixin``); returns the
   real push ``batch_id``. The chunk_text MUST be a filesystem path (the
   plugin's ``parse_chunk`` does ``Path(chunk_text)``); we pass either the
   operator-supplied ``file_path`` or, for content-bytes ingest, the resolved
   blob_storage path. Failure PROPAGATES into the ``ingest_export`` envelope.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

CONTENT_BYTES_CAP = 100 * 1024 * 1024  # 100 MiB decoded — design v3 §5.2

# A3 — content-digest blob identity. ``external_id`` is platform-wide unique, so
# the namespaced ``session-ledger-export-sha256-<digest>`` key (not a bare
# SHA256 that could collide with identical bytes from another plugin) makes a
# re-ingest of the same export bytes converge onto the existing blob. The
# ``kind`` tag is the durable export-blob discovery surface the backfill +
# orphan sweep key on.
EXTERNAL_ID_PREFIX = "session-ledger-export-sha256-"
EXPORT_BLOB_KIND = "claude_ai_export_zip"
_HASH_CHUNK_BYTES = 1024 * 1024

ERR_BOTH_PATH_AND_BYTES = "both_path_and_bytes"
ERR_NEITHER_PATH_NOR_BYTES = "neither_path_nor_bytes"
ERR_MISSING_FILENAME_WITH_BYTES = "missing_filename_with_bytes"
ERR_INVALID_BASE64 = "invalid_base64"
ERR_PAYLOAD_TOO_LARGE = "payload_too_large"
ERR_BLOB_STORE_FAILED = "blob_store_failed"
ERR_BLOB_STORE_FROM_FILE_FAILED = "blob_store_from_file_failed"
ERR_SOURCE_REGISTRATION_FAILED = "source_registration_failed"
ERR_IMPORT_KICKOFF_FAILED = "import_kickoff_failed"
ERR_CHUNK_PATH_UNRESOLVED = "chunk_path_unresolved"


class IngestExportError(RuntimeError):
    """Structured failure inside ``ingest_export``."""

    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def perform_ingest(
    *,
    blob_storage_service: Any,
    session_ledger_service: Any,
    file_path: str | None,
    content_bytes: str | None,
    filename: str | None,
    account_label: str | None,
) -> dict[str, str]:
    """Drive the Claude.ai-export ingest end-to-end."""
    _validate_xor(file_path=file_path, content_bytes=content_bytes, filename=filename)
    blob_id = _store_bytes(
        blob_storage_service=blob_storage_service,
        file_path=file_path,
        content_bytes=content_bytes,
        filename=filename,
    )
    registered = _register_source(
        session_ledger_service=session_ledger_service,
        blob_id=blob_id,
        account_label=account_label,
    )
    source_id = registered["source_id"]
    # A2: claude_ai_export is PUSHED — the register verb opens no batch; the
    # push (dispatch_pushed) owns the batch and surfaces its real id here.
    batch_id = _trigger_import(
        blob_storage_service=blob_storage_service,
        session_ledger_service=session_ledger_service,
        file_path=file_path,
        blob_id=blob_id,
        source_id=source_id,
    )
    return {"source_id": source_id, "batch_id": batch_id, "blob_id": blob_id}


def _validate_xor(
    *,
    file_path: str | None,
    content_bytes: str | None,
    filename: str | None,
) -> None:
    has_path = file_path is not None and file_path != ""
    has_bytes = content_bytes is not None and content_bytes != ""
    if has_path and has_bytes:
        raise IngestExportError(
            ERR_BOTH_PATH_AND_BYTES,
            "exactly one of file_path or content_bytes must be supplied; got both",
        )
    if not has_path and not has_bytes:
        raise IngestExportError(
            ERR_NEITHER_PATH_NOR_BYTES,
            "exactly one of file_path or content_bytes must be supplied; got neither",
        )
    if has_bytes and (filename is None or filename == ""):
        raise IngestExportError(
            ERR_MISSING_FILENAME_WITH_BYTES,
            "filename is required when content_bytes is supplied",
        )


def _store_bytes(
    *,
    blob_storage_service: Any,
    file_path: str | None,
    content_bytes: str | None,
    filename: str | None,
) -> str:
    if file_path is not None and file_path != "":
        return _store_blob_from_file(
            blob_storage_service=blob_storage_service,
            file_path=file_path,
            filename=filename,
        )
    assert content_bytes is not None  # XOR validated above
    decoded = _decode_and_cap(content_bytes)
    return _store_blob_inline(
        blob_storage_service=blob_storage_service,
        content=decoded,
        filename=filename or "",
    )


def _decode_and_cap(content_bytes_b64: str) -> bytes:
    try:
        decoded = base64.b64decode(content_bytes_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IngestExportError(
            ERR_INVALID_BASE64,
            f"content_bytes is not valid base64: {exc}",
        ) from exc
    if len(decoded) > CONTENT_BYTES_CAP:
        raise IngestExportError(
            ERR_PAYLOAD_TOO_LARGE,
            f"content_bytes decoded to {len(decoded)} bytes; cap is {CONTENT_BYTES_CAP}",
        )
    return decoded


def _export_blob_metadata(digest: str) -> dict[str, Any]:
    """Content-digest identity + durable ``kind`` tag for a new export blob (A3)."""
    return {
        "external_id": f"{EXTERNAL_ID_PREFIX}{digest}",
        "file_hash": digest,
        "kind": EXPORT_BLOB_KIND,
    }


def _hash_file(file_path: str) -> str:
    """Stream-hash a file (avoids loading the whole ZIP into memory)."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_blob_from_file(
    *,
    blob_storage_service: Any,
    file_path: str,
    filename: str | None,
) -> str:
    """Store the ZIP via blob_storage; reuse the existing blob on external_id conflict.

    Idempotency (A3): the blob is keyed by content digest
    (``session-ledger-export-sha256-<digest>``), so re-firing against the same
    ZIP — even renamed — reuses the existing blob rather than minting a new one.
    The verb reads the file to hash it, then the provider streams it again to
    upload (two sequential reads, accepted — C3). We catch the documented
    ``blob_storage.external_id_conflict`` code, look up the existing blob via
    ``search_blobs``, and continue against the SAME blob_id — downstream events
    de-dup at the repository's ``(source_id, vendor_event_id)`` unique
    constraint, so the net effect of re-fire is zero new ledger rows.
    """
    digest = _hash_file(file_path)
    kwargs: dict[str, Any] = {
        "namespace": "session_ledger",
        "file_path": file_path,
        "mime_type": "application/zip",
        "metadata": _export_blob_metadata(digest),
    }
    if filename is not None and filename != "":
        kwargs["filename"] = filename
    result = blob_storage_service.store_blob_from_file(**kwargs)
    blob_id = _extract_blob_id_or_existing(
        result=result,
        blob_storage_service=blob_storage_service,
        error_code=ERR_BLOB_STORE_FROM_FILE_FAILED,
    )
    return blob_id


def _store_blob_inline(
    *,
    blob_storage_service: Any,
    content: bytes,
    filename: str,
) -> str:
    digest = hashlib.sha256(content).hexdigest()
    result = blob_storage_service.store_blob(
        namespace="session_ledger",
        content=content,
        metadata={
            "filename": filename,
            "mime_type": "application/zip",
            **_export_blob_metadata(digest),
        },
    )
    return _extract_blob_id_or_existing(
        result=result,
        blob_storage_service=blob_storage_service,
        error_code=ERR_BLOB_STORE_FAILED,
    )


def _extract_blob_id(result: object, *, error_code: str) -> str:
    if not isinstance(result, dict):
        raise IngestExportError(
            error_code,
            f"blob_storage_service returned non-dict: {result!r}",
        )
    status = result.get("action_status")
    # Fail-closed (KB "Critical Development Guidelines v2"): a missing/None action_status is NOT a
    # success. The default blob provider always stamps completed/error; a
    # statusless envelope is malformed and must raise, not be read as a stored
    # blob. Uniform with blob_identity_backfill._status_ok (M2).
    if status not in {"completed", "success"}:
        raise IngestExportError(
            error_code,
            f"blob_storage_service failed: status={status!r} error={result.get('error')!r}",
        )
    data = result.get("data") or {}
    blob_id = data.get("blob_id") if isinstance(data, dict) else None
    if not isinstance(blob_id, str) or not blob_id:
        raise IngestExportError(
            error_code,
            f"blob_storage_service returned no blob_id: data={data!r}",
        )
    return blob_id


def _extract_blob_id_or_existing(
    *,
    result: object,
    blob_storage_service: Any,
    error_code: str,
) -> str:
    """Extract blob_id from a store result; on conflict, look up existing.

    Idempotency contract: re-firing the verb against the same ZIP must
    reuse the existing blob rather than fail. The error envelope's exact
    shape varies across action-processor layers, so we extract the
    external_id from whatever stringified error we find and try the
    lookup before falling through.
    """
    if isinstance(result, dict):
        status = result.get("action_status")
        if status in {"error", "failed"}:
            err_repr = repr(result.get("error"))
            ext_id = _parse_external_id_from_conflict_message(err_repr)
            if ext_id is not None:
                logger.info(
                    "claude_ai_export: detected external_id_conflict for %s; "
                    "looking up existing blob to preserve idempotency",
                    ext_id,
                )
                existing = blob_storage_service.search_blobs(
                    namespace="session_ledger",
                    metadata_filters={"external_id": ext_id},
                )
                blob_id = _find_existing_blob_id(existing, ext_id)
                if blob_id is not None:
                    logger.info(
                        "claude_ai_export: reusing existing blob_id=%s for ext_id=%s",
                        blob_id, ext_id,
                    )
                    return blob_id
                logger.warning(
                    "claude_ai_export: search_blobs returned no match for "
                    "ext_id=%s; envelope=%r",
                    ext_id, existing,
                )
    return _extract_blob_id(result, error_code=error_code)


def _parse_external_id_from_conflict_message(message: str) -> str | None:
    marker = "external_id '"
    start = message.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = message.find("'", start)
    if end < 0:
        return None
    return message[start:end]


def _candidate_list(data: dict[str, object]) -> list[object]:
    """First list found under any known result-collection key, else empty."""
    for key in ("files", "blobs", "results", "matches"):
        value = data.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _candidate_external_id(candidate: dict[str, object]) -> str | None:
    """External_id from a candidate, falling back to its ``metadata`` sub-dict."""
    external_id = candidate.get("external_id")
    if isinstance(external_id, str):
        return external_id
    metadata = candidate.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("external_id")
        if isinstance(value, str):
            return value
    return None


def _find_existing_blob_id(search_result: object, external_id: str) -> str | None:
    """Surface the existing blob_id from a search_blobs result envelope.

    The default_blob_storage_plugin envelope is
    ``{action_status: 'completed', data: {files: [{blob_id, metadata: {external_id, ...}}], ...}}``.
    Match by ``metadata.external_id`` and return the top-level ``blob_id``.
    Fallback to candidate-level ``external_id`` for forward-compat with
    flatter envelope shapes.
    """
    if not isinstance(search_result, dict):
        return None
    data = search_result.get("data") or {}
    if not isinstance(data, dict):
        return None
    for candidate in _candidate_list(data):
        if not isinstance(candidate, dict):
            continue
        if _candidate_external_id(candidate) != external_id:
            continue
        blob_id = candidate.get("blob_id") or candidate.get("id")
        if isinstance(blob_id, str) and blob_id:
            return blob_id
    return None


def _register_source(
    *,
    session_ledger_service: Any,
    blob_id: str,
    account_label: str | None,
) -> dict[str, str]:
    """Register the export source row (A2). Returns ``{"source_id"}`` only.

    ``claude_ai_export`` is PUSHED, so ``register_claude_ai_export_source``
    opens no batch — the push owns it. The register verb is idempotent on the
    blob id (re-upload reuses the existing source row).
    """
    try:
        registered = session_ledger_service.register_claude_ai_export_source(
            blob_id=blob_id, account_label=account_label,
        )
    except Exception as exc:
        raise IngestExportError(
            ERR_SOURCE_REGISTRATION_FAILED,
            f"register_claude_ai_export_source raised: {exc}",
        ) from exc
    if not isinstance(registered, dict) or "source_id" not in registered:
        raise IngestExportError(
            ERR_SOURCE_REGISTRATION_FAILED,
            f"register_claude_ai_export_source returned malformed envelope: {registered!r}",
        )
    return {"source_id": str(registered["source_id"])}


def _trigger_import(
    *,
    blob_storage_service: Any,
    session_ledger_service: Any,
    file_path: str | None,
    blob_id: str,
    source_id: str,
) -> str:
    """Drive the ZIP through the pushed-mode importer; return the push batch_id (A2).

    The plugin is ``PushedSourceMixin`` with a ``parse_chunk`` that does
    ``Path(chunk_text)``; chunk_text MUST therefore be a filesystem path. We
    prefer the operator-supplied ``file_path`` when present (avoids a
    blob_storage round-trip); otherwise we resolve the persisted blob via
    ``blob_storage_service.resolve_blob_path('blob://<blob_id>')``.

    ``ingest_raw_chunk`` is called with the registered ``source_id`` so
    ``dispatch_pushed`` binds to THAT row (not first-by-kind — which would
    dispatch against the wrong row when more than one export exists) and
    surfaces the real push ``batch_id``. Failure PROPAGATES (A2 — no swallow):
    an unresolved path or a push error raises, so ``ingest_export`` cannot
    report success while events silently fail to land.
    """
    chunk_path = file_path if (file_path is not None and file_path != "") else (
        blob_storage_service.resolve_blob_path(f"blob://{blob_id}")
    )
    if not isinstance(chunk_path, str) or not chunk_path:
        raise IngestExportError(
            ERR_CHUNK_PATH_UNRESOLVED,
            f"cannot resolve a filesystem path for source={source_id} "
            f"blob_id={blob_id}",
        )
    try:
        result = session_ledger_service.ingest_raw_chunk(
            source_kind="claude_ai_export",
            chunk_text=chunk_path,
            source_id=source_id,
        )
    except Exception as exc:
        raise IngestExportError(
            ERR_IMPORT_KICKOFF_FAILED,
            f"ingest_raw_chunk failed for source={source_id} path={chunk_path}: {exc}",
        ) from exc
    batch_id = result.get("batch_id") if isinstance(result, dict) else None
    if not isinstance(batch_id, str) or not batch_id:
        raise IngestExportError(
            ERR_IMPORT_KICKOFF_FAILED,
            f"ingest_raw_chunk returned no batch_id for source={source_id}: {result!r}",
        )
    return batch_id


__all__ = [
    "CONTENT_BYTES_CAP",
    "ERR_BLOB_STORE_FAILED",
    "ERR_BLOB_STORE_FROM_FILE_FAILED",
    "ERR_BOTH_PATH_AND_BYTES",
    "ERR_CHUNK_PATH_UNRESOLVED",
    "ERR_IMPORT_KICKOFF_FAILED",
    "ERR_INVALID_BASE64",
    "ERR_MISSING_FILENAME_WITH_BYTES",
    "ERR_NEITHER_PATH_NOR_BYTES",
    "ERR_PAYLOAD_TOO_LARGE",
    "ERR_SOURCE_REGISTRATION_FAILED",
    "IngestExportError",
    "perform_ingest",
]
