"""Export-blob content-digest identity backfill (A3).

Historical export blobs were stored with a filename-derived (or freshly-minted)
``external_id``, so the same export bytes uploaded twice — or re-ingested after
the A3 content-digest upgrade — produced distinct blobs, distinct
``root_uri``s, and distinct ``__source`` rows. This module converges every live
export source onto a single content-addressed blob keyed by
``session-ledger-export-sha256-<digest>`` and reclaims the now-unreferenced
blobs.

Properties (Codex round-3 B3 / round-5 B1/B2 / round-6 B1):

* **confirm-gated.** ``confirm=False`` computes counts ONLY and mutates
  nothing (no Phase-0 tagging). ``confirm=True`` runs Phase 0 → 1 → 2.
* **fail-fast.** Any per-source / per-blob failure raises ``BackfillError``;
  there is no partial per-source step.
* **forward-only + convergent + idempotent.** Re-running re-converges; a crash
  between a repoint-commit and the orphan delete is healed by the next run's
  Phase-2 sweep (the old blob is still ``kind``-tagged and now unreferenced).

Namespace scope: export blobs always live in the ``session_ledger`` blob
namespace, and the namespaced ``session-ledger-export-sha256-`` ``external_id``
prefix makes a cross-namespace collision astronomically unlikely (no other
plugin mints that key). The C4 "key owned outside session_ledger" guard is
therefore enforced as fail-loud on any metadata-write failure rather than via a
cross-namespace pre-search.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ananta.llm.session_ledger.repository import SessionLedgerRepository
from ananta.llm.session_ledger.types import IngestSourceKind

_LEDGER_NAMESPACE = "session_ledger"
_EXTERNAL_ID_PREFIX = "session-ledger-export-sha256-"
_HASH_CHUNK_BYTES = 1024 * 1024

# Source-kind → durable export-blob ``kind`` tag. Also the set of kinds the
# Phase-2 orphan sweep enumerates.
_KIND_TAG_FOR_SOURCE_KIND: dict[IngestSourceKind, str] = {
    IngestSourceKind.CHATGPT_EXPORT: "chatgpt_export_zip",
    IngestSourceKind.CLAUDE_AI_EXPORT: "claude_ai_export_zip",
}
EXPORT_BLOB_KINDS: tuple[str, ...] = tuple(_KIND_TAG_FOR_SOURCE_KIND.values())

_OK_STATUSES = frozenset({"completed", "success"})


class BackfillError(RuntimeError):
    """Fail-fast error inside the export-blob-identity backfill."""


@dataclass(frozen=True, slots=True)
class _ExportSource:
    source_id: str
    blob_id: str
    kind_tag: str


class ExportBlobIdentityBackfill:
    """Drives the A3 export-blob-identity backfill (see module docstring)."""

    def __init__(
        self,
        *,
        blob_storage_service: Any,
        repository: SessionLedgerRepository,
    ) -> None:
        self._blobs = blob_storage_service
        self._repository = repository

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, *, confirm: bool) -> dict[str, Any]:
        sources = self._live_export_sources()
        if not confirm:
            return self._dry_run(sources)
        return self._confirmed_run(sources)

    # ------------------------------------------------------------------
    # Dry run (read-only)
    # ------------------------------------------------------------------

    def _dry_run(self, sources: list[_ExportSource]) -> dict[str, Any]:
        blobs_needing_key = 0
        sources_needing_repoint = 0
        for src in sources:
            digest = self._hash_blob(src.blob_id)
            content_key = f"{_EXTERNAL_ID_PREFIX}{digest}"
            twin = self._find_keyed_twin(content_key, self_blob_id=src.blob_id)
            if twin is not None:
                sources_needing_repoint += 1
            elif self._blob_external_id(src.blob_id) != content_key:
                blobs_needing_key += 1
        return {
            "confirmed": False,
            "sources_scanned": len(sources),
            "blobs_needing_key": blobs_needing_key,
            "sources_needing_repoint": sources_needing_repoint,
            "orphan_blobs_candidate": len(self._orphan_blob_ids(sources)),
        }

    # ------------------------------------------------------------------
    # Confirmed run (Phase 0 → 1 → 2)
    # ------------------------------------------------------------------

    def _confirmed_run(self, sources: list[_ExportSource]) -> dict[str, Any]:
        # Phase 0 — establish the durable kind tag on every referenced blob
        # BEFORE any pointer moves, so a crash mid-Phase-1 still leaves the
        # orphan sweep able to find a stranded blob.
        for src in sources:
            self._tag_blob_kind(src.blob_id, src.kind_tag)

        blobs_keyed = 0
        sources_repointed = 0
        skipped = 0
        # Phase 1 — per source: hash → key → converge.
        for src in sources:
            digest = self._hash_blob(src.blob_id)
            content_key = f"{_EXTERNAL_ID_PREFIX}{digest}"
            twin = self._find_keyed_twin(content_key, self_blob_id=src.blob_id)
            if twin is not None:
                self._verify_twin(twin, digest)
                if not self._repository.repoint_source_root_uri(src.source_id, twin):
                    raise BackfillError(
                        f"repoint of source {src.source_id} to blob {twin} "
                        "changed no row (source vanished mid-backfill)",
                    )
                sources_repointed += 1
            elif self._blob_external_id(src.blob_id) == content_key:
                skipped += 1
            else:
                self._set_blob_external_id(src.blob_id, content_key)
                blobs_keyed += 1

        # Phase 2 — guarded orphan sweep (runs at the end of EVERY confirmed
        # run so a crash-orphaned blob from a prior run is reclaimed).
        export_blobs_deleted, error_count = self._sweep_orphans(
            self._live_export_sources(),
        )
        return {
            "confirmed": True,
            "sources_scanned": len(sources),
            "blobs_keyed": blobs_keyed,
            "sources_repointed": sources_repointed,
            "export_blobs_deleted": export_blobs_deleted,
            "skipped": skipped,
            "error_count": error_count,
        }

    # ------------------------------------------------------------------
    # Source enumeration
    # ------------------------------------------------------------------

    def _live_export_sources(self) -> list[_ExportSource]:
        out: list[_ExportSource] = []
        for row in self._repository.list_sources(enabled_only=False):
            kind_tag = _KIND_TAG_FOR_SOURCE_KIND.get(row.source_kind)
            if kind_tag is None:
                continue
            out.append(
                _ExportSource(
                    source_id=row.id, blob_id=row.root_uri, kind_tag=kind_tag,
                ),
            )
        return out

    # ------------------------------------------------------------------
    # Phase 2 helpers
    # ------------------------------------------------------------------

    def _orphan_blob_ids(self, live_sources: list[_ExportSource]) -> list[str]:
        """Export-tagged blob ids no live export source references (snapshot)."""
        referenced = {src.blob_id for src in live_sources}
        result = self._blobs.search_blobs(
            namespace=_LEDGER_NAMESPACE,
            metadata_filters={"plugin_metadata.kind": list(EXPORT_BLOB_KINDS)},
        )
        return [
            blob_id
            for blob_id in self._iter_search_blob_ids(result)
            if blob_id not in referenced
        ]

    def _sweep_orphans(
        self, live_sources: list[_ExportSource],
    ) -> tuple[int, int]:
        # Snapshot the candidate ids BEFORE any delete (no offset-advance while
        # deleting). Any delete failure leaves the still-tagged unreferenced
        # blob discoverable for the next run and makes this run fail loud.
        candidates = self._orphan_blob_ids(live_sources)
        deleted = 0
        errors = 0
        for blob_id in candidates:
            result = self._blobs.delete_blob(
                namespace=_LEDGER_NAMESPACE, blob_id=blob_id,
            )
            if self._status_ok(result):
                deleted += 1
            else:
                errors += 1
        if errors:
            raise BackfillError(
                f"orphan sweep: {errors} of {len(candidates)} delete(s) failed; "
                f"{deleted} reclaimed — still-tagged blobs remain for re-run",
            )
        return deleted, errors

    # ------------------------------------------------------------------
    # Blob primitives
    # ------------------------------------------------------------------

    def _hash_blob(self, blob_id: str) -> str:
        path = self._blobs.resolve_blob_path(f"blob://{blob_id}")
        if not isinstance(path, str) or not path:
            raise BackfillError(f"cannot resolve a filesystem path for blob {blob_id}")
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise BackfillError(f"cannot read blob {blob_id} at {path}: {exc}") from exc
        return digest.hexdigest()

    def _blob_metadata(self, blob_id: str) -> dict[str, Any]:
        result = self._blobs.get_blob_metadata(
            namespace=_LEDGER_NAMESPACE, blob_id=blob_id,
        )
        if not self._status_ok(result):
            raise BackfillError(
                f"get_blob_metadata failed for blob {blob_id}: {result!r}",
            )
        data = result.get("data") if isinstance(result, dict) else None
        metadata = data.get("metadata") if isinstance(data, dict) else None
        if not isinstance(metadata, dict):
            raise BackfillError(
                f"get_blob_metadata returned no metadata for blob {blob_id}: {result!r}",
            )
        return metadata

    def _blob_external_id(self, blob_id: str) -> str | None:
        value = self._blob_metadata(blob_id).get("external_id")
        return value if isinstance(value, str) else None

    def _find_keyed_twin(
        self, content_key: str, *, self_blob_id: str,
    ) -> str | None:
        """Return a blob id != self that already owns ``content_key``, or None.

        ``external_id`` is platform-wide unique, so at most one blob owns the
        key — either ``self`` (no twin) or exactly one other blob.
        """
        result = self._blobs.search_blobs(
            namespace=_LEDGER_NAMESPACE,
            metadata_filters={"external_id": content_key},
        )
        for blob_id in self._iter_search_blob_ids(result):
            if blob_id != self_blob_id:
                return blob_id
        return None

    def _verify_twin(self, twin_blob_id: str, expected_digest: str) -> None:
        """C1 guard: a key match must be backed by identical bytes + an export kind."""
        metadata = self._blob_metadata(twin_blob_id)
        kind = self._plugin_metadata_kind(metadata)
        if kind not in EXPORT_BLOB_KINDS:
            raise BackfillError(
                f"twin blob {twin_blob_id} is not an export blob (kind={kind!r})",
            )
        if self._hash_blob(twin_blob_id) != expected_digest:
            raise BackfillError(
                f"twin blob {twin_blob_id} shares the content key but its bytes "
                "differ (external_id collision masking different content)",
            )

    def _tag_blob_kind(self, blob_id: str, kind_tag: str) -> None:
        """Phase 0 read-merge-write: set ONLY plugin_metadata.kind, preserving the rest."""
        metadata = self._blob_metadata(blob_id)
        merged_plugin_metadata = dict(self._plugin_metadata(metadata))
        if merged_plugin_metadata.get("kind") == kind_tag:
            return
        merged_plugin_metadata["kind"] = kind_tag
        result = self._blobs.update_blob_metadata(
            namespace=_LEDGER_NAMESPACE,
            blob_id=blob_id,
            metadata={"plugin_metadata": merged_plugin_metadata},
        )
        if not self._status_ok(result):
            raise BackfillError(
                f"Phase 0: failed to tag blob {blob_id} kind={kind_tag}: {result!r}",
            )

    def _set_blob_external_id(self, blob_id: str, content_key: str) -> None:
        """Phase 1 (ii): set the content-digest external_id; fail loud on any error (C4)."""
        result = self._blobs.update_blob_metadata(
            namespace=_LEDGER_NAMESPACE,
            blob_id=blob_id,
            metadata={"external_id": content_key},
        )
        if not self._status_ok(result):
            raise BackfillError(
                f"Phase 1: failed to set external_id={content_key} on blob "
                f"{blob_id} (possible cross-namespace key conflict): {result!r}",
            )

    # ------------------------------------------------------------------
    # Envelope parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _status_ok(result: object) -> bool:
        """Envelope OK iff it EXPLICITLY reports a success ``action_status``.

        Fail-closed (KB "Critical Development Guidelines v2"): a missing / ``None`` ``action_status``
        is treated as FAILURE. The default blob provider always stamps
        ``completed`` / ``error``, but for the MUTATING ops (kind-tag, key-set,
        orphan delete) a malformed envelope from any provider must NOT be read as
        a completed mutation — that would leave the convergence silently
        incomplete (an unkeyed blob or an unreclaimed orphan) against fast-fail.
        """
        if not isinstance(result, dict):
            return False
        return result.get("action_status") in _OK_STATUSES

    @staticmethod
    def _iter_search_blob_ids(result: object) -> list[str]:
        if not isinstance(result, dict):
            return []
        data = result.get("data")
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            return []
        out: list[str] = []
        for item in files:
            if isinstance(item, dict):
                blob_id = item.get("blob_id") or item.get("id")
                if isinstance(blob_id, str) and blob_id:
                    out.append(blob_id)
        return out

    @staticmethod
    def _plugin_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        raw = metadata.get("plugin_metadata")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @classmethod
    def _plugin_metadata_kind(cls, metadata: dict[str, Any]) -> str | None:
        value = cls._plugin_metadata(metadata).get("kind")
        return value if isinstance(value, str) else None
