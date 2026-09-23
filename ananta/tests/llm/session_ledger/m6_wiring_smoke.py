#!/usr/bin/env python3
"""M6 wiring smoke — schema declaration + persist_summary denorm + auto-summarize cron.

Run:

    .venv/bin/python3 ananta/tests/llm/session_ledger/m6_wiring_smoke.py

Covers operator ruling 2026-05-31 (Gap 1 + Gap 2) end-to-end **plus** the
2026-06-01 D8 ruling (hybrid extraction with inference fallback) without
needing a running solet or LM Studio:

1. Schema for ``session_ledger_summary__embeddings`` is declared by
   ``get_session_ledger_summary_embeddings_schema`` AND registered
   via ``CoreSchemaDefinitions.get_all_core_schemas`` (Gap 1 fix —
   pgvector provider previously errored with
   ``relation '<name>.session_ledger_summary__embeddings' does not exist``).

2. ``SUMMARY_VECTOR_NAMESPACE`` is the single source of truth for both
   the SchemaDefinition namespace AND
   ``summarization.VECTOR_NAMESPACE`` so the runtime call into
   ``vector_service.store_vectors(namespace=...)`` matches the table
   the schema bootstrap creates.

3. ``ensure_periodic_summarize_schedule`` raises ``RuntimeError`` when
   scheduling_service is unbound (fail-fast) and otherwise installs a
   cron whose action invokes
   ``service_interface::session_ledger_service::summarize_quiescent_sessions``
   under tag ``ledger:periodic_summarize`` with the system-owned
   flow_id / session_id pattern (mirrors ensure_periodic_poll cron;
   confirmed correct via the 2026-05-31 trigger_poll cron fix).

4. ``summarize_quiescent_sessions`` raises ``RuntimeError`` when
   inference_service is unbound (fail-fast) and otherwise calls
   ``repository.list_quiescent_sessions`` + inference + push_summary,
   reporting per-pass counts.

5. Cadence validation rejects 0 and 60+ for the ensure verb.

6. Both profile yamls carry the
   ``service_interface::session_ledger_service::ensure_periodic_summarize_schedule``
   starting_action.

7. Both KB JSON stubs exist with matching ``process_key``.

8. ``_assemble_transcript`` skips quarantined/null-content events and
   honors the byte cap.

The repository slot is stubbed; this smoke does NOT need Postgres.
Per-session error handling is exercised via an inference stub that
raises for one of two candidate sessions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.schema import (  # noqa: E402
    SUMMARY_VECTOR_NAMESPACE,
    get_session_ledger_summary_embeddings_schema,
)
from ananta.llm.session_ledger.summarization import VECTOR_NAMESPACE  # noqa: E402
from ananta.services.session_ledger_service.service import (  # noqa: E402
    SessionLedgerService,
)
from ananta.services.session_ledger_service.summarize import (  # noqa: E402
    _assemble_transcript,
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


# ─── Fixtures ────────────────────────────────────────────────────────────────


class _RecordingSchedulingService:
    """Captures clear + create calls from ensure_periodic_summarize_schedule."""

    def __init__(self, *, cleared_count: int = 0) -> None:
        self._cleared_count = cleared_count
        self.clear_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def clear_scheduled_actions_by_tag(self, tag: str) -> dict[str, Any]:
        self.clear_calls.append({"tag": tag})
        return {"data": {"cleared_count": self._cleared_count, "tag": tag}}

    def create_cron_schedule(
        self,
        *,
        cron_expression: str,
        actions: list[dict[str, Any]] | None = None,
        label: str | None = None,
        tags: list[str] | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append({
            "cron_expression": cron_expression,
            "actions": actions,
            "label": label,
            "tags": tags,
            "state": state,
        })
        return {"data": {"schedule_id": "sched-summarize-fixture-001"}}


def _non_trivial_timeline(session_id: str) -> list[dict[str, object]]:
    """Build a 4-event timeline with at least one assistant turn.

    Keeps the D8 ``_is_trivial_session`` predicate (< 4 events OR no
    assistant role events ⇒ trivial) on the *non*-trivial branch so the
    summarizer reaches the inference path without faking it.
    """
    return [
        {"event_type": "message", "role": "user",
         "content_text": f"hello {session_id}"},
        {"event_type": "message", "role": "assistant",
         "content_text": f"hi back {session_id}"},
        {"event_type": "message", "role": "user",
         "content_text": f"follow-up {session_id}"},
        {"event_type": "message", "role": "assistant",
         "content_text": f"final answer {session_id}"},
    ]


class _RecordingRepository:
    """Stub repository for the auto-summarize path."""

    def __init__(
        self,
        sessions: list[dict[str, Any]],
        *,
        away_summaries: dict[str, str] | None = None,
        timelines: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.away_summaries = dict(away_summaries or {})
        self.timelines = dict(timelines or {})
        self.calls_list_quiescent: list[dict[str, Any]] = []
        self.calls_get_timeline: list[str] = []
        self.calls_find_away: list[str] = []
        self.calls_mark_trivial: list[dict[str, Any]] = []

    def list_quiescent_sessions(
        self,
        *,
        quiescence_minutes: int,
        limit: int,
        trivial_sentinel: str,
    ) -> list[dict[str, object]]:
        self.calls_list_quiescent.append(
            {
                "quiescence_minutes": quiescence_minutes,
                "limit": limit,
                "trivial_sentinel": trivial_sentinel,
            },
        )
        return [dict(s) for s in self.sessions[:limit]]

    def get_session_timeline(
        self, *, session_id: str, after_sequence: int, limit: int,
    ) -> list[dict[str, object]]:
        self.calls_get_timeline.append(session_id)
        _ = after_sequence
        _ = limit
        if session_id in self.timelines:
            return [dict(e) for e in self.timelines[session_id]]
        return _non_trivial_timeline(session_id)

    def find_latest_away_summary_for_session(
        self, session_id: str,
    ) -> str | None:
        self.calls_find_away.append(session_id)
        return self.away_summaries.get(session_id)

    def mark_session_summary_text(
        self, *, session_id: str, summary_text: str,
    ) -> None:
        self.calls_mark_trivial.append({
            "session_id": session_id,
            "summary_text": summary_text,
        })


class _RecordingSummaryWriter:
    """Capture push_summary_chunk; one entry per push."""

    def __init__(self) -> None:
        self.pushes: list[dict[str, Any]] = []

    def push_summary_chunk(
        self, *, session_id: str, chunk_index: int,
        summary_text: str, generated_by_client_id: str,
    ) -> dict[str, Any]:
        self.pushes.append({
            "session_id": session_id,
            "chunk_index": chunk_index,
            "summary_text": summary_text,
            "generated_by_client_id": generated_by_client_id,
        })
        return {"summary_id": "sum-fixture", "embedding_vector_id": "ev-fixture"}


class _StubInferenceService:
    """Returns canned summaries via ``generate_completion(InferenceRequest)``.

    Tracks ``calls`` as the list of message arrays so tests can assert
    on-the-wire prompt content. ``raise_for`` raises mid-call to exercise
    per-session resilience. ``return_empty_for`` returns an empty completion
    to exercise the skip-on-empty branch.
    """

    def __init__(
        self,
        *,
        raise_for: str | None = None,
        return_empty_for: str | None = None,
    ) -> None:
        self.raise_for = raise_for
        self.return_empty_for = return_empty_for
        self.calls: list[list[dict[str, Any]]] = []

    def generate_completion(self, request: Any) -> dict[str, Any]:
        messages = list(getattr(request, "messages", []) or [])
        self.calls.append(messages)
        last = messages[-1]["content"] if messages else ""
        if self.raise_for and self.raise_for in last:
            raise RuntimeError(f"simulated inference failure for {self.raise_for}")
        if self.return_empty_for and self.return_empty_for in last:
            return {
                "action_status": "completed",
                "data": {"result": {"completion": ""}},
            }
        return {
            "action_status": "completed",
            "data": {
                "result": {"completion": f"Stub summary covering: {last[:40]}"},
            },
        }


class _SynchronousSummaryExecutor:
    """Test double: run the dispatched Phase-0b work INLINE, mirroring the
    production executor's fire-and-forget boundary (swallow + log-drop) so the
    push / discriminator assertions hold deterministically without threads."""

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, work: Any) -> bool:
        self.submitted += 1
        try:
            work()
        except Exception:  # noqa: BLE001 — mirror BoundedSummaryExecutor's boundary
            pass
        return True


def _make_service(
    *,
    scheduling_service: Any | None = None,
    inference_service: Any | None = None,
    repository: Any | None = None,
    summary_writer: Any | None = None,
    state_service: Any | None = None,
    summary_executor: Any | None = None,
) -> SessionLedgerService:
    """Construct a SessionLedgerService stand-in bypassing heavy init.

    ``summary_executor`` defaults to the synchronous test double so the existing
    push/discriminator assertions observe the write inline; pass a
    ``_RecordingSummaryExecutor`` to assert the non-blocking dispatch shape.
    """
    from _stub_state_service import StubStateService  # noqa: PLC0415

    instance = SessionLedgerService.__new__(SessionLedgerService)
    instance._registry = None  # type: ignore[assignment]
    instance._repository = repository  # type: ignore[assignment]
    instance._secret_gate = None  # type: ignore[assignment]
    instance._blob_adapter = None  # type: ignore[assignment]
    instance._importer = None  # type: ignore[assignment]
    instance._summary_writer = summary_writer  # type: ignore[assignment]
    instance._operator_equivalent_check = None
    instance._scheduling_service = scheduling_service
    instance._inference_service = inference_service
    instance._summary_executor = summary_executor or _SynchronousSummaryExecutor()  # type: ignore[assignment]
    instance._state_service = state_service or StubStateService()  # type: ignore[assignment]
    return instance


# ─── Cases ───────────────────────────────────────────────────────────────────


def test_schema_registered_in_core_schemas() -> None:
    """Gap 1: the embeddings schema must be returned by get_all_core_schemas."""
    from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: PLC0415

    schemas = CoreSchemaDefinitions.get_all_core_schemas()
    by_ns = {s.namespace: s for s in schemas}
    _check(
        SUMMARY_VECTOR_NAMESPACE in by_ns,
        f"core_schemas registers {SUMMARY_VECTOR_NAMESPACE!r} namespace",
    )
    summary_schema = by_ns.get(SUMMARY_VECTOR_NAMESPACE)
    if summary_schema is not None:
        _check(
            "embeddings" in summary_schema.tables,
            "summary schema has an 'embeddings' table",
        )


def test_summarization_vector_namespace_matches_schema() -> None:
    """The runtime VECTOR_NAMESPACE must equal the schema's SUMMARY_VECTOR_NAMESPACE."""
    _check(
        VECTOR_NAMESPACE == SUMMARY_VECTOR_NAMESPACE == "session_ledger_summary",
        f"VECTOR_NAMESPACE ({VECTOR_NAMESPACE!r}) "
        f"== SUMMARY_VECTOR_NAMESPACE ({SUMMARY_VECTOR_NAMESPACE!r})",
    )


