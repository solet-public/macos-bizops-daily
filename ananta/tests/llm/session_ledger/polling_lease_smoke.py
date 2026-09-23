#!/usr/bin/env python3
"""Polling-lease (D14) smoke — Tests 1-10 per v8 §9.2.

Covers the v8 owner-aware fenced polling lease + batch-ownership fencing,
the elapsed-time LeaseHeartbeat, the adopt-before-start importer order, and
the lease-handoff batch-clobber regression (Codex v7 B1 + B2).

SQL-lockdown Slice 2: ``polling_driver`` rides the state-interface primitives
(``_acquire_lease`` / ``update_state`` / ``query_ordered``) instead of raw SQL,
so this smoke asserts on the recorded TYPED-OP calls (``stub.acquire_leases`` /
``stub.updates``) + the verdict knobs (``set_acquire_lease_result`` /
``set_update_rows_affected``) rather than SQL strings + ``fetch_one`` priming.
Behavioral filter+order coverage (the real CAS / expiry fence / recency window)
lives in ``polling_driver_migration_live_smoke.py`` vs the real schema.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/polling_lease_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService, UpdateRecord  # noqa: E402
from ananta.llm.session_ledger.importer import (  # noqa: E402
    LeaseHeartbeat,
)
from ananta.llm.session_ledger.repository import (  # noqa: E402
    LeaseLostError,
    PollingLeaseHandle,
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_IMPORT_BATCH,
    TABLE_SOURCE,
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


def _source_updates(stub: StubStateService) -> list[UpdateRecord]:
    return [u for u in stub.updates if u.table == TABLE_SOURCE]


def _refresh_updates(stub: StubStateService) -> list[UpdateRecord]:
    """A refresh update sets polling_lease_until but does NOT clear the token."""
    return [
        u for u in _source_updates(stub)
        if "polling_lease_token" not in u.updates
    ]


def _release_updates(stub: StubStateService) -> list[UpdateRecord]:
    """A release update clears BOTH lease columns to None."""
    return [
        u for u in _source_updates(stub)
        if u.updates.get("polling_lease_until") is None
        and "polling_lease_token" in u.updates
        and u.updates.get("polling_lease_token") is None
    ]


# ─── Tests 1-7: repository-layer lease primitives ───────────────────────


def test_1_happy_path_acquire_release() -> None:
    stub = StubStateService()  # default acquire verdict = True
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]

    handle = repo.try_acquire_polling_lease("src-1", ttl_seconds=600)
    _check(
        handle is not None,
        f"acquire returns handle when source unleased (got {handle})",
    )
    _check(
        len(stub.acquire_leases) == 1
        and stub.acquire_leases[0].table == TABLE_SOURCE
        and stub.acquire_leases[0].lease_column == "polling_lease_until",
        "acquire issues one acquire_lease CAS on __source (lease_column fenced)",
    )
    if handle is not None:
        _check(handle.source_id == "src-1", "handle.source_id is src-1")
        _check(
            len(handle.lease_token) >= 16,
            f"handle.lease_token is non-trivial (got {handle.lease_token!r})",
        )
        repo.release_polling_lease(handle)

    release_calls = _release_updates(stub)
    _check(
        len(release_calls) == 1,
        f"release issues one update_state clearing both columns (got {len(release_calls)})",
    )


def test_2_concurrent_acquire_second_skips() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]

    # First acquire wins (verdict True); the second races a now-held lease
    # (verdict flips to False — the expiry-fenced CAS matches 0 rows).
    h1 = repo.try_acquire_polling_lease("src-1", ttl_seconds=600)
    stub.set_acquire_lease_result(False)
    h2 = repo.try_acquire_polling_lease("src-1", ttl_seconds=600)
    _check(h1 is not None, "first concurrent acquire succeeds")
    _check(h2 is None, "second concurrent acquire skips (returns None)")


def test_3_owner_aware_refresh_extends() -> None:
    stub = StubStateService()  # default update rows-affected = 1
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]

    handle = PollingLeaseHandle(
        source_id="src-1",
        lease_token="tok-A",
        lease_until=datetime.now(UTC) + timedelta(seconds=600),
    )
    refreshed = repo.refresh_polling_lease(handle, ttl_seconds=600)
    _check(
        refreshed is not None,
        "refresh returns a fresh handle when token matches",
    )
    if refreshed is not None:
        _check(refreshed.lease_token == "tok-A", "token preserved across refresh")
        _check(
            refreshed.lease_until >= handle.lease_until,
            "lease_until advanced (or equal) after refresh",
        )

    refresh_calls = _refresh_updates(stub)
    _check(
        len(refresh_calls) == 1
        and refresh_calls[0].filters.get("polling_lease_token") == "tok-A",
        f"refresh issues one token-fenced update_state (got {len(refresh_calls)})",
    )


def test_4_stale_owner_release_is_silent_noop() -> None:
    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    stale = PollingLeaseHandle(
        source_id="src-1",
        lease_token="tok-stale",
        lease_until=datetime.now(UTC),
    )
    repo.release_polling_lease(stale)  # MUST NOT raise

    release_calls = _release_updates(stub)
    _check(
        len(release_calls) == 1
        and release_calls[0].filters.get("polling_lease_token") == "tok-stale",
        "stale-owner release still issues the token-fenced update_state "
        "(no-op against the persisted row in production)",
    )


def test_5_stale_owner_refresh_returns_none() -> None:
    stub = StubStateService()
    stub.set_update_rows_affected(0)  # token mismatch → 0 rows affected
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    stale = PollingLeaseHandle(
        source_id="src-1",
        lease_token="tok-stale",
        lease_until=datetime.now(UTC),
    )
    refreshed = repo.refresh_polling_lease(stale, ttl_seconds=600)
    _check(refreshed is None, "stale-owner refresh returns None")


def test_6_long_walk_heartbeat_extends_past_original_ttl() -> None:
    stub = StubStateService()  # refresh always succeeds (rows-affected = 1)
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    handle = PollingLeaseHandle(
        source_id="src-1",
        lease_token="tok-A",
        lease_until=datetime.now(UTC) + timedelta(seconds=2),
    )
    heartbeat = LeaseHeartbeat(repo, handle, ttl_seconds=2)
    # Force the heartbeat's last_refresh_at into the past so check() triggers
    # a refresh on the next call.
    heartbeat._last_refresh_at = datetime.now(UTC) - timedelta(seconds=5)  # type: ignore[attr-defined]
    heartbeat.check()  # MUST refresh (elapsed >= ttl/2 = 1s)

    _check(
        len(_refresh_updates(stub)) == 1,
        f"heartbeat issued one refresh when elapsed >= ttl/2 "
        f"(got {len(_refresh_updates(stub))})",
    )
    # Within-window check: should NOT refresh again immediately.
    heartbeat.check()
    _check(
        len(_refresh_updates(stub)) == 1,
        f"heartbeat does NOT refresh again within the window "
        f"(got {len(_refresh_updates(stub))})",
    )


def test_7_ownership_loss_mid_walk_aborts_via_lease_lost() -> None:
    stub = StubStateService()
    stub.set_update_rows_affected(0)  # refresh CAS misses → repo returns None
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    handle = PollingLeaseHandle(
        source_id="src-1",
        lease_token="tok-A",
        lease_until=datetime.now(UTC) + timedelta(seconds=2),
    )
    heartbeat = LeaseHeartbeat(repo, handle, ttl_seconds=2)
    heartbeat._last_refresh_at = datetime.now(UTC) - timedelta(seconds=10)  # type: ignore[attr-defined]

    raised = False
    try:
        heartbeat.check()
    except LeaseLostError:
        raised = True
    _check(
        raised,
        "heartbeat raises LeaseLostError when refresh returns None mid-walk",
    )


# ─── Tests 8-10: importer-layer adoption + handoff regression ────────────


def test_8_importer_finish_batch_carries_handle_token() -> None:
    """End-to-end: when the importer's wrapper runs, finish_batch receives
    the lease_token. We assert this by inspecting the typed update_state CAS
    filters of the finish_batch call. The full importer wiring is exercised
    by the live ChatGPT smoke; here we assert the contract surface."""
    stub = StubStateService()  # finish_batch CAS matches (rows-affected = 1)
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]

    from ananta.llm.session_ledger.types import (  # noqa: PLC0415
        ImportBatchStatus,
    )
    landed = repo.finish_batch(
        "imb-1",
        polling_lease_token="tok-A",
        status=ImportBatchStatus.COMPLETED,
    )
    _check(landed is True, "finish_batch returns True when conditional CAS matched")

    finish_calls = [u for u in stub.updates if u.table == TABLE_IMPORT_BATCH]
    _check(
        len(finish_calls) == 1,
        f"finish_batch issues exactly one update_state (got {len(finish_calls)})",
    )
    if finish_calls:
        filters = finish_calls[0].filters
        _check(
            filters.get("polling_lease_token") == "tok-A",
            "finish_batch CAS carries the token guard",
        )
        _check(
            filters.get("status") == ImportBatchStatus.RUNNING.value,
            "finish_batch CAS carries the still-RUNNING guard "
            "(guards against double-finish)",
        )


def test_9_adoption_order_regression_uses_adopt_before_start() -> None:
    """Regression smoke for Codex v7 B1 — adopt MUST run before start_batch
    so the upload-route batch becomes the importer's batch instead of being
    stranded as a permanent orphan."""
    stub = StubStateService()
    # A recent adoptable route batch exists (within the recency window); the
    # query_ordered planted-row shim returns it, the CAS claim succeeds.
    stub.add_select_response(
        "session_ledger__import_batch",
        [{"id": "imb-route", "started_at": datetime.now(UTC)}],
    )
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    claimed = repo.adopt_route_batch_for_source(
        "src-1", polling_lease_token="tok-A", recency_window_minutes=10,
    )
    _check(
        claimed == "imb-route",
        f"adopt_route_batch_for_source returns the claimed batch id "
        f"(got {claimed!r})",
    )

    adopt_calls = [u for u in stub.updates if u.table == TABLE_IMPORT_BATCH]
    _check(
        len(adopt_calls) == 1,
        f"adopt issues exactly one update_state CAS (got {len(adopt_calls)})",
    )
    if adopt_calls:
        filters = adopt_calls[0].filters
        _check(
            filters.get("polling_lease_token") == {"op": "is_null"}
            and filters.get("status") == "running",
            "adopt CAS guards polling_lease_token IS NULL + status='running'",
        )


def test_10_lease_handoff_batch_ownership_no_clobber() -> None:
    """Codex v7 B1 regression: A's late finish_batch must NOT clobber B's
    BATCH_B because A's token doesn't match B's batch row. AND: B cannot
    adopt A's BATCH_A because A's batch row has a non-NULL token.

    Both conditions are enforced by the conditional-CAS guards."""
    stub = StubStateService()
    # B attempts to adopt but no adoptable (token-NULL) batch exists → the
    # query_ordered candidate set is empty → adopt returns None.
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    claimed = repo.adopt_route_batch_for_source(
        "src-1", polling_lease_token="tok-B", recency_window_minutes=10,
    )
    _check(
        claimed is None,
        "B's adopt returns None when no token-NULL batch is adoptable (handoff guard)",
    )

    # A's late finish_batch on BATCH_B fails the CAS because BATCH_B carries
    # tok-B (not tok-A) → 0 rows affected → finish_batch returns False.
    stub.set_update_rows_affected(0)
    from ananta.llm.session_ledger.types import (  # noqa: PLC0415
        ImportBatchStatus,
    )
    landed = repo.finish_batch(
        "imb-B",
        polling_lease_token="tok-A",  # WRONG — A's token; BATCH_B carries tok-B
        status=ImportBatchStatus.FAILED,
        error_message="A's late finish",
        error_kind="lease_lost",
    )
    _check(
        landed is False,
        "A's late finish_batch on BATCH_B returns False (no clobber)",
    )


# ─── Driver ─────────────────────────────────────────────────────────────


def main() -> int:
    print("ananta/tests/llm/session_ledger/polling_lease_smoke.py")
    test_1_happy_path_acquire_release()
    test_2_concurrent_acquire_second_skips()
    test_3_owner_aware_refresh_extends()
    test_4_stale_owner_release_is_silent_noop()
    test_5_stale_owner_refresh_returns_none()
    test_6_long_walk_heartbeat_extends_past_original_ttl()
    test_7_ownership_loss_mid_walk_aborts_via_lease_lost()
    test_8_importer_finish_batch_carries_handle_token()
    test_9_adoption_order_regression_uses_adopt_before_start()
    test_10_lease_handoff_batch_ownership_no_clobber()
    print()
    print(f"passed: {_passed}")
    if _failed:
        print(f"failed: {len(_failed)}")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
