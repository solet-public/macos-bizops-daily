#!/usr/bin/env python3
"""Singleton drain-until-empty auto-summarizer smoke (no solet/Postgres/LM Studio).

Covers the singleton drain design (Coordinator-Dawn assignment, 2026-07-02) that
replaced the bounded ≤5-per-fire pass — the fix that eats the ~10k-session
summary backlog:

  * drain-until-empty: one drain processes every SUMMARIZABLE eligible session
    (paged), and a second drain over the drained repo is a no-op.
  * COVERAGE (Reviewer-C BLOCKER fix): with newest-first ordering + no offset, a
    full page of DETERMINISTIC no-content skips at the head would pin the page and
    strand older backlog forever. The fix sentinel-marks those skips (empty
    timeline; all-blob-offloaded / empty transcript) so they LEAVE eligibility —
    proven by a buried-summarizable test with batch_size < (#skips ahead of it).
  * termination (liveness): a TRANSIENT inference-empty skip is deliberately NOT
    marked (stays eligible so a recovered backend re-picks it) — the in-drain
    ``attempted`` set stops it from spinning the loop forever. Would HANG without it.
  * sentinel/marked-trivial rows are excluded and never re-picked.
  * per-session error isolation: one raising session does not kill the drain.
  * singleton: overlapping cron fires → exactly one drainer runs
    (``BoundedSemaphore(1)`` guard); a second fire no-ops with
    ``{"drainer": "already_running"}``.
  * WI-0: the cron entry does ZERO summarization on the calling (action-queue)
    thread — proven both at runtime (thread-identity) and statically (AST).

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/quiescent_drain_smoke.py
"""

from __future__ import annotations

import ast
import inspect
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

# The per-session error-isolation + persistent-skip cases deliberately trigger a
# caught exception log + a STALL warning. Silence them so the smoke output is
# clean — the assertions, not the logs, prove the behavior. Both live in
# summarize.py post schema-debt service.py decomposition (2026-08-07).
logging.getLogger("ananta.services.session_ledger_service.summarize").setLevel(
    logging.CRITICAL,
)

