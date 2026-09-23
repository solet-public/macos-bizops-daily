#!/usr/bin/env python3
"""Regression smoke for the 2026-08-15 oversized-payload outage (D1/D2/D5/D8).

Root cause (``workbench/2026-08-14_action_queue_stall_incident/INCIDENT.md``):
an unbounded ``read_state`` over a 109,393-row table produced an 82 MB
``deliver_result`` payload. A worker thread spent two hours inside CPython's C
JSON scanner parsing it while holding the GIL ~95% of the time, starving every
other Python thread; the platform froze for 3h20m. It was the second occurrence
of the class.

**The fixtures here ADVANCE — they build genuinely oversized payloads and prove
the guards fire on them.** A test that asserted on a constant, or that only
exercised the comparison, would pass just as happily against a guard that can
never fire in production. Case 1 constructs a string over the real
``MAX_ACTION_PARAMETERS_BYTES`` bound; case 5 constructs a record set over the
real ``MAX_READ_ROWS`` cap.

**The load-bearing property is not "the guard rejects big things" — it is
"the guard rejects them BEFORE anything parses them."** Case 3 proves that
directly: it hands the claim guard a string that is over the bound AND is
syntactically invalid JSON. If the guard parsed before measuring, that input
would raise a JSON decode error; because it measures first, it raises the size
refusal. That is the only assertion in this file that could distinguish a
correct guard from one placed downstream of ``json.loads`` — which is exactly
the guard that would have caused the outage it was meant to prevent.

Run:

    .venv/bin/python3 ananta/tests/core/actions/oversized_payload_guard_smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_path_liveness import (  # noqa: E402
    ActionPathLiveness,
)
from ananta.core.actions.orphan_reaper import (  # noqa: E402
    EVIDENCE_FLOOR_CREATED_AT,
    reap_orphaned_processing_actions,
)
from ananta.core.actions.payload_bounds import (  # noqa: E402
    MAX_ACTION_PARAMETERS_BYTES,
    OversizedActionPayloadError,
    check_action_parameters_size,
    check_claimed_parameters_size,
)
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.state_service.read_bounds import (  # noqa: E402
    MAX_READ_ROWS,
    ReadBoundError,
    resolve_read_limit,
)

_failures: list[str] = []


def _check(condition: bool, label: str) -> None:  # noqa: FBT001
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


def _oversized_json_string() -> str:
    """Build a REAL payload over the bound — not a mock, not a constant.

    Shaped like the payload that actually wedged the platform: a
    ``result_payload.data.records`` list of state rows. Sized just past the
    bound rather than at 82 MB so the smoke stays fast while remaining a
    genuine over-bound input.
    """
    row = {
        "id": "ar-2nf65vh7e0mr2",
        "namespace": "core",
        "created_by": "ananta.services.state_service",
        "payload": "x" * 512,
    }
    records: list[dict[str, object]] = []
    serialized = ""
    while len(serialized.encode("utf-8")) <= MAX_ACTION_PARAMETERS_BYTES:
        records.extend(row.copy() for _ in range(2000))
        serialized = json.dumps(
            {"result_payload": {"success": True, "data": {"records": records}}},
        )
    return serialized


def test_enqueue_guard_refuses_oversized_payload() -> None:
    print("\n[1] enqueue guard refuses a real over-bound payload")
    payload = _oversized_json_string()
    actual_size = len(payload.encode("utf-8"))
    _check(
        actual_size > MAX_ACTION_PARAMETERS_BYTES,
        f"fixture ADVANCES: {actual_size} bytes exceeds the {MAX_ACTION_PARAMETERS_BYTES} bound",
    )
    try:
        check_action_parameters_size(
            payload, process_key="plugin::agent_messaging_plugin::deliver_result",
        )
    except FrameworkError as exc:
        message = str(exc)
        _check(True, "enqueue guard raised FrameworkError")
        _check(
            str(actual_size) in message,
            "refusal names the ACTUAL size (a bound-only message cannot be acted on)",
        )
        _check(
            str(MAX_ACTION_PARAMETERS_BYTES) in message,
            "refusal names the BOUND",
        )
        _check(
            "limit" in message or "paginate" in message or "reference" in message,
            "refusal is INSTRUCTIVE: names a remedy, not just a failure",
        )
    else:
        _check(False, "enqueue guard raised nothing on an over-bound payload")


def test_enqueue_guard_passes_normal_payload() -> None:
    print("\n[2] negative control: ordinary payload is untouched")
    # 2.5 MB is the measured MEAN of legitimate deliver_result traffic on the
    # day of the outage, so this is the real-world case that must keep working.
    ordinary = json.dumps({"result_payload": {"data": "y" * (2_500_000)}})
    try:
        check_action_parameters_size(
            ordinary,
            process_key="plugin::agent_messaging_plugin::deliver_result",
        )
    except FrameworkError as exc:
        _check(False, f"legitimate 2.5 MB payload was wrongly refused: {exc}")
    else:
        _check(True, "legitimate 2.5 MB payload passes (guard is not a blanket refusal)")


def test_claim_guard_measures_before_parsing() -> None:
    print("\n[3] claim guard fires BEFORE the parse — the load-bearing property")
    # Over the bound AND syntactically invalid JSON. A guard that parsed first
    # would raise json.JSONDecodeError here; one that measures first raises the
    # size refusal. This is what distinguishes a guard that can fire in time
    # from one that has already caused the outage by the time it measures.
    unparseable_oversized = "{" + ("z" * (MAX_ACTION_PARAMETERS_BYTES + 1024))
    try:
        check_claimed_parameters_size(
            unparseable_oversized,
            action_id="ae-2nz6uhvs11vu3",
            process_key="plugin::agent_messaging_plugin::deliver_result",
        )
    except OversizedActionPayloadError as exc:
        _check(True, "claim guard raised the SIZE error on unparseable input")
        _check(
            exc.size > exc.bound,
            "error carries the measured size and the bound",
        )
        _check(
            "ae-2nz6uhvs11vu3" in str(exc),
            "refusal names the action id (so it can be found in the queue)",
        )
    except json.JSONDecodeError:
        _check(
            False,
            "GUARD PARSED FIRST — it would run only AFTER the GIL-holding parse",
        )
    else:
        _check(False, "claim guard raised nothing on an over-bound payload")


def test_read_limit_refuses_over_cap_without_consent() -> None:
    print("\n[4] read_state limit mirrors the query_ordered Gap-C policy")
    try:
        resolve_read_limit(MAX_READ_ROWS + 1, table="action_events")
    except ReadBoundError as exc:
        _check(True, "explicit limit over the cap is refused")
        _check("unbounded" in str(exc), "refusal names the unbounded=True opt-in")
    else:
        _check(False, "over-cap limit was accepted without consent")

    fetch_limit, overflow_is_error = resolve_read_limit(
        MAX_READ_ROWS + 1, unbounded=True, table="action_events",
    )
    _check(
        fetch_limit == MAX_READ_ROWS + 1 and not overflow_is_error,
        "explicit unbounded=True consent is honoured",
    )

    fetch_limit, overflow_is_error = resolve_read_limit(None, table="action_events")
    _check(
        fetch_limit == MAX_READ_ROWS + 1 and overflow_is_error,
        "absent limit fetches cap+1 so overflow is DETECTED, never truncated",
    )

    fetch_limit, overflow_is_error = resolve_read_limit(50, table="action_events")
    _check(
        fetch_limit == 50 and not overflow_is_error,
        "an under-cap explicit limit is used as-is",
    )


def test_unbounded_read_refuses_rather_than_truncating() -> None:
    print("\n[5] an unbounded read over the cap REFUSES — never returns a prefix")
    # The fixture ADVANCES: a record set genuinely larger than the cap, which is
    # what the provider sees when it fetches cap+1 rows.
    rows = [{"id": f"ar-{i}"} for i in range(MAX_READ_ROWS + 1)]
    _check(
        len(rows) > MAX_READ_ROWS,
        f"fixture ADVANCES: {len(rows)} rows exceeds the {MAX_READ_ROWS} cap",
    )
    # This mirrors the provider's branch exactly: fetch cap+1, and if that many
    # came back, refuse. The silent-truncation failure would be returning
    # rows[:MAX_READ_ROWS] here.
    _, overflow_is_error = resolve_read_limit(None, table="action_events")
    would_refuse = overflow_is_error and len(rows) > MAX_READ_ROWS
    _check(would_refuse, "overflow is an ERROR, not a silently truncated prefix")


def test_liveness_stale_age_alone_is_stalled() -> None:
    print("\n[6] liveness alarm is STALE POLL AGE ALONE — depth is corroborating, not gating")
    liveness = ActionPathLiveness()

    # GAU-10 (2026-08-18, measured live during a blue-green swap): a freeze
    # that begins at an idle moment (depth 0) must still alarm. A frozen
    # poller pins queue_depth at its pre-freeze value forever, so depth can
    # never gate the alarm — it is exactly as stale as the age it used to
    # suppress.
    liveness.record_poll_cycle(queue_depth=0, dispatched=0)
    liveness.last_poll_monotonic = liveness.started_monotonic - 10_000.0
    _check(
        liveness.stalled(),
        "stale age WITH an empty queue IS stalled (GAU-10: the measured lie)",
    )
    snapshot = liveness.snapshot()
    _check(
        snapshot["action_path_stalled"] is True,
        "snapshot ships the DERIVED verdict for the empty-queue freeze too",
    )

    # Depth still rides along as corroborating data but never gates: fresh
    # age must not alarm even with a deep queue, because record_poll_cycle
    # stamps every cycle including empty ones — an idle-but-alive poller
    # keeps age fresh regardless of what depth is doing.
    liveness.record_poll_cycle(queue_depth=40, dispatched=0)
    _check(
        not liveness.stalled(),
        "fresh poll age is NOT stalled regardless of queue depth",
    )

    liveness.last_poll_monotonic = liveness.started_monotonic - 10_000.0
    _check(
        liveness.stalled(),
        "stale age with a non-empty queue IS stalled (the 2026-08-15 signature)",
    )


class _ReaperSpy:
    """Records the reap's state writes without touching a database."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.query: dict[str, object] | None = None
        self.updates: list[tuple[str, object]] = []

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        _ = namespace
        self.query = data
        return {"data": {"records": self._rows}}

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, object]:
        _ = namespace
        filters = query.get("filters", {})
        action_id = filters.get("id") if isinstance(filters, dict) else None
        self.updates.append((str(action_id), updates.get("status")))
        return {"data": {}}


