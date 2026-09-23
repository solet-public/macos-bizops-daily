#!/usr/bin/env python3
"""M6.5 Bug 2 backfill verb smoke.

Run:

    .venv/bin/python3 ananta/tests/session_ledger_service/backfill_first_last_event_at_repair_smoke.py

Per 2026-06-11 M6.5 Bug 2 backfill (Coordinator-Dawn dispatch §4): the
``backfill_first_last_event_at_repair`` verb on
``SessionLedgerInvertedBoundsRepairAPI`` wraps a per-row recompute pass over inverted
``__session`` rows inside a try/finally pause-resume envelope around
the importer-poll cron. This smoke verifies the verb's contract shape
end-to-end against stub state + stub scheduling services.

Verifications:

1. Dry-run path (``confirm=False``) returns inverted count + does NOT
   touch the scheduling service.
2. Confirm path calls ``clear_scheduled_actions_by_tag(tag=…)`` BEFORE
   the repair loop.
3. Confirm path calls the repository's
   ``repair_inverted_first_last_event_at()`` exactly once.
4. Confirm path calls ``ensure_periodic_poll_schedule`` in its
   finally-block to re-ensure the cron.
5. The finally-block runs even when the repair loop raises.
6. ``scheduling_service=None`` (unbound) makes the confirm path
   raise ``RuntimeError`` before the loop starts (no
   pause-without-resume risk).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: {message}")


class _StubRepository:
    def __init__(
        self,
        inverted_count: int = 7,
        repair_raises: bool = False,
        repaired_count: int = 5,
    ) -> None:
        self._inverted = inverted_count
        self._raises = repair_raises
        self._repaired = repaired_count
        self.calls: list[str] = []

    def count_inverted_first_last_event_at_sessions(self) -> int:
        self.calls.append("count_inverted")
        return self._inverted

    def repair_inverted_first_last_event_at(self) -> int:
        self.calls.append("repair")
        if self._raises:
            raise RuntimeError("synthetic repair failure")
        return self._repaired


class _StubScheduling:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def clear_scheduled_actions_by_tag(self, tag: str) -> dict[str, Any]:
        self.calls.append(("clear", {"tag": tag}))
        return {"data": {"cleared_count": 1}}

    def create_cron_schedule(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_cron", kwargs))
        return {"data": {"schedule_id": "sch_resumed"}}


class _StubStateService:
    """Minimal upsert_state recorder for the template_flow cancel-mark path."""

    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []

    def upsert_state(
        self, namespace: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        self.upserts.append({"namespace": namespace, "data": data})
        return {"action_status": "completed"}


def _build_service(
    repo: _StubRepository,
    scheduling: _StubScheduling | None,
) -> Any:
    from ananta.services.session_ledger_service.service import (
        SessionLedgerService,
    )

    svc = SessionLedgerService.__new__(SessionLedgerService)
    svc._repository = repo  # type: ignore[attr-defined]
    svc._scheduling_service = scheduling  # type: ignore[attr-defined]
    svc._inference_service = None  # type: ignore[attr-defined]
    svc._importer = None  # type: ignore[attr-defined]
    svc._state_service = _StubStateService()  # type: ignore[attr-defined]
    return svc


def test_dry_run_returns_inverted_count_no_scheduling() -> None:
    repo = _StubRepository(inverted_count=42)
    scheduling = _StubScheduling()
    svc = _build_service(repo, scheduling)
    result = svc.backfill_first_last_event_at_repair(confirm=False)
    _expect(result["confirmed"] is False, "dry-run returns confirmed=False")
    _expect(result["inverted_count"] == 42, f"dry-run reports inverted_count=42; got {result['inverted_count']}")
    _expect(result["repaired_count"] == 0, "dry-run reports repaired_count=0")
    _expect(result["resume_outcome"] == "skipped", "dry-run reports resume_outcome=skipped")
    _expect(
        repo.calls == ["count_inverted"],
        f"dry-run only calls count_inverted; got {repo.calls}",
    )
    _expect(
        scheduling.calls == [],
        f"dry-run does NOT touch scheduling; got {scheduling.calls}",
    )


def test_confirm_path_pauses_then_repairs_then_resumes() -> None:
    repo = _StubRepository(inverted_count=7, repaired_count=5)
    scheduling = _StubScheduling()
    svc = _build_service(repo, scheduling)
    result = svc.backfill_first_last_event_at_repair(confirm=True)
    _expect(result["confirmed"] is True, "confirm returns confirmed=True")
    _expect(result["inverted_count"] == 7, "confirm reports inverted_count=7")
    _expect(result["repaired_count"] == 5, "confirm reports repaired_count=5")
    _expect(repo.calls == ["count_inverted", "repair"], f"repo call order; got {repo.calls}")
    # scheduling: clear FIRST (to pause cron before repair), then in finally
    # ensure_periodic_poll_schedule calls its OWN clear (idempotent no-op since
    # we just cleared) followed by create_cron. So the canonical sequence is
    # clear-pause → clear-noop → create-resume.
    scheduling_op_names = [name for name, _ in scheduling.calls]
    _expect(
        scheduling_op_names == ["clear", "clear", "create_cron"],
        f"scheduling op order (pause-clear, re-ensure-clear-noop, create-resume); "
        f"got {scheduling_op_names}",
    )


def test_finally_resumes_even_when_repair_raises() -> None:
    repo = _StubRepository(inverted_count=1, repair_raises=True)
    scheduling = _StubScheduling()
    svc = _build_service(repo, scheduling)
    raised = False
    try:
        svc.backfill_first_last_event_at_repair(confirm=True)
    except RuntimeError as exc:
        raised = True
        _expect("synthetic" in str(exc), "raised exception is the synthetic one")
    _expect(raised, "confirm path propagates the repair exception")
    scheduling_op_names = [name for name, _ in scheduling.calls]
    _expect(
        scheduling_op_names == ["clear", "clear", "create_cron"],
        f"finally still ran clear+create_cron resume even though repair raised; "
        f"got {scheduling_op_names}",
    )


def test_no_scheduling_service_raises_before_pause() -> None:
    repo = _StubRepository(inverted_count=3)
    svc = _build_service(repo, scheduling=None)
    raised = False
    try:
        svc.backfill_first_last_event_at_repair(confirm=True)
    except RuntimeError as exc:
        raised = True
        _expect(
            "scheduling_service" in str(exc),
            "RuntimeError message names scheduling_service",
        )
    _expect(raised, "unbound scheduling_service raises before pause")


def main() -> None:
    print("M6.5 Bug 2 backfill — backfill_first_last_event_at_repair smoke")
    print("=" * 60)
    test_dry_run_returns_inverted_count_no_scheduling()
    test_confirm_path_pauses_then_repairs_then_resumes()
    test_finally_resumes_even_when_repair_raises()
    test_no_scheduling_service_raises_before_pause()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
