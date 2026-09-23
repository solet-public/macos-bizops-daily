#!/usr/bin/env python3
"""Offline unit smoke for the GAP-5 idempotent-ingest ``external_id`` derivation.

The live dedup smoke proves ``append_event`` dedups on the ``external_id`` it is
HANDED; this proves the other half of the idempotency chain — that the importer
PRODUCES a stable ``external_id`` (footgun B). Pure functions, no DB.

Contracts asserted:
* ``derive_event_external_id`` is deterministic (same inputs → identical id) —
  the actual re-ingest idempotency guarantee — and ``vendor_event_id`` is
  returned verbatim when present, else a ``derv:`` hash.
* ``event_at`` canonicalization: a tz-aware-UTC and a naive-UTC value at the SAME
  instant derive the SAME id (so the live importer and the slice-2 backfill,
  which read naive timestamps from the DB, agree).
* the importer ``_event_external_id`` counter gives identical null-vendor events
  distinct ordinals (0, 1, …); a fresh counter over the same source order
  reproduces the same ids (idempotent re-ingest); and a vendor-present event
  does NOT consume an ordinal (the null-vendor-only counting contract the
  slice-2 backfill must match).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.importer import (  # noqa: E402
    _event_external_id,
    _OrdinalCounter,
)
from ananta.llm.session_ledger.shared import derive_event_external_id  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    MessageRole,
    NormalizedSessionEvent,
)

_passed = 0
_failed: list[str] = []

_DT = datetime(2026, 6, 1, 12, 0, 0)


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _ev(*, content_text: str | None, vendor_event_id: str | None) -> NormalizedSessionEvent:
    return NormalizedSessionEvent(
        external_session_id="ext-1",
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text=content_text,
        content_json=None,
        event_at=_DT,
        vendor_event_id=vendor_event_id,
        vendor_parent_event_id=None,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
        actor_session_label="a",
        actor_agent_instance_id="agi-a",
    )


def _derive(*, ordinal: int, content_key: str = "same", event_at: datetime = _DT) -> str:
    return derive_event_external_id(
        vendor_event_id=None,
        session_id="s1",
        event_type="MESSAGE",
        role="user",
        content_key=content_key,
        event_at=event_at,
        ordinal=ordinal,
    )


def test_derivation_deterministic_and_prefixed() -> None:
    a = _derive(ordinal=0)
    b = _derive(ordinal=0)
    _check(a == b, "same inputs → IDENTICAL external_id (re-derivation stability)")
    _check(a.startswith("derv:"), f"null-vendor → derv:-prefixed (got {a!r})")
    _check(_derive(ordinal=1) != a, "a different ordinal → a distinct external_id")
    _check(_derive(ordinal=0, content_key="other") != a, "different content_key → distinct external_id")


def test_vendor_present_verbatim() -> None:
    out = derive_event_external_id(
        vendor_event_id="vev-123", session_id="s1", event_type="MESSAGE",
        role="user", content_key="x", event_at=_DT, ordinal=0,
    )
    _check(out == "vev-123", "vendor-present → vendor_event_id verbatim (no hash)")


def test_event_at_canonicalization_matches_live_and_backfill() -> None:
    aware = _derive(ordinal=0, event_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC))
    naive = _derive(ordinal=0, event_at=datetime(2026, 6, 1, 12, 0, 0))
    _check(
        aware == naive,
        "tz-aware-UTC and naive-UTC at the SAME instant derive the SAME id "
        "(live importer ↔ DB-naive backfill agree)",
    )


def test_importer_counter_ordinals_and_reingest_stability() -> None:
    ordinals: _OrdinalCounter = {}
    ev = _ev(content_text="same", vendor_event_id=None)
    x0 = _event_external_id(normalized=ev, session_id="s1", ordinals=ordinals)
    x1 = _event_external_id(normalized=ev, session_id="s1", ordinals=ordinals)
    _check(x0 != x1, "two identical null-vendor events → distinct ids (ordinals 0, 1)")
    _check(x0 == _derive(ordinal=0), "first null-vendor event uses ordinal 0")
    _check(x1 == _derive(ordinal=1), "second identical event uses ordinal 1")
    # Re-ingest: a FRESH counter over the same source order reproduces the same ids.
    fresh: _OrdinalCounter = {}
    y0 = _event_external_id(normalized=ev, session_id="s1", ordinals=fresh)
    _check(y0 == x0, "re-ingest (fresh counter, same order) → SAME id as first ingest (idempotent)")


def test_vendor_present_does_not_consume_an_ordinal() -> None:
    ordinals: _OrdinalCounter = {}
    null_ev = _ev(content_text="same", vendor_event_id=None)
    vendor_ev = _ev(content_text="same", vendor_event_id="vev-mid")  # SAME tuple as null_ev
    a = _event_external_id(normalized=null_ev, session_id="s1", ordinals=ordinals)
    v = _event_external_id(normalized=vendor_ev, session_id="s1", ordinals=ordinals)
    b = _event_external_id(normalized=null_ev, session_id="s1", ordinals=ordinals)
    _check(v == "vev-mid", "the interleaved vendor-present event returns its id verbatim")
    _check(a == _derive(ordinal=0), "the first null-vendor event is ordinal 0")
    _check(
        b == _derive(ordinal=1),
        "the vendor-present event did NOT consume an ordinal → next null-vendor is ordinal 1 "
        "(the null-vendor-only counting contract the slice-2 backfill must match)",
    )


def test_oversized_offloaded_event_is_content_addressed() -> None:
    # GAP-5 slice-1 MAJOR fix (Reviewer-A): an OFFLOADED null-vendor event must
    # derive over its CONTENT, NOT content_blob_id — the blob id is a random
    # pointer minted fresh on every (unconditional) re-offload, so keying on it
    # would diverge the external_id across re-ingests of the SAME oversized event
    # and silently DUPLICATE it. normalized.content_text is still present at
    # derivation time (offload nulls only the persisted row), and sha256 absorbs
    # any size, so the derivation is content-addressed + re-offload-stable.
    oversized = "x" * 100_000  # well over the offload threshold
    ev = _ev(content_text=oversized, vendor_event_id=None)
    first = _event_external_id(normalized=ev, session_id="s1", ordinals={})
    second = _event_external_id(normalized=ev, session_id="s1", ordinals={})
    _check(
        first == second,
        "an oversized null-vendor event derives a STABLE id across re-ingest "
        "(content-addressed — independent of the per-offload random blob_id)",
    )
    _check(
        first == _derive(ordinal=0, content_key=oversized),
        "the derived id hashes the full content_text (NOT a content_blob_id pointer)",
    )


def main() -> int:
    print("=== ingest_external_id_derivation_smoke ===")
    test_derivation_deterministic_and_prefixed()
    test_vendor_present_verbatim()
    test_event_at_canonicalization_matches_live_and_backfill()
    test_importer_counter_ordinals_and_reingest_stability()
    test_vendor_present_does_not_consume_an_ordinal()
    test_oversized_offloaded_event_is_content_addressed()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