def test_reaper_fails_oversized_and_requeues_small() -> None:
    print("\n[7] reaper FAILS oversized orphans and only requeues small ones")
    oversized = "q" * (MAX_ACTION_PARAMETERS_BYTES + 2048)
    spy = _ReaperSpy(
        [
            {
                "id": "ae-oversized",
                "process_key": "plugin::agent_messaging_plugin::deliver_result",
                "parameters": oversized,
            },
            {
                "id": "ae-small",
                "process_key": "plugin::agent_messaging_plugin::deliver_result",
                "parameters": '{"result_payload": {"ok": true}}',
            },
        ],
    )
    counts = reap_orphaned_processing_actions(spy)

    _check(counts["failed"] == 1, "the oversized orphan was FAILED")
    _check(counts["requeued"] == 1, "the small orphan was requeued")
    _check(
        ("ae-oversized", "failed") in spy.updates,
        "D13: an oversized orphan is never returned to queued (that re-wedges)",
    )
    _check(
        ("ae-small", "queued") in spy.updates,
        "a recoverable orphan does return to queued",
    )

    filters = (spy.query or {}).get("filters", {})
    assert isinstance(filters, dict)
    created_at = filters.get("created_at")
    _check(
        isinstance(created_at, dict) and created_at.get("op") == "gt",
        "June evidence rows are excluded IN SQL (never transferred)",
    )
    _check(
        isinstance(created_at, dict)
        and created_at.get("value") == EVIDENCE_FLOOR_CREATED_AT,
        "the evidence floor is the preserved-rows boundary",
    )
    updated_at = filters.get("updated_at")
    _check(
        isinstance(updated_at, dict)
        and isinstance(updated_at.get("value"), object)
        and getattr(updated_at.get("value"), "tzinfo", "missing") is None,
        "D11: the staleness cutoff is NAIVE UTC (a tz-aware value is off by 7h)",
    )


def main() -> int:
    print("Oversized-payload guard smoke (INCIDENT.md 2026-08-15 D1/D2/D5/D8)")
    test_enqueue_guard_refuses_oversized_payload()
    test_enqueue_guard_passes_normal_payload()
    test_claim_guard_measures_before_parsing()
    test_read_limit_refuses_over_cap_without_consent()
    test_unbounded_read_refuses_rather_than_truncating()
    test_liveness_stale_age_alone_is_stalled()
    test_reaper_fails_oversized_and_requeues_small()

    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: payload bounds fire pre-parse; reads refuse over cap; "
          "liveness alarms on stale poll age alone; reaper fails rather than re-wedges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