def test_embeddings_table_has_pgvector_shape() -> None:
    """Columns must match pgvector_service_plugin's embeddings table shape."""
    sch = get_session_ledger_summary_embeddings_schema()
    table = sch.tables["embeddings"]
    column_names = set(table.columns.keys())
    for required in ("embedding", "dimension", "metadata", "distance_metric"):
        _check(
            required in column_names,
            f"embeddings table declares column {required!r}",
        )


def test_ensure_summarize_raises_when_scheduling_unbound() -> None:
    service = _make_service(scheduling_service=None)
    raised: RuntimeError | None = None
    try:
        service.ensure_periodic_summarize_schedule()
    except RuntimeError as exc:
        raised = exc
    _check(
        raised is not None,
        "ensure_periodic_summarize_schedule raises RuntimeError without scheduling_service",
    )


def test_ensure_summarize_creates_cron_with_correct_shape() -> None:
    scheduler = _RecordingSchedulingService(cleared_count=0)
    service = _make_service(scheduling_service=scheduler)
    result = service.ensure_periodic_summarize_schedule()

    _check(
        result["outcome"] == "created",
        f"first-run outcome='created' (got {result['outcome']!r})",
    )
    _check(
        result["tag"] == "ledger:periodic_summarize",
        f"default tag (got {result['tag']!r})",
    )
    _check(
        result["cadence_minutes"] == 10,
        f"default cadence = 10 minutes (got {result['cadence_minutes']})",
    )
    _check(
        scheduler.clear_calls == [{"tag": "ledger:periodic_summarize"}],
        f"clear called once with default tag (got {scheduler.clear_calls})",
    )
    _check(
        len(scheduler.create_calls) == 1,
        f"create called once (got {len(scheduler.create_calls)})",
    )
    if scheduler.create_calls:
        call = scheduler.create_calls[0]
        _check(
            call["cron_expression"] == "*/10 * * * *",
            f"cron is */10 form for 10-minute default (got {call['cron_expression']!r})",
        )
        _check(
            call["tags"] == ["ledger:periodic_summarize"],
            f"tags carry only ledger:periodic_summarize (got {call['tags']})",
        )
        # Field-set-tolerant: assert the expected action fields ARE present
        # with the expected values; do NOT assert the action carries no
        # extras. Additive fields like ``result_processor_kind`` were added
        # post-2026-06-06 commit ``560093270`` (M6 wiring landing); the
        # original equality assertion broke on the addition. Per the
        # 2026-06-13 broken-smokes cleanup brief.
        _check(
            len(call["actions"]) == 1,
            f"exactly one cron action created (got {len(call['actions'])})",
        )
        action = call["actions"][0] if call["actions"] else {}
        _check(
            action.get("process_key")
            == "service_interface::session_ledger_service::summarize_quiescent_sessions",
            f"action invokes summarize_quiescent_sessions (got {action.get('process_key')!r})",
        )
        _check(
            action.get("arguments") == {},
            f"action carries empty arguments (got {action.get('arguments')!r})",
        )
        # System-owned flow + session ids (matches periodic_poll pattern).
        state = call.get("state") or {}
        _check(
            isinstance(state, dict) and "flow_id" in state and "session_id" in state,
            f"create state carries system flow_id+session_id (got {state})",
        )