from ananta.services.session_ledger_service import summarize as svc_mod  # noqa: E402
from ananta.services.session_ledger_service.service import (  # noqa: E402
    SessionLedgerService,
)
from ananta.services.session_ledger_service.summarize import (  # noqa: E402
    _AUTO_SUMMARIZE_TRIVIAL_SENTINEL,
)
from ananta.services.session_ledger_service.summary_executor import (  # noqa: E402
    BoundedSummaryExecutor,
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


# ─── Faithful fakes (model the idempotency EXCLUSION the real DB enforces) ─────


def _non_trivial_timeline(session_id: str) -> list[dict[str, object]]:
    """4 events with ≥1 assistant turn — clears the trivial floor."""
    return [
        {"event_type": "message", "role": "user", "content_text": f"hi {session_id}"},
        {"event_type": "message", "role": "assistant", "content_text": f"yo {session_id}"},
        {"event_type": "message", "role": "user", "content_text": f"more {session_id}"},
        {"event_type": "message", "role": "assistant", "content_text": f"end {session_id}"},
    ]


def _blob_timeline() -> list[dict[str, object]]:
    """4 conversation events (≥1 assistant) with content_text=None — PASSES the
    trivial floor (role-counted) but assembles to an EMPTY transcript because
    _assemble_transcript skips null content. Models a fully blob-offloaded
    (large/substantive) session: the deterministic empty-transcript skip path."""
    return [
        {"event_type": "message", "role": "user", "content_text": None},
        {"event_type": "message", "role": "assistant", "content_text": None},
        {"event_type": "message", "role": "user", "content_text": None},
        {"event_type": "message", "role": "assistant", "content_text": None},
    ]


class _DrainRepo:
    """Repository fake that models the DONE exclusion (``__summary`` row OR
    trivial sentinel) so the drain terminates via the clean path, exactly as the
    real ``list_quiescent_sessions`` would. ``done`` is SHARED with the writer:
    a pushed summary marks the session done for the next list."""

    def __init__(
        self,
        sessions: list[dict[str, Any]],
        done: set[str],
        *,
        away: dict[str, str] | None = None,
        timelines: dict[str, list[dict[str, object]]] | None = None,
        timeline_raises: set[str] | None = None,
    ) -> None:
        self._sessions = [dict(s) for s in sessions]
        self.done = done
        self._away = dict(away or {})
        self._timelines = dict(timelines or {})
        self._timeline_raises = set(timeline_raises or set())
        self.calls_list_quiescent: list[dict[str, Any]] = []

    def list_quiescent_sessions(
        self, *, quiescence_minutes: int, limit: int, trivial_sentinel: str,
    ) -> list[dict[str, object]]:
        self.calls_list_quiescent.append(
            {"quiescence_minutes": quiescence_minutes, "limit": limit,
             "trivial_sentinel": trivial_sentinel},
        )
        eligible = [
            dict(s) for s in self._sessions
            if str(s["id"]) not in self.done
            and s.get("summary_text") != trivial_sentinel
        ]
        return eligible[:limit]

    def get_session_timeline(
        self, *, session_id: str, after_sequence: int, limit: int,
    ) -> list[dict[str, object]]:
        _ = after_sequence
        _ = limit
        if session_id in self._timeline_raises:
            raise RuntimeError(f"simulated timeline read failure for {session_id}")
        if session_id in self._timelines:
            return [dict(e) for e in self._timelines[session_id]]
        return _non_trivial_timeline(session_id)

    def find_latest_away_summary_for_session(self, session_id: str) -> str | None:
        return self._away.get(session_id)

    def mark_session_summary_text(self, *, session_id: str, summary_text: str) -> None:
        _ = summary_text
        self.done.add(session_id)  # sentinel written → excluded from the next list


class _DrainWriter:
    """Push sink; a pushed summary marks the session DONE in the shared set."""

    def __init__(self, done: set[str]) -> None:
        self.done = done
        self.pushes: list[dict[str, Any]] = []
        self.push_threads: list[int] = []

    def push_summary_chunk(
        self, *, session_id: str, chunk_index: int,
        summary_text: str, generated_by_client_id: str,
    ) -> dict[str, Any]:
        self.pushes.append({
            "session_id": session_id, "chunk_index": chunk_index,
            "summary_text": summary_text,
            "generated_by_client_id": generated_by_client_id,
        })
        self.push_threads.append(threading.get_ident())
        self.done.add(session_id)  # __summary row exists → excluded from the next list
        return {"summary_id": "sum-fixture", "embedding_vector_id": "ev-fixture"}


class _StubInference:
    """Canned completions. ``return_empty_for`` exercises the persistent-skip
    (empty completion → no push → never DONE) path; ``raise_for`` exercises the
    provider-raises path (caught inside ``_call_inference_chat`` → None →
    ``"skipped"`` — a DIFFERENT try/except than a raise in ``_summarize_one_session``)."""

    def __init__(
        self, *, return_empty_for: set[str] | None = None,
        raise_for: set[str] | None = None,
    ) -> None:
        self._return_empty_for = set(return_empty_for or set())
        self._raise_for = set(raise_for or set())
        self.calls = 0

    def generate_completion(self, request: Any) -> dict[str, Any]:
        self.calls += 1
        messages = list(getattr(request, "messages", []) or [])
        last = messages[-1]["content"] if messages else ""
        if any(token in last for token in self._raise_for):
            raise RuntimeError(f"simulated provider failure: {last[:30]}")
        if any(token in last for token in self._return_empty_for):
            return {"action_status": "completed", "data": {"result": {"completion": ""}}}
        return {
            "action_status": "completed",
            "data": {"result": {"completion": f"Stub summary: {last[:40]}"}},
        }


def _make_service(
    *, repository: Any, summary_writer: Any, inference_service: Any,
    summary_executor: Any = None,
) -> SessionLedgerService:
    """Construct a SessionLedgerService stand-in bypassing heavy __init__."""
    instance = SessionLedgerService.__new__(SessionLedgerService)
    instance._repository = repository  # type: ignore[assignment]
    instance._summary_writer = summary_writer  # type: ignore[assignment]
    instance._inference_service = inference_service
    instance._summary_executor = summary_executor  # type: ignore[assignment]
    return instance


def _seed(sid: str, **extra: Any) -> dict[str, Any]:
    return {"id": sid, "source_id": "src-1", "summary_text": None, **extra}


# ─── Cases ─────────────────────────────────────────────────────────────────


def test_drain_until_empty_then_second_drain_noops() -> None:
    """One drain processes EVERY eligible session (paged small); a second drain
    over the now-drained repo examines nothing."""
    done: set[str] = set()
    sessions = [_seed(f"les-{i:02d}") for i in range(5)]  # all inference-path
    repo = _DrainRepo(sessions, done)
    writer = _DrainWriter(done)
    service = _make_service(
        repository=repo, summary_writer=writer, inference_service=_StubInference(),
    )

    result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=2)
    _check(result["sessions_examined"] == 5, f"drained all 5 (got {result})")
    _check(result["sessions_summarized"] == 5, f"all 5 summarized (got {result})")
    _check(len(writer.pushes) == 5, f"one push per session (got {len(writer.pushes)})")
    _check(
        len(repo.calls_list_quiescent) >= 3,
        f"paged the backlog (batch_size=2 over 5 → >=3 list calls; "
        f"got {len(repo.calls_list_quiescent)})",
    )
    first = repo.calls_list_quiescent[0]
    _check(
        first["quiescence_minutes"] == 10 and first["limit"] == 2
        and "trivial" in first["trivial_sentinel"].lower(),
        f"list_quiescent called with drain args (got {first})",
    )

    result2 = service._drain_all_quiescent(quiescence_minutes=10, batch_size=2)
    _check(
        result2["sessions_examined"] == 0 and len(writer.pushes) == 5,
        f"second drain is a no-op — nothing eligible remains (got {result2})",
    )


