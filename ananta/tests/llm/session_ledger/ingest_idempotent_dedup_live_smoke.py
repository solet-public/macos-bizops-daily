#!/usr/bin/env python3
"""Live-Postgres dedup smoke for the GAP-5 idempotent-ingest ``append_event`` upsert.

Proves the slice-1 behavior against the REAL standardized ledger DDL — including
the new ``idx_event_session_external_unique (session_id, external_id)`` index
created from the SchemaDefinition — in a THROWAWAY pg-schema:

1. A fresh ``external_id`` INSERTS: ``deduped=False``, sequence 1, one event row,
   ``session.event_count == 1``.
2. RE-INGEST of the SAME ``(session_id, external_id)`` DEDUPS via the
   ``ON CONFLICT … DO NOTHING`` upsert: ``deduped=True``, STILL one row,
   ``event_count`` UNCHANGED (the footgun-A decouple — counters gated on insert).
3. A DISTINCT ``external_id`` INSERTS again: ``deduped=False``, sequence 2 (the
   ``MAX(sequence)+1`` allocator), two rows, ``event_count == 2``.
4. END-TO-END SEAM: the importer's own derivation (``_event_external_id``,
   footgun B) PRODUCES the ``external_id`` and the real ON CONFLICT (footgun A)
   dedups it — two independent passes derive the SAME ``derv:`` id and the
   second adds no row. Closes the gap between the isolated derivation unit smoke
   and this dedup smoke (which otherwise only proves dedup on a HANDED id).

Reuses the ``_LiveStateAdapter`` (full StateManagementInterface stand-in over a
real ``PostgresProvider``, incl. the new ``max_value`` delegation) + ``_make_event``
from ``ingest_migration_live_smoke``. Env-gated behind ``LEDGER_DEDUP_LIVE_SMOKE=1``.

    LEDGER_DEDUP_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/ingest_idempotent_dedup_live_smoke.py
"""

from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.llm.session_ledger.importer import (  # noqa: E402
    _event_external_id,
    _OrdinalCounter,
)
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

_passed = 0
_failed: list[str] = []

_NOW = datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)
_EVT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_EVENT_TABLE = f"{NAMESPACE}__event"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _create_schema_tables(provider: PostgresProvider) -> None:
    """Build the real ledger tables (incl. the new unique index) in the sandbox
    via the PRODUCTION DDL renderer — the same path schema adoption drives."""
    schema = SchemaStandardizer().standardize_schema(get_session_ledger_schema())
    schema_name = provider.config.schema_name
    ops = [
        op
        for table in schema.tables.values()
        for op in emit_create_table_ops(NAMESPACE, table, schema_name)
    ]
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        provider.apply_schema_change_ops(cur, schema, ops)


def _count_events(provider: PostgresProvider, schema_name: str) -> int:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'SELECT count(*) AS n FROM "{schema_name}"."{_EVENT_TABLE}"'))
        row = cur.fetchone()
        return int(cast("dict[str, Any]", row)["n"]) if row else 0


def _msg(text: str) -> Any:
    return _make_event(
        external_session_id="ext-dedup",
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text=text,
        content_json=None,
        event_at=_EVT,
        vendor_event_id=None,
    )


def _append(
    repo: SessionLedgerRepository,
    session_id: str,
    batch_id: str,
    *,
    external_id: str,
    text: str,
    content_blob_id: str | None = None,
) -> Any:
    return repo.append_event(
        session_id=session_id,
        external_id=external_id,
        normalized=_msg(text),
        batch_id=batch_id,
        content_blob_id=content_blob_id,
        session_vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CODEX_PUSHED,
    )


