#!/usr/bin/env python3
"""F-AISLOP timestamp-fragility unification smoke — value-compare + fail-loud.

The AI code-vetting suite's ai_slop lens found ONE lexical ISO-8601 timestamp
idiom copy-pasted across three durable-queue serve-timeout / GC sites, with
false-mirror docstrings and a diverged missing-stamp default. All three now
compare timestamp VALUES via the canonical ``ananta.core.domain.timestamps``
``to_naive_utc`` helper (no lexical fallback), so they are correct regardless of
whether the stored cell round-trips NAIVE (a ``timestamp without time zone`` /
``ColumnType.DATETIME`` column, offset stripped) or AWARE (a ``ColumnType.TEXT``
column), and fail LOUD on an unparseable stamp.

RED-FIRST design — every value-compare case below is chosen so a NAIVE stored
stamp EQUAL to the tz-aware cutoff instant discriminates value-vs-lexical:

  value:   to_naive_utc('…T12:00:00') == to_naive_utc('…T12:00:00+00:00')  → NOT before
  lexical: '…T12:00:00' < '…T12:00:00+00:00'  → True (the shorter naive spelling
           is a prefix of the aware one, so it sorts BEFORE) — the WRONG answer.

Reverting any site to the lexical compare turns its boundary case (and its
fail-loud case) RED. The three sites:

  S1  forwarded_vertex_reconcile.terminal_row_aged  (GC/DELETE; updated_at DATETIME
      → naive)          — missing default False (never-delete-on-unknown-age).
  S2  deferred_vertex_queue.forwarded_before        (SWEEP/RE-DRIVE; forwarded_at
      DATETIME → naive) — missing default True (surface-on-anomaly; this default
      is the ONE real behavior fix — it was False = silent-stall).
  S3  completion_request_queue.forwarded_before      (SWEEP/RE-QUEUE; forwarded_at
      TEXT → aware)     — missing default True (surface-on-anomaly, unchanged).

The two sweep defaults (True) and the GC default (False) DIVERGE on purpose: a
re-drive is bounded + recoverable, a delete is not — 'honest and different' beats
'uniform and lying'.

Offline / hermetic: pure predicate calls on plain dict rows, plus one integrated
``ForwardedVertexReconciler.gc_terminal_rows`` drive against the shared REAL-SHAPE
state fake to prove ``terminal_row_aged`` is actually WIRED into the reap loop. No
live solet / LM Studio / DB. Needs SOLET_NAME set (the plugin package init
resolves vault-scoped constants eagerly) — no default, raises if unset.

Run from repo root:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/timestamp_value_compare_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.services.inference_service.completion_request_queue import (  # noqa: E402
    forwarded_before as completion_forwarded_before,
)
from ananta.services.inference_service.completion_request_schema import (  # noqa: E402
    COL_FORWARDED_AT as COL_FWD_COMPLETION,
)
from ananta.services.inference_service.deferred_vertex_queue import (  # noqa: E402
    forwarded_before as deferred_forwarded_before,
)
from ananta.services.inference_service.schema import (  # noqa: E402
    COL_FLOW_ID,
    COL_METHOD,
    COL_STATE,
    INFERENCE_DEFERRED_VERTEX_NAMESPACE,
    METHOD_PROCESS_RESULTS,
    STATE_FAILED,
    STATE_FORWARDED,
    TABLE_INFERENCE_DEFERRED_VERTEX,
)
from ananta.services.inference_service.schema import (  # noqa: E402
    COL_FORWARDED_AT as COL_FWD_DEFERRED,
)

from agent_messaging_plugin.forwarded_vertex_reconcile import (  # noqa: E402
    COL_UPDATED_AT,
    ForwardedVertexReconciler,
    terminal_row_aged,
)

# The tz-aware cutoff spelling (as ``datetime.now(UTC).isoformat()`` produces).
CUTOFF_AWARE = "2026-07-20T12:00:00+00:00"
# A NAIVE stored cell for the SAME instant (as a DATETIME column round-trips it).
# Lexical '<' cutoff → True (prefix); by VALUE it is NOT before → the discriminator.
STAMP_NAIVE_EQ = "2026-07-20T12:00:00"
STAMP_NAIVE_BEFORE = "2026-07-20T11:59:59"  # strictly before the cutoff instant
STAMP_NAIVE_AFTER = "2026-07-20T12:00:01"  # strictly after the cutoff instant
STAMP_MALFORMED = "not-a-timestamp"  # present but unparseable → must fail loud
FAR_PAST_NAIVE = "2020-01-01T00:00:00"  # unambiguously aged (integrated GC drive)
FAR_FUTURE_NAIVE = "2099-01-01T00:00:00"  # unambiguously fresh (integrated GC drive)

_passed = 0
_failed: list[str] = []


def _check(cond: bool, label: str) -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  ok  {label}")
    else:
        _failed.append(label)
        print(f"  XX  {label}")


def _raises_loud(fn: Callable[[], object], label: str) -> None:
    """A present-but-unparseable stamp must fail LOUD (to_naive_utc raises)."""
    global _passed
    try:
        fn()
    except (ValueError, TypeError):
        _passed += 1
        print(f"  ok  {label}")
        return
    _failed.append(label)
    print(f"  XX  {label} — did NOT raise (lexical fallback)")


def test_deferred_forwarded_before() -> None:
    print("S2 — deferred_vertex_queue.forwarded_before (SWEEP/RE-DRIVE):")
    _check(
        deferred_forwarded_before(
            {COL_FWD_DEFERRED: STAMP_NAIVE_EQ}, cutoff_iso=CUTOFF_AWARE,
        )
        is False,
        "S2 value-compare: naive stamp == cutoff instant is NOT before "
        "(lexical would spuriously say True → re-drive)",
    )
    _check(
        deferred_forwarded_before(
            {COL_FWD_DEFERRED: STAMP_NAIVE_BEFORE}, cutoff_iso=CUTOFF_AWARE,
        )
        is True,
        "S2 value-compare: naive stamp strictly before cutoff is timed out",
    )
    _check(
        deferred_forwarded_before(
            {COL_FWD_DEFERRED: STAMP_NAIVE_AFTER}, cutoff_iso=CUTOFF_AWARE,
        )
        is False,
        "S2 value-compare: naive stamp strictly after cutoff is NOT timed out",
    )
    _check(
        deferred_forwarded_before({}, cutoff_iso=CUTOFF_AWARE) is True,
        "S2 missing stamp surfaces-on-anomaly (True) — the silent-stall "
        "default fix (was False)",
    )
    _raises_loud(
        lambda: deferred_forwarded_before(
            {COL_FWD_DEFERRED: STAMP_MALFORMED}, cutoff_iso=CUTOFF_AWARE,
        ),
        "S2 malformed present stamp fails loud",
    )


def test_completion_forwarded_before() -> None:
    print("S3 — completion_request_queue.forwarded_before (SWEEP/RE-QUEUE):")
    _check(
        completion_forwarded_before(
            {COL_FWD_COMPLETION: STAMP_NAIVE_EQ}, cutoff_iso=CUTOFF_AWARE,
        )
        is False,
        "S3 value-compare: naive stamp == cutoff instant is NOT before "
        "(now robust to a naive cell; lexical would say True)",
    )
    _check(
        completion_forwarded_before(
            {COL_FWD_COMPLETION: STAMP_NAIVE_BEFORE}, cutoff_iso=CUTOFF_AWARE,
        )
        is True,
        "S3 value-compare: naive stamp strictly before cutoff is timed out",
    )
    _check(
        completion_forwarded_before({}, cutoff_iso=CUTOFF_AWARE) is True,
        "S3 missing stamp surfaces-on-anomaly (True) — correct-for-consequence "
        "(unchanged)",
    )
    _raises_loud(
        lambda: completion_forwarded_before(
            {COL_FWD_COMPLETION: STAMP_MALFORMED}, cutoff_iso=CUTOFF_AWARE,
        ),
        "S3 malformed present stamp fails loud (was a silent lexical bool)",
    )


def test_terminal_row_aged() -> None:
    print("S1 — forwarded_vertex_reconcile.terminal_row_aged (GC/DELETE) predicate:")
    _check(
        terminal_row_aged(
            {COL_UPDATED_AT: STAMP_NAIVE_EQ}, cutoff_iso=CUTOFF_AWARE,
        )
        is False,
        "S1 value-compare: naive updated_at == cutoff instant is NOT aged "
        "(lexical would spuriously reap the boundary row)",
    )
    _check(
        terminal_row_aged(
            {COL_UPDATED_AT: STAMP_NAIVE_BEFORE}, cutoff_iso=CUTOFF_AWARE,
        )
        is True,
        "S1 value-compare: naive updated_at strictly before cutoff is aged",
    )
    _check(
        terminal_row_aged({}, cutoff_iso=CUTOFF_AWARE) is False,
        "S1 missing updated_at is never-delete-on-unknown-age (False) — the "
        "GC default DIVERGES from the sweeps on purpose",
    )
    _raises_loud(
        lambda: terminal_row_aged(
            {COL_UPDATED_AT: STAMP_MALFORMED}, cutoff_iso=CUTOFF_AWARE,
        ),
        "S1 malformed present updated_at fails loud",
    )


def _seed_failed(state: RealShapeState, flow_id: str, *, updated_at: str) -> None:
    state.upsert_state(
        INFERENCE_DEFERRED_VERTEX_NAMESPACE,
        {
            "table": TABLE_INFERENCE_DEFERRED_VERTEX,
            "record": {
                COL_FLOW_ID: flow_id,
                COL_METHOD: METHOD_PROCESS_RESULTS,
                COL_STATE: STATE_FAILED,
                COL_UPDATED_AT: updated_at,
            },
            "conflict_columns": [COL_FLOW_ID],
        },
    )


def _seed_forwarded(state: RealShapeState, flow_id: str) -> None:
    state.upsert_state(
        INFERENCE_DEFERRED_VERTEX_NAMESPACE,
        {
            "table": TABLE_INFERENCE_DEFERRED_VERTEX,
            "record": {
                COL_FLOW_ID: flow_id,
                COL_METHOD: METHOD_PROCESS_RESULTS,
                COL_STATE: STATE_FORWARDED,
                COL_FWD_DEFERRED: FAR_PAST_NAIVE,
            },
            "conflict_columns": [COL_FLOW_ID],
        },
    )


def _row(state: RealShapeState, flow_id: str) -> dict[str, object] | None:
    rows = state._rows.get(  # noqa: SLF001 — smoke reaches into the fake's store
        (INFERENCE_DEFERRED_VERTEX_NAMESPACE, TABLE_INFERENCE_DEFERRED_VERTEX), [],
    )
    for r in rows:
        if r.get(COL_FLOW_ID) == flow_id and not r.get("is_deleted"):
            return r
    return None


def test_gc_terminal_rows_wired() -> None:
    print("S1 — gc_terminal_rows integrated drive (predicate WIRED into reap loop):")
    state = RealShapeState()
    _seed_failed(state, "flow-aged", updated_at=FAR_PAST_NAIVE)
    _seed_failed(state, "flow-fresh", updated_at=FAR_FUTURE_NAIVE)
    _seed_forwarded(state, "flow-live")  # non-failed → never GC-considered

    reconciler = ForwardedVertexReconciler(
        state=lambda: state,
        resubmit_vertex=lambda _flow, _method: True,
        serve_window_seconds=0,
        attempts_cap=5,
        terminal_gc_after_seconds=0,
    )
    reaped = reconciler.gc_terminal_rows()
    _check(
        reaped == 1 and _row(state, "flow-aged") is None,
        "S1 gc reaps the aged 'failed' row (value-compares its naive updated_at)",
    )
    _check(
        _row(state, "flow-fresh") is not None,
        "S1 gc keeps the far-future 'failed' row (NOT aged by value)",
    )
    _check(
        (lambda r: r is not None and r.get(COL_STATE) == STATE_FORWARDED)(
            _row(state, "flow-live"),
        ),
        "S1 gc leaves the non-'failed' forwarded row untouched",
    )


def main() -> int:
    test_deferred_forwarded_before()
    test_completion_forwarded_before()
    test_terminal_row_aged()
    test_gc_terminal_rows_wired()
    total = _passed + len(_failed)
    print(f"\n{_passed}/{total} checks passed")
    if _failed:
        print("FAILURES:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