def test_transient_inference_empty_skip_terminates_and_stays_eligible() -> None:
    """Transient case: an inference-EMPTY session is a ``skipped`` outcome that is
    DELIBERATELY not sentinel-marked (so a recovered backend re-picks it). It
    stays eligible, so without the attempted-set the drain would spin forever;
    WITH it the drain terminates. les-empty must stay ELIGIBLE (not in ``done``) —
    that's the transient-vs-deterministic distinction."""
    done: set[str] = set()
    sessions = [_seed("les-empty"), _seed("les-ok")]
    repo = _DrainRepo(sessions, done)
    writer = _DrainWriter(done)
    service = _make_service(
        repository=repo, summary_writer=writer,
        inference_service=_StubInference(return_empty_for={"les-empty"}),
    )

    result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
    _check(
        result["sessions_examined"] == 2 and result["sessions_summarized"] == 1
        and result["sessions_skipped"] == 1,
        f"les-ok summarized, les-empty skipped, each attempted once (got {result})",
    )
    _check(
        "les-empty" not in done,
        "transient inference-empty stays ELIGIBLE (not marked) → re-picked later",
    )
    _check(
        len(repo.calls_list_quiescent) <= 2,
        f"bounded list calls — attempted-set breaks the stall, no spin "
        f"(got {len(repo.calls_list_quiescent)})",
    )


def test_deterministic_skips_are_sentinel_marked() -> None:
    """The two DETERMINISTIC no-content skips LEAVE eligibility (sentinel-marked →
    marked_trivial): an empty timeline (no-events) and an all-blob-offloaded
    transcript (≥4 role-events, content_text NULL → assembles empty). A TRANSIENT
    inference-empty skip is NOT marked (stays eligible). This is the core of the
    Reviewer-C fix: deterministic no-content = mark; transient = re-pick."""
    done: set[str] = set()
    sessions = [_seed("les-noevents"), _seed("les-blob"), _seed("les-infempty")]
    repo = _DrainRepo(
        sessions, done,
        timelines={"les-noevents": [], "les-blob": _blob_timeline()},
    )
    writer = _DrainWriter(done)
    service = _make_service(
        repository=repo, summary_writer=writer,
        inference_service=_StubInference(return_empty_for={"les-infempty"}),
    )

    result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
    _check(
        result["sessions_marked_trivial"] == 2 and result["sessions_skipped"] == 1,
        f"no-events + blob-empty-transcript marked; inference-empty stays skipped "
        f"(got {result})",
    )
    _check(
        "les-noevents" in done and "les-blob" in done,
        "both deterministic no-content skips LEFT eligibility (sentinel-marked)",
    )
    _check(
        "les-infempty" not in done,
        "transient inference-empty stays ELIGIBLE (re-picked on a later drain)",
    )
    _check(writer.pushes == [], f"nothing summarized (got {writer.pushes})")


