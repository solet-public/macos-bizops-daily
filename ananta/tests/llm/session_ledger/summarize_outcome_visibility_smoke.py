#!/usr/bin/env python3
"""iss_b535985b fix smoke: per-branch outcome counters, loud WARNINGs, and the
real ``macos_inference_plugin`` envelope shape on the inference branch.

Standing guidance: provider/backend outages fail LOUDLY, never silently. Before
this fix, ``_drain_all_quiescent`` collapsed every non-summarized outcome into
one ``"skipped"`` bucket and logged only an INFO line — a permanently
DETERMINISTIC failure (e.g. iss_8b8a970b's cap-refusal, confirmed live 420
times 2026-09-24..27) and a genuinely TRANSIENT one (inference backend briefly
empty) were indistinguishable from the aggregate, and neither produced
anything above INFO. This smoke covers the fix:

1. ``_drain_all_quiescent`` now counts ``errored`` (an exception caught by
   ``_summarize_row``) and ``skipped_empty`` (inference returned no usable
   text) SEPARATELY, and logs a WARNING line whenever either is nonzero — a
   clean drain (both zero) logs no such WARNING.
2. A session that fails with the SAME deterministic error across consecutive
   drains gets a per-session WARNING naming "same failure N consecutive
   drains" (module-level ``_CONSECUTIVE_ERROR_COUNTS``, reset once that
   session's outcome is no longer ``"errored"``) — a permanently-wedged
   session is visible without grepping tracebacks, and it is deliberately
   NEVER sentinel-marked (that would misrepresent a real failure as an
   assessed-trivial session).
3. An empty inference completion gets a per-session WARNING naming the
   provider's actual result-envelope shape (``_describe_result_shape``) — the
   piece needed to diagnose a real extractor/provider mismatch, which the old
   aggregate ``"skipped"`` count could not surface at all.
4. The inference branch (branch 3) is proven against the REAL result shape
   ``macos_inference_plugin``'s Apple FM provider returns — read directly
   from ``plugins/macos_inference_plugin/src/macos_inference_plugin/providers/
   apple_fm_provider.py:376-395`` (``{"action_status": "completed", "data":
   {"result": {"completion": ..., "model": ..., "provider": "apple_fm", ...}},
   "error": None, "timestamp": ...}``) — not an idealized stub shape. Confirms
   ``_extract_summary_text`` accepts it via the canonical
   ``_extract_completion_field`` (``data.result.completion``) extractor and
   the drain reaches ``"inferred"`` end to end.

Run::

    .venv/bin/python3 ananta/tests/llm/session_ledger/summarize_outcome_visibility_smoke.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.session_ledger_service import summarize as svc_mod  # noqa: E402
from ananta.services.session_ledger_service.service import (  # noqa: E402
    SessionLedgerService,
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


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, *, level: int = logging.WARNING) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= level]


class _DrainRepo:
    """Same faithful fake shape as ``quiescent_drain_smoke.py``'s — minimal
    subset needed here."""

    def __init__(
        self, sessions: list[dict[str, Any]], done: set[str],
        *, timeline_raises: set[str] | None = None,
    ) -> None:
        self._sessions = [dict(s) for s in sessions]
        self.done = done
        self._timeline_raises = set(timeline_raises or set())

    def list_quiescent_sessions(
        self, *, quiescence_minutes: int, limit: int, trivial_sentinel: str,
    ) -> list[dict[str, object]]:
        _ = quiescence_minutes
        eligible = [
            dict(s) for s in self._sessions
            if str(s["id"]) not in self.done
            and s.get("summary_text") != trivial_sentinel
        ]
        return eligible[:limit]

    def get_session_timeline(
        self, *, session_id: str, after_sequence: int, limit: int,
    ) -> list[dict[str, object]]:
        _ = after_sequence, limit
        if session_id in self._timeline_raises:
            raise RuntimeError(f"simulated timeline read failure for {session_id}")
        return [
            {"event_type": "message", "role": "user", "content_text": f"hi {session_id}"},
            {"event_type": "message", "role": "assistant", "content_text": f"yo {session_id}"},
            {"event_type": "message", "role": "user", "content_text": f"more {session_id}"},
            {"event_type": "message", "role": "assistant", "content_text": f"end {session_id}"},
        ]

    def find_latest_away_summary_for_session(self, session_id: str) -> str | None:
        _ = session_id
        return None

    def mark_session_summary_text(self, *, session_id: str, summary_text: str) -> None:
        _ = summary_text
        self.done.add(session_id)


class _DrainWriter:
    def __init__(self, done: set[str]) -> None:
        self.done = done
        self.pushes: list[dict[str, Any]] = []

    def push_summary_chunk(
        self, *, session_id: str, chunk_index: int,
        summary_text: str, generated_by_client_id: str,
    ) -> dict[str, Any]:
        self.pushes.append({
            "session_id": session_id, "chunk_index": chunk_index,
            "summary_text": summary_text,
            "generated_by_client_id": generated_by_client_id,
        })
        self.done.add(session_id)
        return {"summary_id": "sum-fixture", "embedding_vector_id": "ev-fixture"}


class _StubInference:
    def __init__(
        self, *, empty_shape_for: dict[str, Any] | None = None,
        raise_for: set[str] | None = None,
        real_shape_for: set[str] | None = None,
    ) -> None:
        self._empty_shape_for = dict(empty_shape_for or {})
        self._raise_for = set(raise_for or set())
        self._real_shape_for = set(real_shape_for or set())
        self.calls = 0

    def generate_completion(self, request: Any) -> dict[str, Any]:
        self.calls += 1
        messages = list(getattr(request, "messages", []) or [])
        last = messages[-1]["content"] if messages else ""
        for token in self._raise_for:
            if token in last:
                raise RuntimeError(f"simulated provider failure: {last[:30]}")
        for token, shape in self._empty_shape_for.items():
            if token in last:
                return shape
        for token in self._real_shape_for:
            if token in last:
                # The REAL macos_inference_plugin Apple FM shape, verbatim
                # per plugins/macos_inference_plugin/src/macos_inference_plugin/
                # providers/apple_fm_provider.py:376-395.
                return {
                    "action_status": "completed",
                    "data": {
                        "result": {
                            "completion": f"Apple FM summary: {last[:40]}",
                            "model": "apple-system",
                            "provider": "apple_fm",
                            "usage": {
                                "input_tokens": 120, "output_tokens": 40,
                                "total_tokens": 160,
                            },
                            "finish_reason": "stop",
                            "latency_ms": 812.4,
                            "original_input_tokens": 120,
                            "input_truncated": False,
                            "omitted_input_chars": 0,
                            "max_response_tokens": 400,
                            "context_size": 4096,
                        },
                    },
                    "error": None,
                    "timestamp": "2026-09-27T14:00:00Z",
                }
        return {
            "action_status": "completed",
            "data": {"result": {"completion": f"Stub summary: {last[:40]}"}},
        }


def _make_service(
    *, repository: Any, summary_writer: Any, inference_service: Any,
) -> SessionLedgerService:
    instance = SessionLedgerService.__new__(SessionLedgerService)
    instance._repository = repository  # type: ignore[assignment]
    instance._summary_writer = summary_writer  # type: ignore[assignment]
    instance._inference_service = inference_service
    instance._summary_executor = None  # type: ignore[assignment]
    return instance


def _seed(sid: str, **extra: Any) -> dict[str, Any]:
    return {"id": sid, "source_id": "src-1", "summary_text": None, **extra}


def _with_capture() -> tuple[_CapturingHandler, logging.Logger]:
    logger = logging.getLogger("ananta.services.session_ledger_service.summarize")
    handler = _CapturingHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return handler, logger


def test_clean_drain_logs_no_warning() -> None:
    """errored=0, skipped_empty=0 → no WARNING line at all (only the INFO
    DRAIN-complete summary)."""
    handler, logger = _with_capture()
    try:
        done: set[str] = set()
        repo = _DrainRepo([_seed("les-clean-1"), _seed("les-clean-2")], done)
        writer = _DrainWriter(done)
        service = _make_service(
            repository=repo, summary_writer=writer, inference_service=_StubInference(),
        )
        result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
        _check(
            result["sessions_errored"] == 0 and result["sessions_skipped_empty"] == 0,
            f"clean drain: no errors, no empty skips (got {result})",
        )
        _check(
            handler.messages(level=logging.WARNING) == [],
            f"no WARNING logged on a clean drain (got {handler.messages(level=logging.WARNING)})",
        )
    finally:
        logger.removeHandler(handler)


def test_errored_and_skipped_empty_counted_separately_and_warned() -> None:
    """A timeline-raise (→ errored) and an empty completion with a
    DISTINCTIVE, non-matching envelope shape (→ skipped_empty) in the same
    drain: counted in separate buckets, each triggers its own per-session
    WARNING, and the drain-level WARNING fires naming both counts."""
    handler, logger = _with_capture()
    try:
        done: set[str] = set()
        sessions = [_seed("les-boom"), _seed("les-weird-shape"), _seed("les-ok")]
        repo = _DrainRepo(sessions, done, timeline_raises={"les-boom"})
        writer = _DrainWriter(done)
        weird_shape = {"action_status": "completed", "data": {"unexpected_field": "no completion here"}}
        service = _make_service(
            repository=repo, summary_writer=writer,
            inference_service=_StubInference(empty_shape_for={"les-weird-shape": weird_shape}),
        )
        result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
        _check(
            result["sessions_errored"] == 1 and result["sessions_skipped_empty"] == 1
            and result["sessions_inferred"] == 1,
            f"errored and skipped_empty counted in separate buckets (got {result})",
        )
        _check(
            result["sessions_skipped"] == 2,
            f"back-compat aggregate sessions_skipped = errored + skipped_empty (got {result})",
        )
        warnings = handler.messages(level=logging.WARNING)
        _check(
            any("les-boom" in m and "consecutive drain" in m for m in warnings),
            f"per-session WARNING names the errored session + consecutive-drain count "
            f"(got {warnings})",
        )
        _check(
            any(
                "les-weird-shape" in m and "unexpected_field" in m
                for m in warnings
            ),
            f"per-session WARNING for skipped_empty names the actual result shape "
            f"(got {warnings})",
        )
        _check(
            any("errored=1" in m and "skipped_empty=1" in m for m in warnings),
            f"drain-level WARNING fires naming both non-clean counts (got {warnings})",
        )
    finally:
        logger.removeHandler(handler)


def test_consecutive_error_counter_increments_and_resets() -> None:
    """The SAME session failing across THREE separate drains accumulates a
    consecutive-failure count (1, 2, 3); once it stops failing, the counter is
    gone (reset), and it is NEVER sentinel-marked while failing."""
    svc_mod._CONSECUTIVE_ERROR_COUNTS.clear()
    handler, logger = _with_capture()
    try:
        session_id = "les-perma-boom"
        done: set[str] = set()
        writer = _DrainWriter(done)
        repo = _DrainRepo([_seed(session_id)], done, timeline_raises={session_id})
        service = _make_service(
            repository=repo, summary_writer=writer, inference_service=_StubInference(),
        )

        for expected_streak in (1, 2, 3):
            handler.records.clear()
            result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
            _check(
                result["sessions_errored"] == 1,
                f"drain #{expected_streak}: still errored (got {result})",
            )
            _check(
                session_id not in done,
                f"drain #{expected_streak}: never sentinel-marked while erroring",
            )
            _check(
                svc_mod._CONSECUTIVE_ERROR_COUNTS.get(session_id) == expected_streak,
                f"drain #{expected_streak}: consecutive count is {expected_streak} "
                f"(got {svc_mod._CONSECUTIVE_ERROR_COUNTS.get(session_id)})",
            )
            warnings = handler.messages(level=logging.WARNING)
            _check(
                any(f"{expected_streak} consecutive drain" in m for m in warnings),
                f"drain #{expected_streak}: WARNING names the streak (got {warnings})",
            )

        # Now the SAME session recovers (repo stops raising for it).
        repo._timeline_raises.discard(session_id)
        result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
        _check(
            result["sessions_inferred"] == 1,
            f"recovered session summarizes normally (got {result})",
        )
        _check(
            session_id not in svc_mod._CONSECUTIVE_ERROR_COUNTS,
            "consecutive-error counter is cleared once the session stops erroring",
        )
    finally:
        logger.removeHandler(handler)
        svc_mod._CONSECUTIVE_ERROR_COUNTS.clear()


def test_branch3_inferred_with_real_apple_fm_envelope_shape() -> None:
    """The real macos_inference_plugin Apple FM result shape
    (apple_fm_provider.py:376-395) is accepted by ``_extract_summary_text``
    end to end: the drain calls ``generate_completion``, extracts the
    completion, and writes an ``internal:auto_summarize:inferred`` push."""
    done: set[str] = set()
    repo = _DrainRepo([_seed("les-apple-fm")], done)
    writer = _DrainWriter(done)
    inference = _StubInference(real_shape_for={"les-apple-fm"})
    service = _make_service(
        repository=repo, summary_writer=writer, inference_service=inference,
    )

    result = service._drain_all_quiescent(quiescence_minutes=10, batch_size=10)
    _check(
        result["sessions_inferred"] == 1 and inference.calls == 1,
        f"branch 3 called generate_completion once and produced 1 inferred "
        f"summary (got {result}, calls={inference.calls})",
    )
    _check(
        len(writer.pushes) == 1
        and writer.pushes[0]["generated_by_client_id"] == "internal:auto_summarize:inferred"
        and writer.pushes[0]["summary_text"].startswith("Apple FM summary:"),
        f"the extracted text came from data.result.completion, the real Apple "
        f"FM field (got {writer.pushes})",
    )


def main() -> int:
    print("=== summarize_outcome_visibility_smoke (iss_b535985b fix, 2026-09-27) ===")
    test_clean_drain_logs_no_warning()
    test_errored_and_skipped_empty_counted_separately_and_warned()
    test_consecutive_error_counter_increments_and_resets()
    test_branch3_inferred_with_real_apple_fm_envelope_shape()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