def test_ensure_summarize_normalizes_when_clear_returns_nonzero() -> None:
    scheduler = _RecordingSchedulingService(cleared_count=2)
    service = _make_service(scheduling_service=scheduler)
    result = service.ensure_periodic_summarize_schedule()
    _check(
        result["outcome"] == "normalized",
        f"outcome='normalized' after clear>0 (got {result['outcome']!r})",
    )
    _check(
        result["cleared_count"] == 2,
        f"cleared_count surfaced (got {result['cleared_count']})",
    )


def test_ensure_summarize_cadence_validation() -> None:
    scheduler = _RecordingSchedulingService()
    service = _make_service(scheduling_service=scheduler)
    for bad in (0, 60, -1, 100):
        raised: ValueError | None = None
        try:
            service.ensure_periodic_summarize_schedule(cadence_minutes=bad)
        except ValueError as exc:
            raised = exc
        _check(
            raised is not None,
            f"cadence_minutes={bad} raises ValueError",
        )
    _check(
        scheduler.create_calls == [],
        "no cron created when validation fails",
    )


def test_summarize_raises_when_inference_unbound() -> None:
    service = _make_service(inference_service=None)
    raised: RuntimeError | None = None
    try:
        service.summarize_quiescent_sessions()
    except RuntimeError as exc:
        raised = exc
    _check(
        raised is not None,
        "summarize_quiescent_sessions raises RuntimeError without inference_service",
    )