def test_buried_summarizable_reached_past_a_full_skip_page() -> None:
    """Reviewer-C BLOCKER: with newest-first ordering + no offset, a full page of
    deterministic-skip sessions at the head would pin the page and strand older
    summarizable sessions FOREVER. The sentinel-mark fix makes the skips leave
    eligibility so the drain advances and reaches the buried session even when
    batch_size < (#skips ahead of it). The old fakes all used batch_size ≥ total
    — this covers exactly that blind spot. WITHOUT the fix this drain summarizes
    nothing (the buried session is never reached)."""
    done: set[str] = set()
    skips = [_seed(f"les-skip-{i}") for i in range(3)]  # 3 no-content skips at HEAD
    buried = _seed("les-buried")                        # summarizable, ordered behind
    sessions = [*skips, buried]
    repo = _DrainRepo(
        sessions, done, timelines={s["id"]: [] for s in skips},  # empty-timeline skips
    )
    writer = _DrainWriter(done)
    service = _make_service(
        repository=repo, summary_writer=writer, inference_service=_StubInference(),
    )

    # batch_size=2 < 3 skips ahead of the buried session — the OLD build pins here.
    result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=2)
    _check(
        result["sessions_marked_trivial"] == 3,
        f"the 3 head skips were sentinel-marked (leave eligibility) (got {result})",
    )
    _check(
        result["sessions_summarized"] == 1
        and {p["session_id"] for p in writer.pushes} == {"les-buried"},
        f"the buried summarizable session is REACHED past the full skip-page "
        f"(got {result}, {[p['session_id'] for p in writer.pushes]})",
    )
    _check(
        all(str(s["id"]) in done for s in skips),
        "each skip is marked DONE so it can never pin the head page again",
    )


def test_trivial_sentinel_excluded_and_not_repicked() -> None:
    """A row already marked with the trivial sentinel is excluded from listing;
    a freshly-trivial session is marked and then excluded — the drain
    terminates cleanly, never re-picking either."""
    done: set[str] = set()
    sessions = [
        _seed("les-already-trivial", summary_text=_AUTO_SUMMARIZE_TRIVIAL_SENTINEL),
        _seed("les-fresh-trivial"),
    ]
    repo = _DrainRepo(
        sessions, done,
        timelines={"les-fresh-trivial": [
            {"event_type": "message", "role": "user", "content_text": "one line"},
        ]},
    )
    writer = _DrainWriter(done)
    service = _make_service(
        repository=repo, summary_writer=writer, inference_service=_StubInference(),
    )

    result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
    _check(
        result["sessions_examined"] == 1,
        f"pre-marked sentinel row never listed; only the fresh one examined "
        f"(got {result})",
    )
    _check(
        result["sessions_marked_trivial"] == 1 and writer.pushes == [],
        f"fresh trivial marked, nothing pushed (got {result})",
    )
    _check(
        "les-fresh-trivial" in done,
        "freshly-trivial session marked DONE so it is not re-picked",
    )


def test_per_session_error_isolation() -> None:
    """Two DISTINCT failure modes are each isolated as ``skipped`` and the drain
    finishes the healthy rest: a raise in ``_summarize_one_session`` (timeline
    read) is caught by ``_summarize_row``; a raise in ``generate_completion`` is
    caught inside ``_call_inference_chat`` (→ None → ``"skipped"``)."""
    done: set[str] = set()
    sessions = [_seed("les-a"), _seed("les-boom"), _seed("les-infboom"), _seed("les-c")]
    repo = _DrainRepo(sessions, done, timeline_raises={"les-boom"})
    writer = _DrainWriter(done)
    service = _make_service(
        repository=repo, summary_writer=writer,
        inference_service=_StubInference(raise_for={"les-infboom"}),
    )

    result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
    _check(
        result["sessions_examined"] == 4 and result["sessions_summarized"] == 2
        and result["sessions_skipped"] == 2,
        f"both raising sessions (timeline-raise + provider-raise) isolated; "
        f"the other two summarized (got {result})",
    )
    _check(
        {p["session_id"] for p in writer.pushes} == {"les-a", "les-c"},
        f"only the healthy sessions pushed (got {[p['session_id'] for p in writer.pushes]})",
    )


def test_entry_raises_when_inference_unbound() -> None:
    """The cron entry fails fast (does not start a drain) with no inference."""
    service = _make_service(
        repository=_DrainRepo([], set()), summary_writer=_DrainWriter(set()),
        inference_service=None,
    )
    raised = False
    try:
        service.summarize_quiescent_sessions()
    except RuntimeError:
        raised = True
    _check(raised, "summarize_quiescent_sessions raises when inference unbound")


