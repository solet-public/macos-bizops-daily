"""lift_canonical_pointer_for_duplicate_sessions — service-layer smoke.

Covers the SERVICE-side dry-run / confirm / cadence contract:
1. **Dry-run** (confirm=False) returns the duplicate_group_count without
   touching scheduling_service.
2. **Confirmed run** (confirm=True) requires a scheduling_service (the
   try/finally pause/resume envelope) — raises when it is None.
3. **cadence_minutes** out of range raises.

The REPOSITORY-level behavior (the dup-finder + the FOR-UPDATE → conditional-CAS
canonical-election rework landed in the SQL-lockdown migration) is verified
behaviorally against a real provider in
``canonical_pointer_repair_live_smoke.py``. This smoke's former stub-based
``GROUP BY … HAVING`` / ``FOR UPDATE`` SQL-shape assertions were retired with
that migration — they pinned raw SQL the repository no longer emits.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_dry_run_does_not_touch_scheduling_service() -> None:
    """Service-side dry-run returns the probe count without pausing the cron."""
    # We exercise via the service-impl path by mocking the repository + scheduler.
    from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: PLC0415

    class _StubRepo:
        def count_canonical_duplicate_sessions(self) -> int:
            return 7

        def lift_canonical_pointer_for_duplicate_sessions(self) -> int:
            return 99  # Should never be called on dry-run

    class _StubScheduler:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def clear_scheduled_actions_by_tag(self, *, tag: str) -> None:
            self.calls.append(f"clear:{tag}")

    repo = _StubRepo()
    scheduler = _StubScheduler()
    service = SessionLedgerService.__new__(SessionLedgerService)
    service._repository = repo  # type: ignore[assignment]
    service._scheduling_service = scheduler  # type: ignore[assignment]
    result = service.lift_canonical_pointer_for_duplicate_sessions(confirm=False)
    _assert(result["confirmed"] is False, str(result))
    _assert(result["duplicate_group_count"] == 7, str(result))
    _assert(result["demoted_count"] == 0, str(result))
    _assert(result["resume_outcome"] == "skipped", str(result))
    _assert(scheduler.calls == [], f"dry-run must NOT touch scheduler; got {scheduler.calls}")


def test_confirmed_run_requires_scheduling_service() -> None:
    """confirm=True with scheduling_service=None raises before any DB writes."""
    from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: PLC0415

    class _StubRepo:
        def count_canonical_duplicate_sessions(self) -> int:
            return 3

        def lift_canonical_pointer_for_duplicate_sessions(self) -> int:
            raise AssertionError("must NOT be called when scheduling_service is None")

    service = SessionLedgerService.__new__(SessionLedgerService)
    service._repository = _StubRepo()  # type: ignore[assignment]
    service._scheduling_service = None  # type: ignore[assignment]
    try:
        service.lift_canonical_pointer_for_duplicate_sessions(confirm=True)
    except RuntimeError as exc:
        _assert(
            "scheduling_service" in str(exc),
            f"error must mention scheduling_service; got {exc}",
        )
        return
    raise AssertionError("confirm=True with no scheduling_service must RuntimeError")


def test_cadence_minutes_out_of_range_raises() -> None:
    from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: PLC0415

    service = SessionLedgerService.__new__(SessionLedgerService)
    for bad in (0, 60, -1, 1000):
        try:
            service.lift_canonical_pointer_for_duplicate_sessions(
                confirm=False, cadence_minutes=bad
            )
        except ValueError:
            continue
        raise AssertionError(f"cadence_minutes={bad} must ValueError")


def main() -> int:
    tests = [
        test_dry_run_does_not_touch_scheduling_service,
        test_confirmed_run_requires_scheduling_service,
        test_cadence_minutes_out_of_range_raises,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