def test_dedup(provider: PostgresProvider, schema_name: str) -> None:
    repo = SessionLedgerRepository(
        state_service=cast("Any", _LiveStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    source_id = repo.insert_source(
        source_kind=IngestSourceKind.CODEX_PUSHED,
        root_uri=f"pushed:{schema_name}",
        account_label="dedup",
        config={},
    )
    session_id = repo.upsert_session(
        source_id=source_id,
        external_session_id="ext-dedup",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CODEX_PUSHED,
        vendor_session_label="lbl",
        project_path=None,
        first_event_at=_EVT,
        last_event_at=_EVT,
    )
    batch_id = repo.start_batch(source_id, polling_lease_token="plt_dedup")

    def _ecount() -> int:
        return int(_row(provider, "session", session_id)["event_count"])

    # 1. fresh external_id INSERTS.
    r1 = _append(repo, session_id, batch_id, external_id="ext_X", text="first")
    _check(r1.deduped is False, "first append (ext_X) inserts (deduped=False)")
    _check(r1.sequence == 1, f"first event sequence == 1 — MAX(sequence)+1 (got {r1.sequence})")
    _check(_count_events(provider, schema_name) == 1, "one event row after the first insert")
    _check(_ecount() == 1, "session.event_count == 1")

    # 2. RE-INGEST the SAME (session_id, external_id) → ON CONFLICT DO NOTHING.
    r2 = _append(repo, session_id, batch_id, external_id="ext_X", text="first")
    _check(r2.deduped is True, "re-ingest of ext_X DEDUPS (deduped=True) via ON CONFLICT DO NOTHING")
    _check(_count_events(provider, schema_name) == 1, "STILL one event row — no duplicate inserted")
    _check(_ecount() == 1, "session.event_count UNCHANGED — counters gated on inserted (footgun A)")

    # 3. a DISTINCT external_id INSERTS again.
    r3 = _append(repo, session_id, batch_id, external_id="ext_Y", text="second")
    _check(r3.deduped is False, "distinct ext_Y inserts (deduped=False)")
    _check(r3.sequence == 2, f"second distinct event sequence == 2 (got {r3.sequence})")
    _check(_count_events(provider, schema_name) == 2, "two event rows after the distinct insert")
    _check(_ecount() == 2, "session.event_count == 2")

    # 4-5. END-TO-END SEAM (footgun B → footgun A): the importer's derivation
    # (``_event_external_id``) PRODUCES the external_id; the real ON CONFLICT
    # dedups it. Two independent passes (fresh ordinals each, as two separate
    # poll passes over the same source event) must derive the SAME ``derv:`` id,
    # and the second append must dedup against the real unique index — proving
    # the full idempotency chain end-to-end, not by composition of two isolated
    # half-proofs. ``vendor_event_id`` is None on ``_msg`` so the hash path runs.
    derive_ev = _msg("derived-path")
    ordinals_a: _OrdinalCounter = {}
    ordinals_b: _OrdinalCounter = {}
    eid_a = _event_external_id(normalized=derive_ev, session_id=session_id, ordinals=ordinals_a)
    eid_b = _event_external_id(normalized=derive_ev, session_id=session_id, ordinals=ordinals_b)
    _check(
        eid_a == eid_b and eid_a.startswith("derv:"),
        f"importer derives the SAME derv: external_id across two independent passes (got {eid_a!r})",
    )
    d1 = _append(repo, session_id, batch_id, external_id=eid_a, text="derived-path")
    _check(d1.deduped is False, "first DERIVED-id append inserts (deduped=False)")
    rows_after_first = _count_events(provider, schema_name)
    d2 = _append(repo, session_id, batch_id, external_id=eid_b, text="derived-path")
    _check(d2.deduped is True, "re-DERIVED id DEDUPS via real ON CONFLICT (footgun B → footgun A seam)")
    _check(
        _count_events(provider, schema_name) == rows_after_first,
        "derived-path re-ingest adds NO row — full derive→dedup idempotency proven end-to-end",
    )

    # 6-7. OVERSIZED / OFFLOADED re-ingest dedups (slice-1 MAJOR fix, Reviewer-A).
    # A null-vendor oversized event is offloaded — content_blob_id is a RANDOM
    # state-generated pointer minted fresh on every (unconditional) re-offload.
    # The derivation is CONTENT-addressed (normalized.content_text, present at
    # derivation time), so the external_id is STABLE across re-ingest even though
    # the persisted blob pointer differs (bmd_off_A vs bmd_off_B) → the re-ingest
    # DEDUPS. Pre-fix this keyed on content_blob_id → diverged → silent duplicate.
    oversized = "z" * 100_000  # over the offload threshold
    big_ev = _msg(oversized)
    eid_big1 = _event_external_id(normalized=big_ev, session_id=session_id, ordinals={})
    eid_big2 = _event_external_id(normalized=big_ev, session_id=session_id, ordinals={})
    _check(
        eid_big1 == eid_big2,
        "oversized null-vendor event derives a STABLE id across re-ingest (content-addressed)",
    )
    o1 = _append(
        repo, session_id, batch_id, external_id=eid_big1, text=oversized, content_blob_id="bmd_off_A",
    )
    _check(o1.deduped is False, "first oversized/offloaded append inserts (blob pointer bmd_off_A)")
    rows_after_big = _count_events(provider, schema_name)
    o2 = _append(
        repo, session_id, batch_id, external_id=eid_big2, text=oversized, content_blob_id="bmd_off_B",
    )
    _check(
        o2.deduped is True,
        "re-ingest with a DIFFERENT blob pointer (bmd_off_B) DEDUPS — external_id is "
        "content-addressed, not keyed on the per-offload random blob_id",
    )
    _check(
        _count_events(provider, schema_name) == rows_after_big,
        "oversized re-ingest adds NO row — offloaded events dedup across re-offload",
    )


def main() -> int:
    if os.environ.get("LEDGER_DEDUP_LIVE_SMOKE") != "1":
        print("=== ingest_idempotent_dedup_live_smoke ===")
        print(
            "  SKIP  set LEDGER_DEDUP_LIVE_SMOKE=1 to run; "
            "needs the live solet DB."
        )
        return 0
    print("=== ingest_idempotent_dedup_live_smoke ===")
    schema_name = f"example_test_dedup_{secrets.token_hex(3)}"
    config = _load_pg_config()
    config.pg_schema = schema_name
    provider = PostgresProvider(config)
    provider.initialize()
    try:
        _create_schema_tables(provider)
        test_dedup(provider, schema_name)
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