def test_singleton_second_fire_noops_while_drainer_running() -> None:
    """With the REAL executor, a second cron fire while a drainer holds the
    single slot returns already_running; the slot frees once the drain ends."""
    entered = threading.Event()
    release = threading.Event()

    class _BlockingRepo(_DrainRepo):
        def list_quiescent_sessions(self, **kw: Any) -> list[dict[str, object]]:
            entered.set()
            release.wait(timeout=5.0)  # park the drain thread holding the slot
            return super().list_quiescent_sessions(**kw)

    done: set[str] = set()
    repo = _BlockingRepo([_seed("les-1")], done)
    writer = _DrainWriter(done)
    service = _make_service(
        repository=repo, summary_writer=writer, inference_service=_StubInference(),
        summary_executor=BoundedSummaryExecutor(name="drain-singleton-test"),
    )

    first = service.summarize_quiescent_sessions(batch_size=5)
    _check(first["drainer"] == "started", f"first fire starts the drainer (got {first})")
    _check(entered.wait(timeout=2.0), "drainer thread entered the drain (holds the slot)")

    second = service.summarize_quiescent_sessions(batch_size=5)
    _check(
        second["drainer"] == "already_running",
        f"second fire no-ops while a drainer holds the slot (got {second})",
    )

    release.set()
    freed = False
    for _ in range(60):
        if service.summarize_quiescent_sessions(batch_size=5)["drainer"] == "started":
            freed = True
            break
        time.sleep(0.05)
    _check(freed, "slot frees for a fresh drainer once the drain completes")
    release.set()


def test_wi0_no_summarization_on_the_calling_thread() -> None:
    """WI-0 runtime guard: with the REAL executor, the cron entry returns without
    doing any push on the calling thread; the drain's pushes all land on a
    DIFFERENT (daemon) thread."""
    done: set[str] = set()
    repo = _DrainRepo([_seed("les-x"), _seed("les-y")], done)
    writer = _DrainWriter(done)
    service = _make_service(
        repository=repo, summary_writer=writer, inference_service=_StubInference(),
        summary_executor=BoundedSummaryExecutor(name="drain-wi0-test"),
    )
    calling_thread = threading.get_ident()

    result = service.summarize_quiescent_sessions(batch_size=5)
    _check(result["drainer"] == "started", f"entry returns started (got {result})")
    _check(
        writer.push_threads == [] or all(t != calling_thread for t in writer.push_threads),
        "no summary pushed on the calling thread at entry-return time",
    )

    for _ in range(100):  # let the daemon drain finish
        if len(writer.pushes) >= 2:
            break
        time.sleep(0.05)
    _check(len(writer.pushes) == 2, f"drain completed off-thread (got {len(writer.pushes)})")
    _check(
        writer.push_threads and all(t != calling_thread for t in writer.push_threads),
        "every push ran on a background thread, never the calling thread",
    )


def test_wi0_static_entry_submits_without_inline_summarization() -> None:
    """WI-0 static guard (AST): the cron entry ``summarize_quiescent_sessions``
    must dispatch via ``_summary_executor.submit`` and contain NO inline
    summarization; the inline synchronous inference lives in
    ``_summarize_one_session`` (which runs only on the drain thread)."""
    src = Path(inspect.getfile(svc_mod)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    bodies: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "summarize_quiescent_sessions", "_summarize_one_session",
        ):
            bodies[node.name] = ast.get_source_segment(src, node) or ""

    entry = bodies.get("summarize_quiescent_sessions", "")
    one = bodies.get("_summarize_one_session", "")
    _check(entry and one, "both methods located in summarize.py")
    _check(
        "_summary_executor.submit" in entry,
        "entry dispatches the drain via _summary_executor.submit",
    )
    _check(
        "_request_inference_summary(" not in entry
        and "generate_completion" not in entry
        and "push_summary_chunk" not in entry,
        "entry does NO inline summarization (WI-0: never on the action queue)",
    )
    _check(
        "_request_inference_summary(" in one,
        "inline synchronous inference lives in _summarize_one_session (drain thread)",
    )


def main() -> int:
    print("=== quiescent_drain_smoke (singleton drain-until-empty, 2026-07-02) ===")
    test_drain_until_empty_then_second_drain_noops()
    test_transient_inference_empty_skip_terminates_and_stays_eligible()
    test_deterministic_skips_are_sentinel_marked()
    test_buried_summarizable_reached_past_a_full_skip_page()
    test_trivial_sentinel_excluded_and_not_repicked()
    test_per_session_error_isolation()
    test_entry_raises_when_inference_unbound()
    test_singleton_second_fire_noops_while_drainer_running()
    test_wi0_no_summarization_on_the_calling_thread()
    test_wi0_static_entry_submits_without_inline_summarization()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
