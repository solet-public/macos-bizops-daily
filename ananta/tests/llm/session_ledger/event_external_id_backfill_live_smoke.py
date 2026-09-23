#!/usr/bin/env python3
"""Live-Postgres round-trip smoke for the GAP-5 slice-2 ``external_id`` backfill.

Proves the backfill REPRODUCES the live importer's ``external_id`` EXACTLY,
against the REAL standardized ledger DDL: for each event, live-derive its
``external_id`` (``importer._event_external_id``) → append it → NULL it (simulate
a legacy pre-slice-1 row) → run ``backfill_event_external_ids`` → assert the
re-stamped id == the original live-derived value. If they match, a historical
re-ingest dedups.

Cases:
1. INLINE content — backfill derives over the stored ``content_text``.
2. OFFLOADED content — stored ``content_text`` is NULL; the backfill FETCHES the
   blob (stub fetcher) to recompute the SAME content-addressed key.
3. A multi-event tied-``(event_type, role, content_key, event_at)`` group — the
   ordinal ranking by ``sequence`` reproduces the live occurrence-index.
4. A VENDOR-present event — stamped with ``vendor_event_id`` verbatim, no ordinal.
5. IDEMPOTENCY — a second backfill pass stamps nothing (``events_stamped`` 0).
6. LIVE-WINDOW collision — a legacy null row whose derived id already belongs to
   a live row is SKIPPED + counted (``collisions_skipped``), stays NULL.
7. REAL blob round-trip — ``store_event_text`` → ``fetch_event_text`` through the
   ACTUAL local ``FilesystemProvider`` (not the stub): proves the real
   ``retrieve_blob`` envelope (``data["content"]`` = ``content.hex()``) decodes
   byte-exact, so the offloaded backfill key matches a live re-ingest's. Local
   solets use this filesystem provider; S3 is AWS-only.

Reuses ``_LiveStateAdapter`` / ``_make_event`` / ``_load_pg_config`` / ``_row``
from ``ingest_migration_live_smoke``. Env-gated behind ``LEDGER_BACKFILL_LIVE_SMOKE=1``.

    LEDGER_BACKFILL_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/event_external_id_backfill_live_smoke.py
"""

from __future__ import annotations

import os
import secrets
import string
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "default_blob_storage_plugin" / "src"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.llm.session_ledger.blob_adapter import (  # noqa: E402
    SessionLedgerBlobAdapter,
)
from ananta.llm.session_ledger.importer import _event_external_id  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    NAMESPACE,
    get_session_ledger_schema,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    IngestSourceKind,
    MessageRole,
    SourceVendor,
)
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from ingest_migration_live_smoke import (  # noqa: E402
    _LiveStateAdapter,
    _load_pg_config,
    _make_event,
    _row,
)
from postgres_state_management_plugin.postgres_backend.ddl_renderer import (  # noqa: E402
    emit_create_table_ops,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)

from default_blob_storage_plugin.constants import PLUGIN_NAME as _BLOB_NS  # noqa: E402
from default_blob_storage_plugin.providers.filesystem_provider import (  # noqa: E402
    FilesystemProvider,
)
from default_blob_storage_plugin.schema import get_blob_storage_schema  # noqa: E402

_passed = 0
_failed: list[str] = []

_NOW = datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)
# Non-round time WITH microseconds — real event_at carries both, and the backfill
# round-trips event_at through the state read path's ISO-string serialization
# (_parse_event_at → fromisoformat → _canonical_event_at). A midnight/no-micros
# value would be the friendliest possible case; this exercises the real shape.
_EVT = datetime(2026, 6, 1, 12, 34, 56, 789012, tzinfo=UTC)
_EVENT_TABLE = f"{NAMESPACE}__event"


class _IdGeneratingStateAdapter(_LiveStateAdapter):
    """``_LiveStateAdapter`` + the system columns the real state service injects.

    The real StateManagementInterface generates a row ``id`` from the schema's
    ``id_prefix`` and stamps the standard ``namespace`` column; the bare provider
    ``insert`` the test adapter delegates to does not. The ledger smokes never hit
    this (their records set ``id`` + ``namespace`` explicitly), but the blob
    plugin's ``prepare_metadata_for_storage`` relies on the state service — so
    inject both when absent.
    """

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        record = dict(cast("dict[str, Any]", data.get("record") or {}))
        if record.get("id") is None:
            # blob_id format: ``bmd-<12 base36 chars>`` (what the real state
            # service generates; validate_blob_id_format enforces it on retrieve).
            suffix = "".join(secrets.choice(string.digits + string.ascii_lowercase) for _ in range(12))
            record["id"] = f"bmd-{suffix}"
        if record.get("namespace") is None:
            record["namespace"] = namespace
        return super().write_state(namespace, {**data, "record": record})

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        # The blob plugin's metadata lookup uses read_state (single-namespace
        # equality read) — same ``{records: [...]}`` envelope as query_state.
        return self.query_state(namespace, query)


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _create_schema_tables(provider: PostgresProvider) -> None:
    """Build the ledger tables AND the blob-storage metadata table (the real
    ``FilesystemProvider`` round-trip needs the latter) in the sandbox schema."""
    schema_name = provider.config.schema_name
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        for schema_def, ns in (
            (get_session_ledger_schema(), NAMESPACE),
            (get_blob_storage_schema(), _BLOB_NS),
        ):
            std = SchemaStandardizer().standardize_schema(schema_def)
            ops = [
                op
                for table in std.tables.values()
                for op in emit_create_table_ops(ns, table, schema_name)
            ]
            provider.apply_schema_change_ops(cur, std, ops)