# NOTE: the multi-session PASS-level cases (happy-path aggregate, per-session
# inference-failure isolation, no-events skip) moved to
# ``quiescent_drain_smoke.py`` when the bounded ≤5-per-fire pass became the
# singleton drain-until-empty. Per-branch outcome coverage stays below via
# direct ``_summarize_one_session`` calls.


# ─── D8 hybrid extraction-with-inference-fallback (2026-06-01) ───────────────


def test_d8_custom_title_seed_reaches_push_summary_chunk() -> None:
    """Operator 2026-06-01 Bug 1: claude_code sessions seeded with
    ``custom_title`` (operator-authoritative per 2026-05-31 Architect §3)
    were never picked up by M6 because list_quiescent_sessions filtered
    by ``summary_text IS NULL``. Fix: the filter now uses NOT EXISTS on
    ``__summary``, and ``_summarize_one_session`` accepts the seeded
    text as a 0th-priority branch that pushes it through embedding +
    persist_summary so the session becomes searchable. The seed path is
    attributed via the ``custom_title_seed`` discriminator."""
    repo = _RecordingRepository(
        sessions=[
            {
                "id": "les-cc-seeded-001",
                "summary_text": "M6 wiring slice (operator-set custom title)",
            },
        ],
    )
    writer = _RecordingSummaryWriter()
    inference = _StubInferenceService()
    service = _make_service(
        inference_service=inference,
        repository=repo,
        summary_writer=writer,
    )
    outcome = service._summarize_one_session(
        "les-cc-seeded-001",
        existing_summary_text="M6 wiring slice (operator-set custom title)",
        source_kind=None,
    )

    _check(
        repo.calls_find_away == [],
        f"extraction path NOT consulted when a seed is present "
        f"(got {repo.calls_find_away})",
    )
    _check(
        inference.calls == [],
        f"inference NOT invoked when a seed is present "
        f"(got {len(inference.calls)})",
    )
    _check(
        outcome == "summarized",
        f"seeded session counted as summarized (got {outcome!r})",
    )
    _check(
        len(writer.pushes) == 1,
        f"push_summary_chunk fired exactly once on the seed "
        f"(got {len(writer.pushes)})",
    )
    if writer.pushes:
        push = writer.pushes[0]
        _check(
            push["summary_text"]
            == "M6 wiring slice (operator-set custom title)",
            f"push carries the existing seed text verbatim "
            f"(got {push['summary_text']!r})",
        )
        _check(
            push["generated_by_client_id"]
            == "internal:auto_summarize:custom_title_seed",
            f"seed path uses custom_title_seed discriminator "
            f"(got {push['generated_by_client_id']!r})",
        )


def test_d8_trivial_sentinel_is_not_treated_as_a_seed() -> None:
    """A row whose ``summary_text`` is the trivial sentinel must NOT be
    treated as a custom_title seed — the sentinel is the cron's previous
    "no usable content here, skip" signal, not operator-authored text.
    Without this guard, a marked-trivial session would loop back through
    push_summary_chunk and pollute embeddings with the sentinel string.

    In production, ``list_quiescent_sessions`` excludes sentinel rows
    upstream of this method. This test pins the second line of defense
    by giving ``_summarize_one_session`` an empty timeline so any
    misrouted seed-branch attempt would still be visible as an extra
    push; the correct fall-through is to ``skipped``."""
    sentinel = "(trivial session — no summarization)"
    repo = _RecordingRepository(
        sessions=[
            {"id": "les-cc-marked-trivial-001", "summary_text": sentinel},
        ],
        timelines={"les-cc-marked-trivial-001": []},
    )
    writer = _RecordingSummaryWriter()
    inference = _StubInferenceService()
    service = _make_service(
        inference_service=inference,
        repository=repo,
        summary_writer=writer,
    )
    outcome = service._summarize_one_session(
        "les-cc-marked-trivial-001",
        existing_summary_text=sentinel,
        source_kind=None,
    )

    _check(
        writer.pushes == [],
        f"trivial sentinel NOT pushed back through embedding "
        f"(got {writer.pushes})",
    )
    _check(
        outcome == "marked_trivial",
        f"trivial-sentinel row with an empty timeline is (idempotently) re-marked "
        f"marked_trivial, NOT treated as a seed (got {outcome!r})",
    )


def test_d8_inference_path_uses_inferred_discriminator() -> None:
    """Sanity: the three discriminator client_ids attribute every push
    back to its origin branch — extraction sets ``extracted_away_summary``,
    inference sets ``inferred``, custom_title sets ``custom_title_seed``."""
    repo = _RecordingRepository(
        sessions=[{"id": "les-cc-infer-disc-001"}],
    )
    writer = _RecordingSummaryWriter()
    inference = _StubInferenceService()
    service = _make_service(
        inference_service=inference,
        repository=repo,
        summary_writer=writer,
    )
    outcome = service._summarize_one_session(
        "les-cc-infer-disc-001", existing_summary_text=None, source_kind=None,
    )
    _check(
        outcome == "summarized"
        and len(writer.pushes) == 1
        and writer.pushes[0]["generated_by_client_id"]
        == "internal:auto_summarize:inferred",
        f"inference path uses inferred discriminator "
        f"(got {outcome!r}, {writer.pushes})",
    )


