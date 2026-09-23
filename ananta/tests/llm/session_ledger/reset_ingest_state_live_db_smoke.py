#!/usr/bin/env python3
"""Live-Postgres convergence keystone for the GAP-5 slice-3 non-destructive reset.

``reset_ingest_state`` was reworked (slice 3) from a DESTRUCTIVE content-table
truncation into a NON-DESTRUCTIVE per-source cursor reset: it clears every
source's ``session_ledger__source_cursor`` rows so the next poll replays each
source and the live ``(session_id, external_id)`` upsert reconverges — no content
is deleted. This smoke proves that end-to-end against the REAL standardized ledger
DDL (incl. the slice-1 ``(session_id, external_id)`` unique index) in a THROWAWAY
pg-schema:

1. Seed a source + session + two events + two source cursors.
2. ``reset_ingest_state(confirm=False)`` dry-run reports the 2 cursors that would
   clear + the content it PRESERVES, and deletes NOTHING (cursors still present).
3. ``reset_ingest_state(confirm=True)`` clears BOTH cursors (``deleted_count==2``)
   and leaves the event + session rows UNTOUCHED — the non-destructive proof
   (the pre-slice-3 verb would have wiped them).
4. CONVERGENCE: re-appending the SAME ``external_id``s post-reset DEDUPS via the
   ON CONFLICT DO NOTHING upsert — no duplicate rows — so a replay after a reset
   reconverges instead of duplicating.

FIDELITY BOUNDARY: step 4 re-appends the same ``external_id``s directly to model
the next poll's replay; it is NOT a true importer re-poll (which would re-read the
source file and re-derive the ids). The dedup property exercised is identical —
the real ``append_event`` ON CONFLICT path — but the source-re-read is not in scope.

Runs in a disposable schema (``example_test_reset_<hex>``), so it NEVER touches
live solet data; env-gated behind ``LEDGER_RESET_LIVE_SMOKE=1``.

    LEDGER_RESET_LIVE_SMOKE=1 \\
      .venv/bin/python3 ananta/tests/llm/session_ledger/reset_ingest_state_live_db_smoke.py
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    NAMESPACE,
    get_session_ledger_schema,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    CursorScope,
    EventType,
    IngestSourceKind,
    MessageRole,
    SourceVendor,
)
from ananta.services.session_ledger_service.service import (  # noqa: E402
    SessionLedgerService,
)
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from ingest_migration_live_smoke import (  # noqa: E402
    _envelope,
    _LiveStateAdapter,
    _load_pg_config,
    _make_event,
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
_EXT_SESSION = "ext-reset"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _ResetStateAdapter(_LiveStateAdapter):
    """Adds the two primitives the reset verb needs that the base ingest adapter
    doesn't surface: ``delete_records`` (the cursor clear) + ``count`` (the
    dry-run preserved-content counts). Both delegate to the REAL provider."""

    def delete_records(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        deleted = self._provider.delete(
            namespace=namespace,
            table=str(data["table"]),
            conditions=cast("dict[str, Any]", data.get("filters") or {}),
            soft_delete=bool(data.get("soft_delete", True)),
        )
        return _envelope({"result": {"deleted": deleted}})

    def count(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        value = self._provider.aggregate(
            namespace=namespace,
            table=str(data["table"]),
            op="count",
            column=None,
            filters=cast("dict[str, Any]", data.get("filters") or {}),
        )
        return _envelope({"result": {"value": value}})


def _create_schema_tables(provider: PostgresProvider) -> None:
    """Build the real ledger tables (incl. the slice-1 unique index) in the
    sandbox via the PRODUCTION DDL renderer — the path schema adoption drives."""
    schema = SchemaStandardizer().standardize_schema(get_session_ledger_schema())
    schema_name = provider.config.schema_name
    ops = [
        op
        for table in schema.tables.values()
        for op in emit_create_table_ops(NAMESPACE, table, schema_name)
    ]
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        provider.apply_schema_change_ops(cur, schema, ops)


def _count_table(provider: PostgresProvider, schema_name: str, table: str) -> int:
    physical = f"{NAMESPACE}__{table}"
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'SELECT count(*) AS n FROM "{schema_name}"."{physical}"'))
        row = cur.fetchone()
    return int(cast("dict[str, Any]", row)["n"]) if row else 0


def _build_service(provider: PostgresProvider) -> SessionLedgerService:
    repo = SessionLedgerRepository(
        state_service=cast("Any", _ResetStateAdapter(provider)),
        clock=lambda: _NOW,
    )
    service = SessionLedgerService.__new__(SessionLedgerService)
    service._registry = None  # type: ignore[assignment]
    service._repository = repo  # type: ignore[assignment]
    service._secret_gate = None  # type: ignore[assignment]
    service._blob_adapter = None  # type: ignore[assignment]
    service._importer = None  # type: ignore[assignment]
    service._summary_writer = None  # type: ignore[assignment]
    service._operator_equivalent_check = None
    service._scheduling_service = None
    return service


def _msg(text: str) -> Any:
    return _make_event(
        external_session_id=_EXT_SESSION,
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text=text,
        content_json=None,
        event_at=_EVT,
        vendor_event_id=None,
    )


def _append(repo: SessionLedgerRepository, session_id: str, batch_id: str, *, external_id: str, text: str) -> Any:
    return repo.append_event(
        session_id=session_id,
        external_id=external_id,
        normalized=_msg(text),
        batch_id=batch_id,
        content_blob_id=None,
        session_vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CODEX_PUSHED,
    )


def test_reset_is_non_destructive_and_reconverges(
    provider: PostgresProvider, schema_name: str,
) -> None:
    service = _build_service(provider)
    repo = cast("SessionLedgerRepository", service._repository)  # type: ignore[attr-defined]

    # (1) Seed: source + session + two events + two source cursors.
    source_id = repo.insert_source(
        source_kind=IngestSourceKind.CODEX_PUSHED,
        root_uri=f"pushed:{schema_name}",
        account_label="reset",
        config={},
    )
    session_id = repo.upsert_session(
        source_id=source_id,
        external_session_id=_EXT_SESSION,
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CODEX_PUSHED,
        vendor_session_label="lbl",
        project_path=None,
        first_event_at=_EVT,
        last_event_at=_EVT,
    )
    batch_id = repo.start_batch(source_id, polling_lease_token="plt_reset")
    _append(repo, session_id, batch_id, external_id="ext_1", text="first")
    _append(repo, session_id, batch_id, external_id="ext_2", text="second")
    repo.write_cursor(
        source_id=source_id, scope=CursorScope.DISCOVERY, cursor_payload={"offset": 1},
    )
    repo.write_cursor(
        source_id=source_id, scope=CursorScope.EVENT_READ,
        cursor_payload={"offset": 5}, scope_key=_EXT_SESSION,
    )
    _check(_count_table(provider, schema_name, "event") == 2, "seed: two event rows")
    _check(repo.count_active_source_cursors(source_id) == 2, "seed: two active source cursors")

    # (2) Dry-run reports the cursors that WOULD clear + preserved content, and
    # deletes NOTHING.
    dry = service.reset_ingest_state(confirm=False)
    _check(dry["confirmed"] is False, f"dry-run confirmed=False (got {dry['confirmed']!r})")
    _check(dry["action"] == "cursor_reset_replay", "dry-run action=cursor_reset_replay")
    _check(
        dry["active_cursor_count_before"] == 2,
        f"dry-run reports 2 cursors that would clear (got {dry['active_cursor_count_before']!r})",
    )
    _check(dry["deleted_count"] == 0, f"dry-run deletes nothing (got {dry['deleted_count']!r})")
    preserved = {
        str(row["table"]): int(cast("int", row["rows_preserved"]))
        for row in cast("list[dict[str, object]]", dry["preserved_content"])
    }
    _check(
        preserved.get("session_ledger__event") == 2 and preserved.get("session_ledger__session") == 1,
        f"dry-run preserved_content reports 2 events + 1 session (got {preserved})",
    )
    _check(
        repo.count_active_source_cursors(source_id) == 2,
        "dry-run did NOT touch the cursors (still 2 active)",
    )

    # (3) Confirm: clears BOTH cursors; content rows UNTOUCHED (non-destructive).
    confirmed = service.reset_ingest_state(confirm=True)
    _check(confirmed["confirmed"] is True, f"confirm confirmed=True (got {confirmed['confirmed']!r})")
    _check(
        confirmed["deleted_count"] == 2,
        f"confirm clears both cursors (deleted_count=2, got {confirmed['deleted_count']!r})",
    )
    per_source = cast("list[dict[str, object]]", confirmed["per_source"])
    _check(
        len(per_source) == 1 and per_source[0]["deleted_count"] == 2,
        f"per-source breakdown: the one source cleared 2 cursors (got {per_source})",
    )
    _check(
        repo.count_active_source_cursors(source_id) == 0,
        "post-reset: source cursors cleared (0 active)",
    )
    _check(
        _count_table(provider, schema_name, "event") == 2,
        "post-reset: event rows UNTOUCHED (content preserved — the non-destructive proof)",
    )
    _check(
        _count_table(provider, schema_name, "session") == 1,
        "post-reset: session row UNTOUCHED (content preserved)",
    )

    # (4) CONVERGENCE: re-appending the SAME external_ids post-reset DEDUPS via
    # the ON CONFLICT DO NOTHING upsert (models the next poll's replay) — no
    # duplicate rows, so a reset + replay reconverges rather than duplicating.
    r1 = _append(repo, session_id, batch_id, external_id="ext_1", text="first")
    r2 = _append(repo, session_id, batch_id, external_id="ext_2", text="second")
    _check(
        r1.deduped is True and r2.deduped is True,
        f"post-reset replay of the same external_ids DEDUPS (got {r1.deduped!r}/{r2.deduped!r})",
    )
    _check(
        _count_table(provider, schema_name, "event") == 2,
        "post-reset replay adds NO row — reset + replay reconverges via the upsert",
    )


def test_reset_refuses_with_null_external_id_events(
    provider: PostgresProvider, schema_name: str,
) -> None:
    """The slice-3 dup-window guard, live: a legacy null-``external_id`` event
    (NULLs DISTINCT in the ``(session_id, external_id)`` unique) would be
    DUPLICATED by a post-reset re-walk, so ``confirm=True`` must REFUSE while any
    remain — before clearing any cursor."""
    service = _build_service(provider)
    repo = cast("SessionLedgerRepository", service._repository)  # type: ignore[attr-defined]

    source_id = repo.insert_source(
        source_kind=IngestSourceKind.CODEX_PUSHED,
        root_uri=f"pushed:{schema_name}",
        account_label="guard",
        config={},
    )
    session_id = repo.upsert_session(
        source_id=source_id,
        external_session_id=_EXT_SESSION,
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CODEX_PUSHED,
        vendor_session_label="lbl",
        project_path=None,
        first_event_at=_EVT,
        last_event_at=_EVT,
    )
    batch_id = repo.start_batch(source_id, polling_lease_token="plt_guard")
    repo.write_cursor(
        source_id=source_id, scope=CursorScope.DISCOVERY, cursor_payload={"offset": 1},
    )
    # Seed a real event, then NULL its external_id to model a legacy null-vendor
    # row (the importer always stamps a non-null external_id post-slice-1, so the
    # only way to a null is the legacy backlog — emulated here by a raw UPDATE).
    _append(repo, session_id, batch_id, external_id="ext_g1", text="legacy")
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString,
                 f'UPDATE "{schema_name}"."{NAMESPACE}__event" '
                 "SET external_id = NULL WHERE external_id = %s"),
            ("ext_g1",),
        )
    _check(
        repo.count_events_missing_external_id() == 1,
        "seed: one null-external_id event present (the legacy-row emulation)",
    )

    # Dry-run surfaces the precondition without mutating.
    dry = service.reset_ingest_state(confirm=False)
    _check(
        dry["null_external_id_count"] == 1 and dry["precondition_met"] is False,
        f"dry-run flags precondition NOT met (1 null) (got {dry['null_external_id_count']!r}/"
        f"{dry['precondition_met']!r})",
    )

    # confirm=True REFUSES (raises) before clearing any cursor.
    raised = False
    try:
        service.reset_ingest_state(confirm=True)
    except ValueError as exc:
        raised = True
        _check(
            "null external_id" in str(exc) and "backfill_event_external_ids" in str(exc),
            "guard ValueError names the null-external_id precondition + the backfill remedy",
        )
    _check(raised, "confirm=True REFUSES while a null-external_id event remains (live dup-window guard)")
    _check(
        repo.count_active_source_cursors(source_id) == 1,
        "guard refused BEFORE clearing the cursor (still 1 active)",
    )


def _run_in_fresh_schema(
    config: Any, test_fn: Callable[[PostgresProvider, str], None],
) -> None:
    schema_name = f"example_test_reset_{secrets.token_hex(3)}"
    config.pg_schema = schema_name
    provider = PostgresProvider(config)
    provider.initialize()
    try:
        _create_schema_tables(provider)
        test_fn(provider, schema_name)
    finally:
        with provider.get_transactional_connection() as conn, conn.cursor() as cur:
            cur.execute(cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))


def main() -> int:
    if os.environ.get("LEDGER_RESET_LIVE_SMOKE") != "1":
        print("=== reset_ingest_state_live_db_smoke ===")
        print(
            "  SKIP  set LEDGER_RESET_LIVE_SMOKE=1 to run; "
            "needs the live solet DB."
        )
        return 0
    print("=== reset_ingest_state_live_db_smoke ===")
    config = _load_pg_config()
    for test_fn in (
        test_reset_refuses_with_null_external_id_events,
        test_reset_is_non_destructive_and_reconverges,
    ):
        _run_in_fresh_schema(config, test_fn)

    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