def _null_external_ids(provider: PostgresProvider, schema_name: str, session_id: str) -> None:
    """Simulate legacy pre-slice-1 rows: clear external_id for the session."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'UPDATE "{schema_name}"."{_EVENT_TABLE}" SET external_id = NULL '
                "WHERE session_id = %s",
            ),
            (session_id,),
        )


def _ev(text: str, *, vendor_event_id: str | None = None) -> Any:
    return _make_event(
        external_session_id="ext-bf",
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text=text,
        content_json=None,
        event_at=_EVT,
        vendor_event_id=vendor_event_id,
    )


def _setup_session(repo: SessionLedgerRepository, *, external_session_id: str) -> tuple[str, str]:
    source_id = repo.insert_source(
        source_kind=IngestSourceKind.CODEX_PUSHED,
        root_uri=f"pushed:{external_session_id}",
        account_label="bf",
        config={},
    )
    session_id = repo.upsert_session(
        source_id=source_id,
        external_session_id=external_session_id,
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CODEX_PUSHED,
        vendor_session_label="lbl",
        project_path=None,
        first_event_at=_EVT,
        last_event_at=_EVT,
    )
    batch_id = repo.start_batch(source_id, polling_lease_token=f"plt_{external_session_id}")
    return session_id, batch_id


def _append_live(
    repo: SessionLedgerRepository,
    session_id: str,
    batch_id: str,
    normalized: Any,
    ordinals: dict[Any, int],
    *,
    content_blob_id: str | None = None,
) -> str:
    """Append an event with the LIVE-derived external_id; return that id."""
    external_id = _event_external_id(normalized=normalized, session_id=session_id, ordinals=ordinals)
    repo.append_event(
        session_id=session_id,
        external_id=external_id,
        normalized=normalized,
        batch_id=batch_id,
        content_blob_id=content_blob_id,
        session_vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CODEX_PUSHED,
    )
    return external_id


def test_backfill_reproduces_live_derivation(provider: PostgresProvider, schema_name: str) -> None:
    repo = SessionLedgerRepository(state_service=cast("Any", _LiveStateAdapter(provider)), clock=lambda: _NOW)
    session_id, batch_id = _setup_session(repo, external_session_id="ext-bf")

    oversized = "z" * 100_000  # offloaded
    blob_texts = {"bmd_bf_D": oversized}

    ordinals: dict[Any, int] = {}
    # Append 6 events with their LIVE-derived external_ids (one source stream).
    live_ids: dict[str, str] = {}
    live_ids["A"] = _append_live(repo, session_id, batch_id, _ev("alpha"), ordinals)
    live_ids["B"] = _append_live(repo, session_id, batch_id, _ev("beta"), ordinals)
    live_ids["C1"] = _append_live(repo, session_id, batch_id, _ev("same"), ordinals)
    live_ids["C2"] = _append_live(repo, session_id, batch_id, _ev("same"), ordinals)
    live_ids["D"] = _append_live(
        repo, session_id, batch_id, _ev(oversized), ordinals, content_blob_id="bmd_bf_D",
    )
    live_ids["E"] = _append_live(repo, session_id, batch_id, _ev("ev", vendor_event_id="vev-E"), ordinals)

    # Record each event row's id (in append order = sequence order).
    rows = repo._read_session_events(session_id)  # noqa: SLF001
    ev_ids = [str(r["id"]) for r in rows]
    _check(len(ev_ids) == 6, f"6 events appended (got {len(ev_ids)})")
    _check(live_ids["C1"] != live_ids["C2"], "tied-tuple events C1/C2 got DISTINCT ids (ordinals 0,1)")
    _check(live_ids["E"] == "vev-E", "vendor-present event E derives its vendor_event_id verbatim")

    # Simulate legacy: NULL every external_id, then backfill.
    _null_external_ids(provider, schema_name, session_id)
    result = repo.backfill_event_external_ids(fetch_blob_text=lambda bid: blob_texts[bid])
    _check(result["events_stamped"] == 6, f"backfill stamped all 6 rows (got {result['events_stamped']})")
    _check(result["collisions_skipped"] == 0, "no collisions on the clean session")

    # Each row's re-stamped external_id == the original live-derived value.
    label_by_id = {  # event id (append order) → its logical label
        ev_ids[i]: lbl for i, lbl in enumerate(["A", "B", "C1", "C2", "D", "E"])
    }
    all_match = True
    for ev_id, lbl in label_by_id.items():
        stamped = _row(provider, "event", ev_id)["external_id"]
        if stamped != live_ids[lbl]:
            all_match = False
            print(f"    MISMATCH {lbl}: backfilled {stamped!r} != live {live_ids[lbl]!r}")
    _check(all_match, "EVERY backfilled external_id == the live-derived id (inline + offloaded + tied + vendor)")

    # Idempotency: a second pass stamps nothing.
    rerun = repo.backfill_event_external_ids(fetch_blob_text=lambda bid: blob_texts[bid])
    _check(rerun["events_stamped"] == 0, "re-run stamps 0 (idempotent — fill-only)")


def test_window_dup_skip_and_count(provider: PostgresProvider, schema_name: str) -> None:
    repo = SessionLedgerRepository(state_service=cast("Any", _LiveStateAdapter(provider)), clock=lambda: _NOW)
    session_id, batch_id = _setup_session(repo, external_session_id="ext-bf-win")

    # F = the LIVE row (re-ingested post-deploy, owns derv(windowed, ord 0)),
    # stays NON-NULL. G = the LEGACY duplicate of the SAME event, external_id
    # nulled below. ``_append_live`` returns the external_id; the event ids come
    # from the rows in sequence order ([F, G]).
    f_eid = _append_live(repo, session_id, batch_id, _ev("windowed"), {})
    repo.append_event(
        session_id=session_id, external_id="placeholder_G", normalized=_ev("windowed"),
        batch_id=batch_id, content_blob_id=None,
        session_vendor=SourceVendor.CLAUDE_CODE, source_kind=IngestSourceKind.CODEX_PUSHED,
    )
    rows = repo._read_session_events(session_id)  # noqa: SLF001 — ordered by sequence: [F, G]
    f_id, g_id = str(rows[0]["id"]), str(rows[1]["id"])

    # NULL only G (F stays non-null = the live row owning derv(windowed, ord 0)).
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'UPDATE "{schema_name}"."{_EVENT_TABLE}" SET external_id = NULL WHERE id = %s',
            ),
            (g_id,),
        )

    result = repo.backfill_event_external_ids(fetch_blob_text=lambda _bid: "")
    _check(result["collisions_skipped"] == 1, f"the window-dup is SKIPPED + counted (got {result['collisions_skipped']})")
    _check(result["events_stamped"] == 0, "no row stamped — the only null row collided")
    _check(_row(provider, "event", g_id)["external_id"] is None, "the collided legacy row STAYS NULL (operator disposes before 2b)")
    _check(
        _row(provider, "event", f_id)["external_id"] == f_eid,
        "the live row F is untouched (still owns the derived id)",
    )


def test_real_blob_fetch_round_trip(provider: PostgresProvider) -> None:
    """REAL ``store_event_text`` → ``fetch_event_text`` via the local FilesystemProvider.

    The offloaded derivation cases above use a STUB fetcher; this runs the ACTUAL
    blob path — ``store_event_text`` writes the payload to the filesystem +
    metadata to Postgres, ``fetch_event_text`` does the real ``retrieve_blob`` →
    ``data["content"]`` (``content.hex()``) → decode. Proves the real envelope
    shape (no actr-style nesting surprise) end-to-end: an oversized UTF-8 payload
    round-trips byte-exact. Local solets use this filesystem provider; S3 is
    AWS-only.
    """
    with tempfile.TemporaryDirectory() as app_home:
        fs = FilesystemProvider(app_home=app_home, config={})
        fs.set_state_service(cast("Any", _IdGeneratingStateAdapter(provider)))
        adapter = SessionLedgerBlobAdapter(cast("Any", fs))
        payload = "ünïcode tail — 🌍 " + ("q" * 80_000)  # offloaded, multi-byte
        blob_id = adapter.store_event_text(
            content_text=payload,
            session_id="les_rt",
            external_session_id="ext-rt",
            sequence=-1,
        )
        got = adapter.fetch_event_text(blob_id)
        _check(
            got == payload,
            f"REAL store_event_text → fetch_event_text round-trips byte-exact via "
            f"the local FilesystemProvider (len {len(got)}, real retrieve_blob envelope)",
        )


def main() -> int:
    if os.environ.get("LEDGER_BACKFILL_LIVE_SMOKE") != "1":
        print("=== event_external_id_backfill_live_smoke ===")
        print(
            "  SKIP  set LEDGER_BACKFILL_LIVE_SMOKE=1 to run; "
            "needs the live solet DB."
        )
        return 0
    print("=== event_external_id_backfill_live_smoke ===")
    schema_name = f"example_test_bf_{secrets.token_hex(3)}"
    config = _load_pg_config()
    config.pg_schema = schema_name
    provider = PostgresProvider(config)
    provider.initialize()
    try:
        _create_schema_tables(provider)
        test_backfill_reproduces_live_derivation(provider, schema_name)
        test_window_dup_skip_and_count(provider, schema_name)
        test_real_blob_fetch_round_trip(provider)
    finally:
        with provider.get_transactional_connection() as conn, conn.cursor() as cur:
            cur.execute(cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