def test_d8_away_summary_extraction_skips_inference() -> None:
    """Case (a): claude_code session with away_summary → extract, 0 inference calls."""
    # The real repository.find_latest_away_summary_for_session strips
    # before returning; the stub mirrors that contract so the service
    # gets the same shape it would see in production.
    repo = _RecordingRepository(
        sessions=[{"id": "les-cc-with-recap"}],
        away_summaries={"les-cc-with-recap": "Recap: shipped slice X."},
    )
    writer = _RecordingSummaryWriter()
    inference = _StubInferenceService()
    service = _make_service(
        inference_service=inference,
        repository=repo,
        summary_writer=writer,
    )
    outcome = service._summarize_one_session(
        "les-cc-with-recap", existing_summary_text=None, source_kind=None,
    )

    _check(
        repo.calls_find_away == ["les-cc-with-recap"],
        f"find_latest_away_summary_for_session called once "
        f"(got {repo.calls_find_away})",
    )
    _check(
        repo.calls_get_timeline == [],
        f"get_session_timeline NOT called when away_summary present "
        f"(got {repo.calls_get_timeline})",
    )
    _check(
        inference.calls == [],
        f"inference NOT invoked on extraction path (got {len(inference.calls)})",
    )
    _check(
        outcome == "summarized",
        f"extraction counted as summarized (got {outcome!r})",
    )
    _check(
        len(writer.pushes) == 1 and writer.pushes[0]["summary_text"] == "Recap: shipped slice X.",
        f"push uses trimmed away_summary recap (got {writer.pushes})",
    )
    _check(
        writer.pushes[0]["generated_by_client_id"]
        == "internal:auto_summarize:extracted_away_summary",
        f"extraction path tagged with extracted_away_summary discriminator "
        f"(got {writer.pushes[0]['generated_by_client_id']!r})",
    )


def test_d8_no_away_summary_falls_back_to_inference() -> None:
    """Case (b): claude_code session without away_summary → inference fallback."""
    repo = _RecordingRepository(
        sessions=[{"id": "les-cc-no-recap"}],
    )
    writer = _RecordingSummaryWriter()
    inference = _StubInferenceService()
    service = _make_service(
        inference_service=inference,
        repository=repo,
        summary_writer=writer,
    )
    outcome = service._summarize_one_session(
        "les-cc-no-recap", existing_summary_text=None, source_kind=None,
    )

    _check(
        repo.calls_find_away == ["les-cc-no-recap"],
        f"extraction attempted before inference "
        f"(got {repo.calls_find_away})",
    )
    _check(
        len(inference.calls) == 1,
        f"inference fallback invoked exactly once "
        f"(got {len(inference.calls)})",
    )
    _check(
        outcome == "summarized",
        f"synchronous inference fallback counted as summarized (got {outcome!r})",
    )
    _check(
        len(writer.pushes) == 1 and "Stub summary" in writer.pushes[0]["summary_text"],
        f"push carries inference completion (got {writer.pushes})",
    )
    # The inference request carried the expected system prompt at index 0.
    messages = inference.calls[0]
    _check(
        messages and messages[0]["role"] == "system",
        f"first message is the system prompt (got {messages[0]['role'] if messages else None!r})",
    )
    _check(
        len(messages) == 2 and messages[1]["role"] == "user",
        "second message is the user transcript",
    )


def test_d8_trivial_session_marked_and_skipped() -> None:
    """Case (c): tiny session (< 4 events, no assistant turns) → marked_trivial."""
    repo = _RecordingRepository(
        sessions=[{"id": "les-trivial"}],
        timelines={
            "les-trivial": [
                {"event_type": "message", "role": "user",
                 "content_text": "just a single message"},
            ],
        },
    )
    writer = _RecordingSummaryWriter()
    inference = _StubInferenceService()
    service = _make_service(
        inference_service=inference,
        repository=repo,
        summary_writer=writer,
    )
    outcome = service._summarize_one_session(
        "les-trivial", existing_summary_text=None, source_kind=None,
    )

    _check(
        inference.calls == [],
        f"inference NOT invoked on trivial session "
        f"(got {len(inference.calls)})",
    )
    _check(
        writer.pushes == [],
        f"no summary chunk pushed for trivial session "
        f"(got {writer.pushes})",
    )
    _check(
        outcome == "marked_trivial",
        f"trivial outcome distinct from summarized/skipped (got {outcome!r})",
    )
    _check(
        len(repo.calls_mark_trivial) == 1
        and repo.calls_mark_trivial[0]["session_id"] == "les-trivial"
        and "trivial" in repo.calls_mark_trivial[0]["summary_text"].lower(),
        f"mark_session_summary_text called with trivial sentinel "
        f"(got {repo.calls_mark_trivial})",
    )


