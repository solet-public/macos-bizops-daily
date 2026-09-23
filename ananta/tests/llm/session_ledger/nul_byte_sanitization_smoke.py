#!/usr/bin/env python3
"""NUL-byte sanitization smoke (operator ruling 2026-06-01, option B).

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/nul_byte_sanitization_smoke.py

Background: PostgreSQL TEXT columns reject embedded NUL bytes. Some
LLM session JSONL emitters (claude_code in particular) carry NULs in
tool output / system recap text. Pre-fix, a single NUL-bearing line
killed the whole claude_code re-walk after a fresh
``reset_ingest_state`` (0 sessions / 0 events landed; codex landed
96k events because most codex content routes through ``content_json``
(JSONB), which tolerates JSON Unicode-escape for NUL via ``json.dumps``).

Operator ratified option B: strip NULs at the shared store boundary
in ``SessionLedgerRepository``. This smoke verifies:

1. ``_strip_nuls`` helper passes None through; passes NUL-free strings
   through unchanged (no allocation); strips NULs when present.
2. ``append_event`` strips NULs from every TEXT-bound field
   (``content_text``, ``vendor_event_id``, ``vendor_parent_event_id``,
   ``actor_session_label``, ``actor_agent_instance_id``) before the
   INSERT params reach the state service.
3. ``append_event`` ALSO strips NULs from ``content_json`` recursively
   via ``_strip_nuls_in_json`` (Bug C amendment 2026-06-13: Postgres
   rejects ``\\u0000`` in JSONB with error 22P05).
4. ``upsert_session`` strips NULs from ``vendor_session_label``,
   ``project_path``, ``summary_text_seed``, the originator/recipient
   columns, and ``external_session_id`` (the session-row key seed).
5. ``persist_summary`` strips NULs from ``summary_text`` on both the
   ``__summary`` INSERT and the ``__session`` denormalize UPDATE.
6. ``mark_session_summary_text`` strips NULs from the sentinel-shaped
   ``summary_text`` (defensive — caller always passes a fixed string,
   but the seam is here for completeness).
7. ``finish_batch`` strips NULs from ``error_message`` / ``error_kind``.
8. ``insert_source`` strips NULs from ``root_uri`` / ``account_label``.
9. ``record_attachment`` strips NULs from ``mime_type`` / ``filename``.
10. Regression: the per-event-shape validator runs BEFORE the strip-aware
    INSERT — a structurally invalid event still raises ValueError.

The stub state-service records every executed SQL + params tuple;
each assertion inspects those tuples to confirm the params reaching
the store contain zero ``\\x00`` bytes (no Postgres needed; no
running solet needed).
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_ATTACHMENT,
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SOURCE,
    TABLE_SUMMARY,
)
from ananta.llm.session_ledger.shared import _strip_nuls  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    ImportBatchStatus,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    SourceVendor,
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


NUL = "\x00"


def _record_str_values_carry_nul(record: dict[str, object]) -> bool:
    """True if any string value of a typed-op ``record`` dict carries a NUL.

    The migrated writes (``append_event`` / ``insert_source`` /
    ``record_attachment``) pass a Python ``record`` dict to the ``write_state``
    primitive instead of a positional SQL param list, so the NUL assertions
    inspect the dict's string values directly.
    """
    return any(isinstance(v, str) and NUL in v for v in record.values())


def _json_has_nul(value: object) -> bool:
    """Recursively True if any string anywhere in a JSON-shaped value has a NUL."""
    if isinstance(value, str):
        return NUL in value
    if isinstance(value, dict):
        return any(_json_has_nul(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_json_has_nul(item) for item in value)
    return False


# ─── Helper-level unit cases ─────────────────────────────────────────────────


def test_strip_nuls_helper_contract() -> None:
    _check(_strip_nuls(None) is None, "_strip_nuls(None) returns None")
    _check(
        _strip_nuls("clean text") == "clean text",
        "_strip_nuls passes NUL-free string through unchanged",
    )
    _check(
        _strip_nuls(f"hello{NUL}world") == "helloworld",
        f"_strip_nuls strips embedded NUL (got "
        f"{_strip_nuls(f'hello{NUL}world')!r})",
    )
    _check(
        _strip_nuls(NUL) == "",
        "_strip_nuls on NUL-only string returns empty string",
    )
    _check(
        _strip_nuls(f"{NUL}{NUL}both ends{NUL}") == "both ends",
        "_strip_nuls strips multiple NULs",
    )


# ─── append_event integration cases ──────────────────────────────────────────


def _make_event(
    *,
    content_text: str | None,
    content_json: dict[str, object] | None = None,
    role: MessageRole | None = MessageRole.USER,
    event_type: EventType = EventType.MESSAGE,
    vendor_event_id: str | None = None,
    vendor_parent_event_id: str | None = None,
    actor_session_label: str | None = None,
    actor_agent_instance_id: str | None = None,
) -> NormalizedSessionEvent:
    return NormalizedSessionEvent(
        external_session_id="ext-nul-001",
        event_type=event_type,
        role=role,
        content_text=content_text,
        content_json=content_json,
        event_at=datetime.now(UTC),
        vendor_event_id=vendor_event_id,
        vendor_parent_event_id=vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
        actor_session_label=actor_session_label,
        actor_agent_instance_id=actor_agent_instance_id,
    )


def _event_record(stub: StubStateService) -> dict[str, object]:
    """Return the single ``__event`` upsert record dict recorded.

    GAP-5: ``append_event`` lands the event via the (session_id, external_id)
    DO-NOTHING upsert now, not ``write_state``.
    """
    upserts = [u for u in stub.upserts if u.table == TABLE_EVENT]
    assert len(upserts) == 1, f"expected exactly one __event upsert (got {len(upserts)})"
    return upserts[0].record


def test_append_event_strips_content_text_nul() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    nul_text = f"tool output{NUL} with a NUL"
    event = _make_event(content_text=nul_text)
    repo.append_event(
        session_id="les-nul-001",
        external_id="cc_evt_nul_001",
        normalized=event,
        batch_id="ib-nul-001",
        content_blob_id=None,
        session_vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
    )
    rec = _event_record(stub)
    _check(
        not _record_str_values_carry_nul(rec),
        f"no TEXT field carries a NUL byte after strip "
        f"(found: {[v for v in rec.values() if isinstance(v, str) and NUL in v]!r})",
    )
    _check(
        rec["content_text"] == "tool output with a NUL",
        f"stripped content_text reaches the record (got {rec['content_text']!r})",
    )


def test_append_event_strips_every_text_field() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    event = _make_event(
        content_text=f"text{NUL}",
        vendor_event_id=f"vev{NUL}-001",
        vendor_parent_event_id=f"vparent{NUL}-001",
        actor_session_label=f"label{NUL}",
        actor_agent_instance_id=f"agi-{NUL}001",
    )
    repo.append_event(
        session_id="les-nul-002",
        external_id="cc_evt_nul_002",
        normalized=event,
        batch_id="ib-nul-002",
        content_blob_id=None,
        session_vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
    )
    rec = _event_record(stub)
    _check(
        not _record_str_values_carry_nul(rec),
        "no NUL in any TEXT-bound record field",
    )
    _check(rec["content_text"] == "text", "stripped content_text in record")
    _check(rec["vendor_event_id"] == "vev-001", "stripped vendor_event_id in record")
    _check(rec["vendor_parent_event_id"] == "vparent-001", "stripped vendor_parent_event_id")
    _check(rec["actor_session_label"] == "label", "stripped actor_session_label in record")
    _check(rec["actor_agent_instance_id"] == "agi-001", "stripped actor_agent_instance_id")


def test_append_event_strips_content_json_nul_recursively() -> None:
    """content_json was originally JSONB-tolerant of escaped NULs, but the
    2026-06-13 Bug C amendment extended the strip to JSONB via
    ``_strip_nuls_in_json`` (Postgres rejects ``\\u0000`` in JSONB with 22P05).
    NULs at every depth must be stripped. Post-Slice-5 the migrated
    ``append_event`` passes content_json as a Python DICT to ``write_state``
    (the provider serializes it to JSONB), so the smoke inspects the recorded
    dict directly rather than a json.dumps string."""
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    inner_json: dict[str, object] = {
        "tool_use_id": "tu-001",
        "input": {"arg": f"val{NUL}ue"},
    }
    event = _make_event(
        content_text=None,
        content_json=inner_json,
        event_type=EventType.TOOL_CALL,
        role=None,
    )
    repo.append_event(
        session_id="les-nul-003",
        external_id="cc_evt_nul_003",
        normalized=event,
        batch_id="ib-nul-003",
        content_blob_id=None,
        session_vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
    )
    rec = _event_record(stub)
    content_json = rec["content_json"]
    _check(
        isinstance(content_json, dict),
        f"content_json passed as a dict (provider serializes to JSONB); got "
        f"{type(content_json).__name__}",
    )
    _check(
        not _json_has_nul(content_json),
        f"no NUL at any depth post-strip (Bug C invariant; got {content_json!r})",
    )
    if isinstance(content_json, dict):
        nested = content_json.get("input")
        _check(
            isinstance(nested, dict) and nested.get("arg") == "value",
            f"stripped nested string surfaces (got {content_json!r})",
        )


# ─── upsert_session ──────────────────────────────────────────────────────────


def test_upsert_session_strips_all_text_fields() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    repo.upsert_session(
        source_id="src-001",
        external_session_id=f"ext{NUL}-sess-001",
        vendor=SourceVendor.CLAUDE_CODE,
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        vendor_session_label=f"label{NUL}",
        project_path=f"/tmp/foo{NUL}/bar",
        first_event_at=now,
        last_event_at=now,
        originator_session_label=f"orig{NUL}label",
        originator_agent_instance_id=f"orig-agi{NUL}",
        recipient_session_label=f"recip{NUL}label",
        recipient_agent_instance_id=f"recip-agi{NUL}",
        summary_text_seed=f"recap{NUL} seed",
    )
    # SQL-lockdown Slice 6: the INSERT branch is now Phase 1 ``upsert_state``
    # DO-NOTHING (recorded in ``stub.upserts``); with the stub default
    # inserted=True it lands canonical (no Phase 2 write). NUL stripping is
    # inspected on the typed-op record dict, like the other migrated writes.
    session_upserts = [u for u in stub.upserts if u.table == TABLE_SESSION]
    _check(
        len(session_upserts) == 1,
        f"one __session upsert_state issued (got {len(session_upserts)})",
    )
    if session_upserts:
        record = session_upserts[0].record
        _check(
            not _record_str_values_carry_nul(record),
            f"no NUL in upsert_session record values (offenders: "
            f"{[v for v in record.values() if isinstance(v, str) and NUL in v]!r})",
        )
        for col, want in (
            ("external_session_id", "ext-sess-001"),
            ("vendor_session_label", "label"),
            ("project_path", "/tmp/foo/bar"),
            ("originator_session_label", "origlabel"),
            ("originator_agent_instance_id", "orig-agi"),
            ("recipient_session_label", "reciplabel"),
            ("recipient_agent_instance_id", "recip-agi"),
            ("summary_text", "recap seed"),
        ):
            _check(
                record.get(col) == want,
                f"stripped {col}={want!r} in upsert record (got {record.get(col)!r})",
            )


# ─── persist_summary ─────────────────────────────────────────────────────────


def test_persist_summary_strips_text_on_insert_and_update() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    repo.persist_summary(
        session_id="les-sum-001",
        chunk_index=0,
        summary_text=f"Recap of session{NUL} including binary tool output.",
        embedding_vector_id="ev-001",
        generated_by_client_id="internal:auto_summarize",
        generated_at=datetime.now(UTC),
    )
    stripped = "Recap of session including binary tool output."
    summary_writes = [w for w in stub.writes if w.table == TABLE_SUMMARY]
    session_updates = [u for u in stub.updates if u.table == TABLE_SESSION]
    _check(len(summary_writes) == 1, "one __summary write_state issued")
    _check(len(session_updates) == 1, "one __session denormalize update_state issued")
    if summary_writes:
        rec = summary_writes[0].record
        _check(
            not _record_str_values_carry_nul(rec),
            "no NUL in the __summary write record",
        )
        _check(
            rec.get("summary_text") == stripped,
            f"stripped recap reaches the __summary write (got {rec.get('summary_text')!r})",
        )
    if session_updates:
        updates = session_updates[0].updates
        _check(
            not _record_str_values_carry_nul(updates),
            "no NUL in the __session denormalize update",
        )
        _check(
            updates.get("summary_text") == stripped,
            f"stripped recap reaches the __session denorm update "
            f"(got {updates.get('summary_text')!r})",
        )


# ─── mark_session_summary_text ───────────────────────────────────────────────


def test_mark_session_summary_text_strips_nul() -> None:
    """Defensive: callers always pass a fixed sentinel; the seam still strips."""
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    repo.mark_session_summary_text(
        session_id="les-trivial-001",
        summary_text=f"(trivial{NUL} session)",
    )
    session_updates = [u for u in stub.updates if u.table == TABLE_SESSION]
    _check(
        len(session_updates) == 1,
        "one update_state issued for mark_session_summary_text",
    )
    if session_updates:
        updates = session_updates[0].updates
        _check(
            not _record_str_values_carry_nul(updates),
            "no NUL in mark_session_summary_text update",
        )
        _check(
            updates.get("summary_text") == "(trivial session)",
            f"stripped sentinel reaches the update (got {updates.get('summary_text')!r})",
        )


# ─── finish_batch ────────────────────────────────────────────────────────────


def test_finish_batch_strips_error_message_and_kind() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    repo.finish_batch(
        batch_id="ib-fb-001",
        polling_lease_token="plt_nul_fb",
        status=ImportBatchStatus.FAILED,
        error_message=f"psycopg.DataError{NUL} cannot contain NUL bytes",
        error_kind=f"DataError{NUL}",
    )
    batch_updates = [u for u in stub.updates if u.table == TABLE_IMPORT_BATCH]
    _check(len(batch_updates) == 1, "one __import_batch update_state issued")
    if batch_updates:
        updates = batch_updates[0].updates
        _check(
            not _record_str_values_carry_nul(updates),
            "no NUL in finish_batch update",
        )
        _check(
            updates.get("error_message") == "psycopg.DataError cannot contain NUL bytes",
            f"stripped error_message in update (got {updates.get('error_message')!r})",
        )
        _check(
            updates.get("error_kind") == "DataError",
            "stripped error_kind in update",
        )


# ─── insert_source ───────────────────────────────────────────────────────────


def test_insert_source_strips_text_fields() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    repo.insert_source(
        source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        root_uri=f"file:///home/alice/.claude/projects{NUL}",
        account_label=f"alice{NUL}operator",
    )
    source_writes = [w for w in stub.writes if w.table == TABLE_SOURCE]
    _check(len(source_writes) == 1, "one source write_state issued")
    if source_writes:
        rec = source_writes[0].record
        _check(
            not _record_str_values_carry_nul(rec),
            "no NUL in insert_source record",
        )
        _check(
            rec["root_uri"] == "file:///home/alice/.claude/projects",
            f"stripped root_uri in record (got {rec['root_uri']!r})",
        )
        _check(rec["account_label"] == "aliceoperator", "stripped account_label in record")


# ─── record_attachment ───────────────────────────────────────────────────────


def test_record_attachment_strips_mime_and_filename() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    repo.record_attachment(
        event_id="ev-att-001",
        blob_id=None,
        mime_type=f"image/png{NUL}",
        filename=f"capture{NUL}.png",
        size_bytes=1024,
    )
    attachment_writes = [w for w in stub.writes if w.table == TABLE_ATTACHMENT]
    _check(len(attachment_writes) == 1, "one attachment write_state issued")
    if attachment_writes:
        rec = attachment_writes[0].record
        _check(
            not _record_str_values_carry_nul(rec),
            "no NUL in record_attachment record",
        )
        _check(rec["mime_type"] == "image/png", "stripped mime_type in record")
        _check(rec["filename"] == "capture.png", "stripped filename in record")


# ─── Regression: validator still runs on the un-stripped normalized ─────────


def test_validator_still_runs_after_strip() -> None:
    """A structurally invalid event (MESSAGE with both content_text and
    content_json None) must still raise ValueError; the NUL strip does not
    paper over shape violations."""
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    bad = _make_event(content_text=None, content_json=None)
    raised: Exception | None = None
    try:
        repo.append_event(
            session_id="les-bad-001",
            external_id="cc_evt_bad_001",
            normalized=bad,
            batch_id="ib-bad-001",
            content_blob_id=None,
            session_vendor=SourceVendor.CLAUDE_CODE,
            source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
        )
    except ValueError as exc:
        raised = exc
    _check(
        raised is not None and "require" in str(raised).lower(),
        f"structurally invalid MESSAGE still raises ValueError "
        f"(got {raised!r})",
    )


# ─── operator-identity guard ─────────────────────────────────────────────────

_OPERATOR_USERNAME_TOKEN = "d" + "w"


def test_source_carries_no_operator_username() -> None:
    """RED-FIRST (operator-identity parameterization, 2026-07-31): this smoke's
    own fixture data must never carry the real operator's OS username as a
    decorative literal — this file ships in every seed (ananta/tests/ is
    unconditional in seed_manifest.yaml's ``copy:``). The fixture values
    (``root_uri``, ``account_label``) are arbitrary strings the NUL-stripping
    logic under test does not interpret, so any neutral placeholder proves the
    same behavior; there is no functional reason for the real username to
    appear here. Composed from two concatenated halves (see
    ``_OPERATOR_USERNAME_TOKEN``) so this guard's OWN source never contains the
    contiguous token it hunts for. Word-bounded so it does not collide with an
    unrelated substring.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(_OPERATOR_USERNAME_TOKEN)}(?![A-Za-z0-9_])")
    _check(
        pattern.search(source) is None,
        "fixture source carries no bare operator-username token",
    )


def main() -> int:
    print("=== nul_byte_sanitization_smoke (operator ruling 2026-06-01 option B) ===")
    test_strip_nuls_helper_contract()
    test_append_event_strips_content_text_nul()
    test_append_event_strips_every_text_field()
    test_append_event_strips_content_json_nul_recursively()
    test_upsert_session_strips_all_text_fields()
    test_persist_summary_strips_text_on_insert_and_update()
    test_mark_session_summary_text_strips_nul()
    test_finish_batch_strips_error_message_and_kind()
    test_insert_source_strips_text_fields()
    test_record_attachment_strips_mime_and_filename()
    test_validator_still_runs_after_strip()
    test_source_carries_no_operator_username()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
