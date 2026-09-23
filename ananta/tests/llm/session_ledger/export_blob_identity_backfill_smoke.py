#!/usr/bin/env python3
"""Smoke for ExportBlobIdentityBackfill (A3 content-digest convergence; no pytest).

Drives the full backfill against a faithful in-memory blob store + a fake
repository, with REAL bytes on disk so the sha256 content digest is computed for
real. The fixture reproduces the historical duplicate-export footgun:

* ``S1 → B1`` and ``S2 → B2`` are two ``chatgpt_export`` sources whose blobs hold
  the SAME bytes (uploaded twice / re-ingested after a rename) — each with a
  filename-derived ``external_id``, neither content-keyed.
* ``S3 → B3`` is a ``claude_ai_export`` source with DISTINCT bytes and no twin —
  exercises the key-only path (no repoint, no orphan).

Asserted behaviours:

1. ``confirm=False`` mutates nothing and reports the pending-work counts.
2. ``confirm=True`` (run #1) keys B1 + B3, finds B1 as B2's content twin,
   verifies it (export kind + identical bytes), repoints S2 → B1, then sweeps the
   now-unreferenced B2.
3. ``confirm=True`` (run #2) is convergent + idempotent: every source skips, no
   blob is keyed/repointed, no orphan remains.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/export_blob_identity_backfill_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.shared import SourceRow  # noqa: E402
from ananta.llm.session_ledger.types import IngestSourceKind  # noqa: E402
from ananta.services.session_ledger_service.blob_identity_backfill import (  # noqa: E402
    EXPORT_BLOB_KINDS,
    BackfillError,
    ExportBlobIdentityBackfill,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


_NAMESPACE = "session_ledger"


class _FakeBlobStore:
    """In-memory blob store mirroring the blob_storage_service envelope shapes.

    ``update_blob_metadata`` does a TOP-LEVEL shallow merge (the real provider's
    contract): the backfill passes either ``{"external_id": ...}`` or a wholesale
    ``{"plugin_metadata": {...}}`` it already read-merged, so a top-level update is
    faithful. ``search_blobs`` honours the two filter shapes the backfill uses:
    exact ``external_id`` and dotted ``plugin_metadata.kind`` list-membership.
    """

    def __init__(self) -> None:
        # blob_id -> {"path": str, "metadata": {"external_id":..., "plugin_metadata": {...}}}
        self.blobs: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    def add_blob(self, blob_id: str, *, path: str, external_id: str) -> None:
        self.blobs[blob_id] = {
            "path": path,
            "metadata": {"external_id": external_id, "plugin_metadata": {}},
        }

    # --- primitives the backfill calls -------------------------------------

    def resolve_blob_path(self, uri: str) -> str:
        blob_id = uri.removeprefix("blob://")
        return self.blobs[blob_id]["path"]

    def get_blob_metadata(self, *, namespace: str, blob_id: str) -> dict[str, Any]:
        assert namespace == _NAMESPACE
        meta = self.blobs[blob_id]["metadata"]
        # Return a deep-ish copy so callers can't mutate our store by reference.
        return {
            "action_status": "completed",
            "data": {
                "metadata": {
                    "external_id": meta.get("external_id"),
                    "plugin_metadata": dict(meta.get("plugin_metadata") or {}),
                },
            },
        }

    def update_blob_metadata(
        self, *, namespace: str, blob_id: str, metadata: dict[str, Any],
    ) -> dict[str, Any]:
        assert namespace == _NAMESPACE
        self.blobs[blob_id]["metadata"].update(metadata)
        return {"action_status": "completed"}

    def search_blobs(
        self, *, namespace: str, metadata_filters: dict[str, Any],
    ) -> dict[str, Any]:
        assert namespace == _NAMESPACE
        matched: list[dict[str, str]] = []
        for blob_id, rec in self.blobs.items():
            if self._matches(rec["metadata"], metadata_filters):
                matched.append({"blob_id": blob_id})
        return {"action_status": "completed", "data": {"files": matched}}

    def delete_blob(self, *, namespace: str, blob_id: str) -> dict[str, Any]:
        assert namespace == _NAMESPACE
        del self.blobs[blob_id]
        self.deleted.append(blob_id)
        return {"action_status": "completed"}

    # --- filter matching ---------------------------------------------------

    @staticmethod
    def _matches(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
        for key, want in filters.items():
            if key == "external_id":
                if metadata.get("external_id") != want:
                    return False
            elif key == "plugin_metadata.kind":
                kind = (metadata.get("plugin_metadata") or {}).get("kind")
                if kind not in want:
                    return False
            else:  # pragma: no cover - the backfill uses only the two above
                raise AssertionError(f"unexpected metadata filter {key!r}")
        return True


class _FakeRepo:
    """Minimal repository surface the backfill touches: list + repoint."""

    def __init__(self, sources: list[SourceRow]) -> None:
        self._sources = list(sources)

    def list_sources(self, *, enabled_only: bool = False) -> list[SourceRow]:
        del enabled_only  # backfill always passes enabled_only=False
        return list(self._sources)

    def repoint_source_root_uri(self, source_id: str, new_root_uri: str) -> bool:
        for i, row in enumerate(self._sources):
            if row.id == source_id:
                # SourceRow is frozen; rebuild with the new root_uri.
                self._sources[i] = SourceRow(
                    id=row.id,
                    source_kind=row.source_kind,
                    root_uri=new_root_uri,
                    account_label=row.account_label,
                    enabled=row.enabled,
                    config_json=row.config_json,
                )
                return True
        return False

    def root_uri_of(self, source_id: str) -> str:
        return next(r.root_uri for r in self._sources if r.id == source_id)


def _src(source_id: str, kind: IngestSourceKind, blob_id: str) -> SourceRow:
    return SourceRow(
        id=source_id,
        source_kind=kind,
        root_uri=blob_id,
        account_label=None,
        enabled=True,
        config_json={},
    )


def _write(root: Path, name: str, payload: bytes) -> str:
    path = root / name
    path.write_bytes(payload)
    return str(path)


def _build(root: Path) -> tuple[ExportBlobIdentityBackfill, _FakeBlobStore, _FakeRepo]:
    alpha = b"EXPORT_ALPHA_BYTES" * 64  # duplicated across B1 + B2
    beta = b"EXPORT_BETA_BYTES" * 64    # distinct, single source
    store = _FakeBlobStore()
    store.add_blob("B1", path=_write(root, "b1.zip", alpha), external_id="b1.zip")
    store.add_blob("B2", path=_write(root, "b2.zip", alpha), external_id="b2.zip")
    store.add_blob("B3", path=_write(root, "b3.zip", beta), external_id="b3.zip")
    repo = _FakeRepo(
        [
            _src("S1", IngestSourceKind.CHATGPT_EXPORT, "B1"),
            _src("S2", IngestSourceKind.CHATGPT_EXPORT, "B2"),
            _src("S3", IngestSourceKind.CLAUDE_AI_EXPORT, "B3"),
        ],
    )
    backfill = ExportBlobIdentityBackfill(
        blob_storage_service=store,
        repository=repo,  # type: ignore[arg-type]
    )
    return backfill, store, repo


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_dry_run_mutates_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        backfill, store, repo = _build(Path(tmp))
        before = {bid: dict(rec["metadata"]) for bid, rec in store.blobs.items()}
        result = backfill.run(confirm=False)
        _check(result["confirmed"] is False, "dry-run reports confirmed=False")
        _check(result["sources_scanned"] == 3, "dry-run scanned all 3 export sources")
        _check(
            result["blobs_needing_key"] == 3,
            f"dry-run: 3 blobs lack a content key (got {result['blobs_needing_key']})",
        )
        _check(
            result["sources_needing_repoint"] == 0,
            "dry-run: no twin visible until a confirmed run keys the first blob",
        )
        _check(
            store.deleted == []
            and all(store.blobs[b]["metadata"] == before[b] for b in before),
            "dry-run mutated no blob metadata and deleted nothing",
        )
        _check(
            repo.root_uri_of("S2") == "B2",
            "dry-run left S2 pointed at its original blob B2",
        )


def test_confirmed_run_converges_then_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        backfill, store, repo = _build(Path(tmp))

        # ── Run #1: key + repoint + sweep ──────────────────────────────────
        r1 = backfill.run(confirm=True)
        _check(r1["confirmed"] is True, "run#1 confirmed=True")
        _check(r1["sources_scanned"] == 3, "run#1 scanned 3 sources")
        _check(
            r1["blobs_keyed"] == 2,
            f"run#1 keyed B1 + B3 (got {r1['blobs_keyed']})",
        )
        _check(
            r1["sources_repointed"] == 1,
            f"run#1 repointed S2 onto its twin (got {r1['sources_repointed']})",
        )
        _check(
            r1["export_blobs_deleted"] == 1,
            f"run#1 swept the orphaned B2 (got {r1['export_blobs_deleted']})",
        )
        _check(r1["error_count"] == 0, "run#1 zero errors")
        _check(repo.root_uri_of("S2") == "B1", "run#1 S2 now points at twin B1")
        _check("B2" not in store.blobs and store.deleted == ["B2"], "run#1 B2 deleted")
        b1_key = store.blobs["B1"]["metadata"]["external_id"]
        _check(
            b1_key.startswith("session-ledger-export-sha256-"),
            f"run#1 B1 carries content-digest external_id (got {b1_key!r})",
        )
        _check(
            store.blobs["B1"]["metadata"]["plugin_metadata"]["kind"] in EXPORT_BLOB_KINDS,
            "run#1 B1 carries the durable export kind tag",
        )

        # ── Run #2: convergent, idempotent ─────────────────────────────────
        deleted_before = list(store.deleted)
        r2 = backfill.run(confirm=True)
        _check(
            r2["blobs_keyed"] == 0 and r2["sources_repointed"] == 0,
            "run#2 keys/repoints nothing (already converged)",
        )
        _check(
            r2["export_blobs_deleted"] == 0,
            "run#2 finds no orphan to sweep",
        )
        _check(
            r2["skipped"] == 3,
            f"run#2 skips all 3 already-keyed sources (got {r2['skipped']})",
        )
        _check(
            store.deleted == deleted_before,
            "run#2 deleted nothing further",
        )


class _StatuslessMutationBlobStore(_FakeBlobStore):
    """A blob store whose mutating envelope OMITS action_status (malformed).

    Exercises the Architect-M2 fail-closed hardening: a status-less mutation
    envelope must be treated as FAILURE, not a silent success.
    """

    def update_blob_metadata(
        self, *, namespace: str, blob_id: str, metadata: dict[str, Any],
    ) -> dict[str, Any]:
        assert namespace == _NAMESPACE
        self.blobs[blob_id]["metadata"].update(metadata)
        return {"data": {}}  # no action_status — must NOT read as success


def test_mutation_without_action_status_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _StatuslessMutationBlobStore()
        store.add_blob(
            "B1",
            path=_write(Path(tmp), "m1.zip", b"EXPORT_ALPHA_BYTES" * 64),
            external_id="m1.zip",
        )
        backfill = ExportBlobIdentityBackfill(
            blob_storage_service=store,
            repository=_FakeRepo([_src("S1", IngestSourceKind.CHATGPT_EXPORT, "B1")]),  # type: ignore[arg-type]
        )
        try:
            backfill.run(confirm=True)
        except BackfillError:
            _check(True, "status-less mutation envelope raises BackfillError (fail-closed)")
            return
        _check(False, "expected BackfillError on a status-less mutation envelope")


def main() -> int:
    print("=== export_blob_identity_backfill_smoke ===")
    test_dry_run_mutates_nothing()
    test_confirmed_run_converges_then_is_idempotent()
    test_mutation_without_action_status_fails_closed()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