def test_d8_zero_assistant_turns_also_trivial() -> None:
    """Case (c.2): >= 4 events but 0 assistant role events → also trivial."""
    repo = _RecordingRepository(
        sessions=[{"id": "les-no-assistant"}],
        timelines={
            "les-no-assistant": [
                {"event_type": "message", "role": "user", "content_text": "a"},
                {"event_type": "message", "role": "user", "content_text": "b"},
                {"event_type": "message", "role": "user", "content_text": "c"},
                {"event_type": "message", "role": "user", "content_text": "d"},
            ],
        },
    )
    writer = _RecordingSummaryWriter()
    inference = _StubInferenceService()
    service = _make_service(
        inference_service=inference,
        repository=repo,
        summary_writer=writer,
    )
    outcome = service._summarize_one_session(
        "les-no-assistant", existing_summary_text=None, source_kind=None,
    )
    _check(
        outcome == "marked_trivial",
        f">=4 events with 0 assistant turns marked trivial (got {outcome!r})",
    )
    _check(
        inference.calls == [] and writer.pushes == [],
        "no inference + no push for the zero-assistant trivial case",
    )


def test_d8_codex_session_falls_back_to_inference() -> None:
    """Case (d): codex / agent_messaging sessions have no away_summary → inference."""
    repo = _RecordingRepository(
        sessions=[
            {"id": "les-codex-001"},
            {"id": "les-agentmsg-001"},
        ],
    )
    writer = _RecordingSummaryWriter()
    inference = _StubInferenceService()
    service = _make_service(
        inference_service=inference,
        repository=repo,
        summary_writer=writer,
    )
    outcome_a = service._summarize_one_session(
        "les-codex-001", existing_summary_text=None, source_kind=None,
    )
    outcome_b = service._summarize_one_session(
        "les-agentmsg-001", existing_summary_text=None, source_kind=None,
    )
    _check(
        repo.calls_find_away == ["les-codex-001", "les-agentmsg-001"],
        f"extraction attempted for both non-claude-code sessions "
        f"(got {repo.calls_find_away})",
    )
    _check(
        len(inference.calls) == 2,
        f"inference fallback invoked for each codex / agent_messaging session "
        f"(got {len(inference.calls)})",
    )
    _check(
        outcome_a == "summarized" and outcome_b == "summarized",
        f"both sessions summarized via synchronous inference fallback "
        f"(got {outcome_a!r}, {outcome_b!r})",
    )


def test_d8_quiescent_filter_still_summary_null_only() -> None:
    """Case (e) regression: ``list_quiescent_sessions`` SQL still filters
    ``summary_text IS NULL``. Mark a session trivial via the new path and
    confirm the LIVE-DB filter excludes it on the next pass.

    Uses the stub state-service so we exercise SQL composition without a
    real Postgres connection — the same approach as
    ``test_persist_summary_issues_insert_plus_denormalize_update``.
    """
    sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))
    from _stub_state_service import StubStateService  # noqa: E402, PLC0415
    from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: PLC0415

    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]

    # mark_session_summary_text emits a typed update_state guarded by the
    # ``summary_text {"op": "is_null"}`` filter (SQL-lockdown summarize slice).
    from ananta.llm.session_ledger.schema import TABLE_SESSION  # noqa: PLC0415

    repo.mark_session_summary_text(
        session_id="les-from-d8",
        summary_text="(trivial session — no summarization)",
    )
    mark_updates = [u for u in stub.updates if u.table == TABLE_SESSION]
    _check(
        len(mark_updates) == 1,
        f"mark_session_summary_text issues one update_state (got {len(mark_updates)})",
    )
    if mark_updates:
        upd = mark_updates[0]
        _check(
            upd.filters.get("summary_text") == {"op": "is_null"},
            "update filters by summary_text is_null (won't clobber a real summary)",
        )
        _check(
            "summary_text" in upd.updates,
            "update sets the summary_text column",
        )

    # SQL-lockdown (list_quiescent read-then-route migration): the verb no
    # longer composes a NOT EXISTS raw SQL on __summary. It now issues typed
    # query_state reads (candidates → summaries → sources) + a Python fold
    # (select_quiescent_sessions) that excludes the trivial sentinel. Plant a
    # clean + a sentinel-marked candidate (the stub's query_state returns
    # planted rows; the fold applies the exclusion) and confirm only the clean
    # one survives. Cutoff/canonical/summarized behavioral coverage lives in
    # quiescent_fold_smoke.py's filter-honoring shim + the live smoke.
    sentinel = "(trivial session — no summarization)"

    def _cand(sid: str, summary_text: str | None) -> dict[str, object]:
        return {
            "id": sid, "source_id": "src-1", "summary_text": summary_text,
            "last_event_at": "2026-06-01T00:00:00",
            "canonical_external_session_id": None, "is_deleted": 0,
        }

    stub2 = StubStateService()
    repo2 = SessionLedgerRepository(stub2)  # type: ignore[arg-type]
    stub2.add_select_response(
        "session_ledger__session", [_cand("les-clean", None), _cand("les-from-d8", sentinel)],
    )
    stub2.add_select_response("session_ledger__summary", [])
    stub2.add_select_response(
        "session_ledger__source", [{"id": "src-1", "source_kind": "codex_local", "is_deleted": 0}],
    )
    rows = repo2.list_quiescent_sessions(
        quiescence_minutes=10, limit=5, trivial_sentinel=sentinel,
    )
    ids = {str(r["id"]) for r in rows}
    _check(
        "les-from-d8" not in ids,
        "trivial-sentinel session EXCLUDED by the read-then-route fold",
    )
    _check(
        "les-clean" in ids,
        "clean candidate survives (read-then-route includes it)",
    )
    _check(
        not any("NOT EXISTS" in c.sql for c in stub2.calls),
        "no raw NOT EXISTS SQL — list_quiescent migrated to typed query_state",
    )
    _check(
        any("query session_ledger__session" in c.sql for c in stub2.calls),
        "candidate read issued via typed query_state on __session",
    )


def test_assemble_transcript_honors_max_chars_and_skips_null() -> None:
    events = [
        {"role": "user", "content_text": "first user message"},
        {"role": "tool", "content_text": "TOOL NOISE excluded"},        # non-conversation
        {"role": "system", "content_text": "SYSTEM NOISE excluded"},    # non-conversation
        {"role": "assistant", "content_text": None},  # blobbed
        {"role": "assistant", "content_text": ""},     # empty
        {"role": "user", "content_text": "second user message"},
        {"content_text": "NULL ROLE NOISE excluded"},  # no role -> excluded
    ]
    out = _assemble_transcript(events, 10_000)
    _check(
        "first user message" in out,
        "transcript carries first user line",
    )
    _check(
        "second user message" in out,
        "transcript carries second user line",
    )
    _check(
        "None" not in out,
        "null content_text rows skipped (not stringified 'None')",
    )
    _check(
        "NOISE" not in out,
        "non-conversation roles (tool/system/null) excluded from transcript "
        "(operator 2026-06-30: only user/assistant)",
    )

    bounded = _assemble_transcript(events, 20)  # too small for both lines
    _check(
        len(bounded) <= 25,
        f"max_chars cap respected (got len={len(bounded)})",
    )


def test_starting_actions_present_in_both_profiles() -> None:
    import yaml  # noqa: PLC0415
    process_key = "service_interface::session_ledger_service::ensure_periodic_summarize_schedule"
    # ORIGIN-ONLY LEG — identical shape and identical reason to the same guard in
    # ensure_periodic_poll_smoke: `initialization/` is absent from the seed
    # manifest's `copy:` allowlist, so these profiles describe the ORIGIN's boot
    # wiring only. Also 1 of the r21 verdict's 14 BLOCKING failures, failing here
    # after its schema and denormalization checks had passed.
    profiles_root = REPO_ROOT / "initialization" / "profiles"
    if not profiles_root.is_dir():
        print("  SKIP  starting_actions profile wiring: initialization/profiles is origin-only (not shipped)")
        return
    for profile_name in ("local.yaml", "cloud.yaml"):
        text = (profiles_root / profile_name).read_text()
        raw = yaml.safe_load(text)
        keys = {entry["process_key"] for entry in raw.get("starting_actions", [])}
        _check(
            process_key in keys,
            f"{profile_name} starting_actions includes {process_key}",
        )


def test_persist_summary_issues_insert_plus_denormalize_update() -> None:
    """Gap 2(B): persist_summary must emit both a typed write_state INSERT into
    __summary AND a denormalizing update_state on __session.

    SQL-lockdown summarize slice: the single-statement ``COALESCE((SELECT
    MAX(chunk_index)))`` guard is recomputed in Python from in-txn reads, so it
    cannot be asserted as an SQL substring here. The stub structurally fires the
    update every time (its chunk read returns [] and its synthetic __session row
    carries no summary_text → the NULL arm is always true), so the no-clobber /
    stale-lower-chunk branch is exercised in ``summarize_migration_live_smoke.py``
    against the real schema, not here. This test verifies the typed-op SHAPE:
    one __summary write + one __session denorm update.
    """
    sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))
    from _stub_state_service import StubStateService  # noqa: E402, PLC0415
    from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: PLC0415
    from ananta.llm.session_ledger.schema import (  # noqa: PLC0415
        TABLE_SESSION,
        TABLE_SUMMARY,
    )

    stub = StubStateService()
    repo = SessionLedgerRepository(stub)  # type: ignore[arg-type]
    from datetime import UTC, datetime  # noqa: PLC0415

    repo.persist_summary(
        session_id="les-denorm-001",
        chunk_index=0,
        summary_text="first chunk summary",
        embedding_vector_id="ev-001",
        generated_by_client_id="internal:auto_summarize",
        generated_at=datetime.now(UTC),
    )

    summary_writes = [w for w in stub.writes if w.table == TABLE_SUMMARY]
    session_updates = [u for u in stub.updates if u.table == TABLE_SESSION]
    _check(
        len(summary_writes) == 1,
        f"persist_summary issues exactly one write_state into __summary "
        f"(got {len(summary_writes)})",
    )
    _check(
        len(session_updates) == 1,
        f"persist_summary issues exactly one denorm update_state on __session "
        f"(got {len(session_updates)})",
    )
    if summary_writes:
        rec = summary_writes[0].record
        _check(
            rec.get("chunk_index") == 0 and rec.get("embedding_vector_id") == "ev-001",
            "the __summary write carries the chunk_index + embedding_vector_id",
        )
    if session_updates:
        _check(
            "summary_text" in session_updates[0].updates,
            "the __session denorm update sets summary_text",
        )


def test_kb_stubs_present() -> None:
    for name in (
        "summarize_quiescent_sessions.json",
        "ensure_periodic_summarize_schedule.json",
    ):
        stub_path = (
            REPO_ROOT
            / "ananta"
            / "knowledge_base"
            / "processes"
            / "session_ledger_service"
            / name
        )
        _check(stub_path.is_file(), f"KB stub exists: {name}")
        if stub_path.is_file():
            import json  # noqa: PLC0415
            body = json.loads(stub_path.read_text())
            expected_key = (
                f"service_interface::session_ledger_service::{name[:-len('.json')]}"
            )
            _check(
                body.get("process_key") == expected_key,
                f"KB stub {name} process_key matches",
            )


# ─── Phase-0b: non-blocking dispatch + bounded executor (WI-0, 2026-07-01) ───


# NOTE: the singleton-drain entry behavior (a cron fire submits the WHOLE drain
# and no-ops if a drainer is already running; WI-0 no summarization on the
# calling thread) moved to ``quiescent_drain_smoke.py``. The two tests below pin
# the underlying ``BoundedSummaryExecutor`` mechanism, which is unchanged.


def test_phase0b_executor_concurrency_one() -> None:
    """BoundedSummaryExecutor admits exactly one background summary at a time."""
    import threading  # noqa: PLC0415
    import time  # noqa: PLC0415

    ex = BoundedSummaryExecutor(name="test-conc")
    started = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        started.set()
        release.wait(timeout=5.0)

    _check(ex.submit(_hold) is True, "first submit accepted")
    _check(started.wait(timeout=1.0), "first work started")
    _check(
        ex.submit(lambda: None) is False,
        "second submit rejected while the single slot is held (concurrency 1)",
    )
    release.set()
    freed = False
    for _ in range(40):
        if ex.submit(lambda: None):
            freed = True
            break
        time.sleep(0.05)
    _check(freed, "slot frees once the held work completes")


def test_phase0b_executor_raising_work_frees_slot() -> None:
    """A background task that RAISES — e.g. the provider's InferenceTimeoutError
    now that ``generate_completion`` enforces ``timeout=self.timeout`` (600s)
    instead of the old ``timeout=None`` — is caught at the fire-and-forget
    boundary and the single slot is freed so the next summary can dispatch. The
    worker never wedges on a failed call; the session's ``summary_text`` stays
    NULL and it is re-picked on the cron's next firing."""
    import threading  # noqa: PLC0415
    import time  # noqa: PLC0415

    ex = BoundedSummaryExecutor(name="test-raise")
    raised = threading.Event()

    def _boom() -> None:
        raised.set()
        raise TimeoutError("simulated provider InferenceTimeoutError")

    _check(ex.submit(_boom) is True, "submit accepted")
    _check(raised.wait(timeout=1.0), "raising work ran on the background thread")
    freed = False
    for _ in range(60):
        if ex.submit(lambda: None):
            freed = True
            break
        time.sleep(0.05)
    _check(
        freed,
        "slot frees after the raising work is caught (session re-picked next pass)",
    )


# NOTE: the WI-0 static AST guard now lives in ``quiescent_drain_smoke.py``
# (``test_wi0_static_entry_submits_without_inline_summarization``). The
# invariant inverted with the singleton drain: the ENTRY dispatches via
# ``_summary_executor.submit`` with no inline summarization, while
# ``_summarize_one_session`` DOES run synchronous inference (only ever on the
# drain's daemon thread).


def main() -> int:
    print("=== m6_wiring_smoke (operator ruling 2026-05-31 Gap 1 + Gap 2) ===")
    test_schema_registered_in_core_schemas()
    test_summarization_vector_namespace_matches_schema()
    test_embeddings_table_has_pgvector_shape()
    test_ensure_summarize_raises_when_scheduling_unbound()
    test_ensure_summarize_creates_cron_with_correct_shape()
    test_ensure_summarize_normalizes_when_clear_returns_nonzero()
    test_ensure_summarize_cadence_validation()
    test_summarize_raises_when_inference_unbound()
    test_phase0b_executor_concurrency_one()
    test_phase0b_executor_raising_work_frees_slot()
    test_d8_custom_title_seed_reaches_push_summary_chunk()
    test_d8_trivial_sentinel_is_not_treated_as_a_seed()
    test_d8_inference_path_uses_inferred_discriminator()
    test_d8_away_summary_extraction_skips_inference()
    test_d8_no_away_summary_falls_back_to_inference()
    test_d8_trivial_session_marked_and_skipped()
    test_d8_zero_assistant_turns_also_trivial()
    test_d8_codex_session_falls_back_to_inference()
    test_d8_quiescent_filter_still_summary_null_only()
    test_assemble_transcript_honors_max_chars_and_skips_null()
    test_persist_summary_issues_insert_plus_denormalize_update()
    test_starting_actions_present_in_both_profiles()
    test_kb_stubs_present()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
