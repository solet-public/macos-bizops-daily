"""Summarize domain mixin for ``SessionLedgerService`` (M6 summarization).

Schema-debt-external-id lane, service.py decomposition seam (2026-08-07).
Split out of ``service.py`` to pay down its pre-existing structural debt
(HEAD was already B(9.23) before this lane's ``retire_duplicate_source``
addition tipped it to C(8.75)) — mirrors the repository layer's own
per-ABC-family mixin split (``ananta.llm.session_ledger.summarize``'s
``SessionLedgerSummarizeMixin`` is the exact precedent this module follows)
rather than a single combined mixin, per seat ratification: a single
~600-650 line combined Summarize+EmbeddingDrain mixin would land in
god-class-gate territory (>500 non-process LOC), repeating the debt pattern
this split exists to resolve. See
``workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md``
for the full seam-scope proposal and ratification.

Everything here implements :class:`SessionLedgerSummarizeAPI`
(``interfaces/public.py``): the codex-stage1 seed lift, the auto-summarize
drain cron, and its idempotent schedule installer. The cron-plumbing helpers
(``_clear_and_prep_periodic_cron`` / ``_periodic_cron_result`` /
``_extract_schedule_id``) are shared with ``ensure_periodic_poll_schedule``
and ``ensure_periodic_embed_schedule`` and stay in ``service.py`` — imported
here locally (inside the method, not at module level) to avoid the circular
import a module-level import would create (``service.py`` imports this
module's mixin for its own class bases).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.interfaces.inference_service_interface import InferenceRequest
from ananta.llm.session_ledger.types import MessageRole
from ananta.services.session_ledger_service.interfaces.public import (
    SessionLedgerSummarizeAPI,
)
from ananta.services.session_ledger_service.periodic_cron import (
    clear_and_prep_periodic_cron,
    periodic_cron_result,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ananta.llm.session_ledger.repository import SessionLedgerRepository
    from ananta.llm.session_ledger.summarization import SummaryWriter
    from ananta.services.session_ledger_service.summary_executor import (
        SummaryExecutor,
    )

logger = logging.getLogger(__name__)

RELOAD_SAFE = True

# System-owned scheduler identifiers for the periodic-summarize cron. Mirrors
# the pattern at default_scheduling_plugin.constants.HEARTBEAT_FLOW_ID.
_LEDGER_PERIODIC_SUMMARIZE_FLOW_ID = "flow-ledger-periodic-summarize"
_LEDGER_PERIODIC_SUMMARIZE_SESSION_ID = "sess-ledger-periodic-summarize"

# Operator ruling 2026-06-01 Bug 1 fix: three discriminators on
# ``__summary.generated_by_client_id`` so every embed attributes back to
# its origin path. Architect's 2026-05-31 §3 mapping is preserved
# (custom_title remains the operator-authoritative summary text) while
# making those sessions searchable by also embedding the seed text.
_AUTO_SUMMARIZE_CLIENT_ID_CUSTOM_TITLE = (
    "internal:auto_summarize:custom_title_seed"
)
# M19 (Codex state_5.threads.title → __session.summary_text_seed): distinct
# discriminator per v2 §5.5 canonical (``_title`` suffix prevents collision
# if a future M-section adds another state_5-derived seed like
# ``first_user_message`` or ``preview``). Source-kind dispatch in
# ``_summarize_one_session`` picks this over CUSTOM_TITLE when the
# session's __source.source_kind is ``codex_state``.
_AUTO_SUMMARIZE_CLIENT_ID_CODEX_STATE_TITLE = (
    "internal:auto_summarize:codex_state_title_seed"
)
_AUTO_SUMMARIZE_CLIENT_ID_EXTRACTED = (
    "internal:auto_summarize:extracted_away_summary"
)
_AUTO_SUMMARIZE_CLIENT_ID_INFERRED = "internal:auto_summarize:inferred"

# Source-kind → seed-discriminator dispatch. Sessions whose source_kind
# is not present here fall back to CUSTOM_TITLE (the pre-M19 default,
# preserved for backward compat with claude_code's operator-set custom_title
# and any future source whose seed is a curated operator-set title).
_SEED_DISCRIMINATOR_BY_SOURCE_KIND: dict[str, str] = {
    "codex_state": _AUTO_SUMMARIZE_CLIENT_ID_CODEX_STATE_TITLE,
}
# Bound transcript size fed to the summarizer per session — caps inference
# cost when a quiescent session has thousands of events.
_AUTO_SUMMARIZE_MAX_EVENTS = 50
_AUTO_SUMMARIZE_MAX_CHARS = 24_000
# System prompt sets the model's ROLE as an outside analyst — NOT a
# participant. Without this, the configured instruct model
# (qwen/qwen3-30b-a3b-2507) gets pulled into CONTINUING the transcript
# (e.g. answering the last user turn / writing more of the same content),
# blows the token budget, and the LM Studio provider raises
# ``Response truncated`` → the pass records ``"skipped"`` with no sentinel →
# the row is re-picked forever (head-of-line clog). Proven 2026-06-30: the
# bare "summarize" prompt made the model write MORE transcript,
# finish_reason=length at 299 tokens; the analyst framing below returns a
# clean 3–5 sentence summary in ~150–200 tokens with finish_reason=stop.
_AUTO_SUMMARIZE_PROMPT = (
    "You are a transcript analyst. You are given a record of a past "
    "conversation between a user and an AI assistant. Your only job is to "
    "DESCRIBE what happened in it. Never continue, answer, or roleplay the "
    "conversation — only summarize it from the outside."
)
# The transcript is fenced between markers and the instruction is RE-STATED
# AFTER it — models follow a trailing instruction far more reliably than one
# buried before a long transcript. ``{transcript}`` is filled at call time.
_AUTO_SUMMARIZE_USER_TEMPLATE = (
    "Here is the session transcript, between the markers:\n"
    "<<<TRANSCRIPT\n"
    "{transcript}\n"
    "TRANSCRIPT>>>\n\n"
    "In 2–4 sentences of plain prose (no preamble, no bullet points), "
    "summarize the transcript above: the user's intent, the work the "
    "assistant performed, and any artifacts or decisions produced. "
    "Describe the session in the third person; do NOT continue or "
    "respond to it."
)
# Operator ruling 2026-06-01 D8: cap inference temp + length at the seam
# closest to the call so peer tuning lives in one place.
_AUTO_SUMMARIZE_INFERENCE_TEMPERATURE = 0.3
# 400 (was 300) — pure headroom. The analyst-framed prompt produces summaries
# in ~150–200 tokens; the extra budget absorbs the occasional longer session
# so a correct summary finishes (finish_reason=stop) rather than tripping the
# provider's length-truncation guard on a near-miss.
_AUTO_SUMMARIZE_INFERENCE_MAX_TOKENS = 400
# Trivial-session sentinel: written into ``session.summary_text`` so the
# quiescent-session list stops re-picking sessions that can't usefully be
# summarized (operator ruling 2026-06-01 D8). Distinct prose so it's
# trivially greppable in audits.
_AUTO_SUMMARIZE_TRIVIAL_SENTINEL = "(trivial session — no summarization)"
# Trivial-threshold tuning (operator ruling 2026-06-01 D8): "below 4 events
# OR zero assistant role events". Below the floor the transcript can't carry
# enough signal to summarize; the cron marks-and-moves-on.
_AUTO_SUMMARIZE_TRIVIAL_MIN_EVENTS = 4
# Operator ruling 2026-06-30: summarize ONLY real conversation — user + assistant
# messages. Everything else (tool results, system/hook events, the ~245K null-role
# claude_code noise, agent_messaging coordination chatter) is excluded from both
# the transcript and the trivial-session count, so the summary reflects the
# conversation and noise-only sessions are correctly marked trivial.
_CONVERSATION_ROLES = frozenset({MessageRole.USER.value, MessageRole.ASSISTANT.value})


class SessionLedgerSummarizeMixin(SessionLedgerSummarizeAPI):
    """M6 summarization surface: codex-stage1 seed lift + auto-summarize cron.

    Depends on attributes owned by :class:`SessionLedgerService`'s
    ``__slots__`` (``_repository``, ``_summary_writer``,
    ``_inference_service``, ``_scheduling_service``, ``_summary_executor``)
    — declared below under ``TYPE_CHECKING`` only, same idiom as the
    repository mixins depending on ``SessionLedgerRepositoryBase``'s
    attributes, adapted for a composed-class (not shared-base) layout since
    the service ABCs have no equivalent common base to declare them once.
    """

    __slots__ = ()

    if TYPE_CHECKING:
        _repository: SessionLedgerRepository
        _summary_writer: SummaryWriter
        _inference_service: Any
        _scheduling_service: Any
        _summary_executor: SummaryExecutor

    def lift_codex_stage1_summaries(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_summarize",
        cadence_minutes: int = 10,
    ) -> dict[str, Any]:
        """G8-mitigated one-shot rewrite of ``__session.summary_text`` from Codex stage1.

        Reads ``~/.codex/memories_1.sqlite::stage1_outputs`` (filtered to
        ``selected_for_phase2 = 1``), joins to existing ``__session`` rows by
        ``external_session_id = thread_id``, and rewrites each match's
        ``summary_text`` to the stage1 ``rollout_summary`` with
        ``internal:auto_summarize:codex_stage1_seed`` attribution on the
        chunk push. PAUSES the M6 SUMMARIZE cron (NOT the poll cron) for the
        duration; re-ensures it in a try/finally.
        """
        # Local import to avoid coupling the session_ledger_service module to
        # the codex_memories vendor parser at import time.
        from ananta.llm.session_ledger.vendor import codex_memories  # noqa: PLC0415

        if not 1 <= int(cadence_minutes) <= 59:
            raise ValueError(
                f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
            )
        db_path = Path(os.path.expanduser(codex_memories.DEFAULT_DB_PATH))
        if not db_path.exists():
            return {
                "confirmed": False,
                "stage1_row_count": 0,
                "candidate_count": 0,
                "lifted_count": 0,
                "pause_tag": tag,
                "resume_outcome": "skipped",
            }
        candidates = self._collect_codex_stage1_candidates(codex_memories, db_path)
        stage1_row_count = len(candidates)
        candidate_count = sum(1 for _, session_id in candidates if session_id is not None)
        if not confirm:
            return {
                "confirmed": False,
                "stage1_row_count": stage1_row_count,
                "candidate_count": candidate_count,
                "lifted_count": 0,
                "pause_tag": tag,
                "resume_outcome": "skipped",
            }
        if self._scheduling_service is None:
            raise RuntimeError(
                "lift_codex_stage1_summaries requires scheduling_service "
                "to be bound at session_ledger_service construction (to "
                "pause+resume the M6 summarize cron around the rewrite)",
            )
        lifted_count = 0
        resume_outcome = "skipped"
        try:
            self._scheduling_service.clear_scheduled_actions_by_tag(tag=tag)
            lifted_count = self._lift_stage1_candidates(candidates)
            logger.info(
                "lift_codex_stage1_summaries: stage1_row_count=%d "
                "candidate_count=%d lifted_count=%d",
                stage1_row_count, candidate_count, lifted_count,
            )
        finally:
            # MUST re-ensure the cron even on exception.
            resume_result = self.ensure_periodic_summarize_schedule(
                cadence_minutes=int(cadence_minutes),
                tag=tag,
            )
            resume_outcome = str(resume_result.get("outcome", ""))
        return {
            "confirmed": True,
            "stage1_row_count": stage1_row_count,
            "candidate_count": candidate_count,
            "lifted_count": lifted_count,
            "pause_tag": tag,
            "resume_outcome": resume_outcome,
        }

    def _collect_codex_stage1_candidates(
        self,
        codex_memories: Any,
        db_path: Path,
    ) -> list[tuple[Any, str | None]]:
        """Read stage1_outputs and resolve each thread_id to a __session row id.

        Returns ``(row, session_id_or_None)`` pairs preserving the read
        order. Rows without a matching __session land as ``(row, None)``
        and are skipped by the rewrite loop.
        """
        pairs: list[tuple[Any, str | None]] = []
        with codex_memories.open_readonly(db_path) as con:
            for row in codex_memories.iter_stage1_rows(con):
                session_id = self._repository.find_session_id_by_external_session_id(
                    row.thread_id,
                )
                pairs.append((row, session_id))
        return pairs

    def _lift_stage1_candidates(
        self,
        candidates: list[tuple[Any, str | None]],
    ) -> int:
        """Apply the per-row rewrite for each (row, session_id) pair with a match."""
        lifted_count = 0
        for row, session_id in candidates:
            if session_id is None:
                continue
            self._repository.overwrite_summary_text_for_codex_stage1(
                session_id=session_id,
                new_summary_text=row.rollout_summary,
            )
            try:
                self._summary_writer.push_summary_chunk(
                    session_id=session_id,
                    chunk_index=0,
                    summary_text=row.rollout_summary,
                    generated_by_client_id=(
                        "internal:auto_summarize:codex_stage1_seed"
                    ),
                )
            except Exception:
                logger.exception(
                    "lift_codex_stage1_summaries: chunk push failed for "
                    "session_id=%s; continuing with remaining candidates",
                    session_id,
                )
                continue
            lifted_count += 1
        return lifted_count

    def summarize_quiescent_sessions(
        self,
        quiescence_minutes: int = 10,
        batch_size: int = 50,
    ) -> dict[str, Any]:
        """Cron heartbeat: (re)start the singleton drain-until-empty summarizer.

        Does ZERO summarization on the calling (action-queue) thread — it only
        submits the whole drain (:meth:`_drain_all_quiescent`) to the single-slot
        :class:`BoundedSummaryExecutor` and returns in milliseconds (WI-0: an
        inline model call would park the queue). Returns ``{"drainer": "started"}``
        if this fire launched the drainer, or ``{"drainer": "already_running"}``
        if the slot is held (a drainer is already active → this fire is a no-op).
        The drain's per-session counts are logged when it completes (it runs
        asynchronously, so they cannot ride this return). ``batch_size`` is the
        drainer's per-iteration page (clamped by the read to 1..50), NOT a
        per-fire cap — the drainer loops until nothing eligible remains.
        """
        if self._inference_service is None:
            raise RuntimeError(
                "summarize_quiescent_sessions requires inference_service "
                "to be bound at session_ledger_service construction",
            )
        started = self._summary_executor.submit(
            lambda: self._drain_all_quiescent(
                quiescence_minutes=int(quiescence_minutes),
                batch_size=int(batch_size),
            ),
        )
        outcome = "started" if started else "already_running"
        logger.info(
            "auto-summarize drain %s (quiescence=%dm, batch=%d)",
            outcome, int(quiescence_minutes), int(batch_size),
        )
        return {
            "drainer": outcome,
            "quiescence_minutes": int(quiescence_minutes),
            "batch_size": int(batch_size),
        }

    def _drain_all_quiescent(
        self,
        *,
        quiescence_minutes: int,
        batch_size: int,
    ) -> dict[str, int]:
        """Drain every eligible quiescent session, in series, until none remain.

        Runs on the executor's daemon thread (off the action queue), so its
        synchronous inference calls cannot park the queue. Two properties:

        * LIVENESS (termination): ``attempted`` holds every id tried this drain;
          each pass processes only the fresh (not-yet-attempted) rows and stops
          when a page surfaces none. It grows ≥1 per pass over a finite universe,
          so the loop always terminates — even for a ``"skipped"`` return, which
          (in the transient inference-empty case) writes neither a ``__summary``
          row nor a sentinel and would otherwise re-list forever.
        * COVERAGE: progress requires processed rows to LEAVE eligibility.
          Summarized (→ ``__summary`` row) AND deterministic no-content skips
          (→ sentinel via ``_mark_unsummarizable``) both do, so no persistent
          no-content cluster can pin the DESC (newest-first) head page and strand
          older backlog behind it. Only a TRANSIENT inference-empty cluster
          filling the whole newest page can stall ONE drain
          (``_log_drain_stall``); it self-clears — a later cron-fired drain
          re-picks it once the backend recovers.
        """
        attempted: set[str] = set()
        counts = {"summarized": 0, "marked_trivial": 0, "skipped": 0}
        iterations = 0
        while True:
            candidates = self._repository.list_quiescent_sessions(
                quiescence_minutes=quiescence_minutes,
                limit=batch_size,
                trivial_sentinel=_AUTO_SUMMARIZE_TRIVIAL_SENTINEL,
            )
            if not candidates:
                break  # clean drain — nothing eligible remains
            fresh = [
                row for row in candidates if str(row.get("id")) not in attempted
            ]
            if not fresh:
                # Persistent-skip clog: candidates remain but all were already
                # tried this drain. The ``attempted`` set stops the spin; surface
                # it loudly so a real coverage gap is never a silent no-op.
                _log_drain_stall(candidates)
                break
            iterations += 1
            for row in fresh:
                session_id = str(row.get("id"))
                attempted.add(session_id)
                counts[_summarize_row(self._summarize_one_session, session_id, row)] += 1
        logger.info(
            "auto-summarize DRAIN complete: examined=%d summarized=%d "
            "marked_trivial=%d skipped=%d iterations=%d (quiescence=%dm, batch=%d)",
            len(attempted), counts["summarized"], counts["marked_trivial"],
            counts["skipped"], iterations, quiescence_minutes, batch_size,
        )
        return {
            "sessions_examined": len(attempted),
            "sessions_summarized": counts["summarized"],
            "sessions_marked_trivial": counts["marked_trivial"],
            "sessions_skipped": counts["skipped"],
            "drain_iterations": iterations,
        }

    def _summarize_one_session(
        self,
        session_id: str,
        *,
        existing_summary_text: str | None = None,
        source_kind: str | None = None,
    ) -> str:
        """One quiescent-session pass: seed → extraction → trivial → inference.

        Operator ruling 2026-06-01 (Bug 1 fix) drives the branch order:

        0. If the session already has ``summary_text`` set (operator-set
           ``custom_title`` per 2026-05-31 Architect §3, NOT the trivial
           sentinel), push that text through ``push_summary_chunk`` so it
           becomes searchable too — Architect's authoritative-title
           mapping was previously the silent reason claude_code sessions
           never got embedded by M6.
        1. Cheap SQL for an existing claude_code ``away_summary`` recap —
           if present, write it as the summary chunk (zero inference).
        2. DETERMINISTICALLY unsummarizable → write the trivial sentinel and
           return ``"marked_trivial"`` so the session LEAVES eligibility (never
           re-listed). Three cases: below the trivial floor (< 4 events OR no
           assistant turns), an empty timeline, and an empty transcript (all
           events blob-offloaded, content_text NULL). Marking is what stops a
           no-content cluster from pinning the DESC head page (Reviewer-C, §BLOCKER).
        3. Otherwise build a transcript and run the inference fallback
           SYNCHRONOUSLY (this method only ever runs on the drain's daemon
           thread, so the model call cannot park the action queue). A usable
           completion is pushed and returns ``"summarized"``; an empty/failed
           completion is a TRANSIENT skip → returns ``"skipped"`` WITHOUT a
           sentinel (summary_text stays NULL → deliberately re-picked on a later
           drain so a recovered backend self-resolves it).

        Returns one of
        ``{"summarized", "marked_trivial", "skipped"}``.
        The ``generated_by_client_id`` on each push attributes which
        branch produced the summary so post-hoc audits can split
        custom_title seeds, extracted recaps, and inference output.
        M19 added ``source_kind`` so branch 0 can pick a source-specific
        seed discriminator (e.g. Codex state_5 → ``codex_state_title_seed``)
        per v2 §5.5; sources not present in
        ``_SEED_DISCRIMINATOR_BY_SOURCE_KIND`` keep the pre-M19 default.
        """
        if (
            existing_summary_text
            and existing_summary_text != _AUTO_SUMMARIZE_TRIVIAL_SENTINEL
        ):
            seed_discriminator = _SEED_DISCRIMINATOR_BY_SOURCE_KIND.get(
                source_kind or "",
                _AUTO_SUMMARIZE_CLIENT_ID_CUSTOM_TITLE,
            )
            self._summary_writer.push_summary_chunk(
                session_id=session_id,
                chunk_index=0,
                summary_text=existing_summary_text,
                generated_by_client_id=seed_discriminator,
            )
            return "summarized"

        extracted = self._repository.find_latest_away_summary_for_session(
            session_id,
        )
        if extracted:
            self._summary_writer.push_summary_chunk(
                session_id=session_id,
                chunk_index=0,
                summary_text=extracted,
                generated_by_client_id=_AUTO_SUMMARIZE_CLIENT_ID_EXTRACTED,
            )
            return "summarized"

        events = self._repository.get_session_timeline(
            session_id=session_id,
            after_sequence=0,
            limit=_AUTO_SUMMARIZE_MAX_EVENTS,
        )
        if not events:
            # Deterministic skip: last_event_at set but an empty timeline.
            # Sentinel-mark so it LEAVES eligibility — else it pins the DESC
            # (newest-first) head page forever and strands older backlog behind
            # it (Reviewer-C MEDIUM, 2026-07-02).
            return _mark_unsummarizable(self._repository, session_id)

        if _is_trivial_session(events):
            return _mark_unsummarizable(self._repository, session_id)

        transcript = _assemble_transcript(events, _AUTO_SUMMARIZE_MAX_CHARS)
        if not transcript:
            # Deterministic skip: every event is blob-offloaded (content_text
            # NULL) so the transcript assembles empty. Sentinel-mark for the same
            # head-block reason. (A future un-blob summarizer would clear the
            # sentinel to re-enable these substantive sessions.)
            return _mark_unsummarizable(self._repository, session_id)
        # Inference runs SYNCHRONOUSLY here — this method only ever runs on the
        # drain's daemon thread (off the action queue), so a model call cannot
        # park the queue. NB the provider's ``timeout_seconds`` is now
        # load-bearing for SINGLETON liveness: a hung call holds the single drain
        # slot, so a future regression to None/huge would wedge every cron fire
        # (TMO-01 — currently a verified positive int, risk is future-only).
        summary_text = _request_inference_summary(
            inference_service=self._inference_service,
            transcript=transcript,
        )
        if not summary_text:
            # TRANSIENT skip (backend empty/down): deliberately NOT sentinel-marked
            # → re-picked on a later drain so it self-resolves. The attempted-set
            # prevents an intra-drain spin; a same-fire stall self-clears next fire.
            return "skipped"
        self._summary_writer.push_summary_chunk(
            session_id=session_id,
            chunk_index=0,
            summary_text=summary_text,
            generated_by_client_id=_AUTO_SUMMARIZE_CLIENT_ID_INFERRED,
        )
        return "summarized"

    def ensure_periodic_summarize_schedule(
        self,
        cadence_minutes: int = 10,
        tag: str = "ledger:periodic_summarize",
    ) -> dict[str, Any]:
        """Idempotently install a cron firing summarize_quiescent_sessions every N minutes."""
        cron_expression, cleared_count = clear_and_prep_periodic_cron(
            self._scheduling_service, cadence_minutes=int(cadence_minutes), tag=tag,
        )
        # The create_cron_schedule call lives HERE (not in a shared helper) so its
        # literal process_key is AST-visible to the whole-tree C5.1 cron-target
        # gate, which grants the EDGE_SINK exemption only for a resolvable literal.
        create_result = self._scheduling_service.create_cron_schedule(
            cron_expression=cron_expression,
            actions=[{
                "process_key": (
                    "service_interface::session_ledger_service::summarize_quiescent_sessions"
                ),
                "arguments": {},
            }],
            label="Ledger periodic auto-summarize",
            tags=[tag],
            state={
                "flow_id": _LEDGER_PERIODIC_SUMMARIZE_FLOW_ID,
                "session_id": _LEDGER_PERIODIC_SUMMARIZE_SESSION_ID,
            },
        )
        return periodic_cron_result(
            create_result, tag=tag, cadence_minutes=int(cadence_minutes),
            cleared_count=cleared_count,
        )


def _summarize_row(
    summarize_one: Callable[..., str],
    session_id: str,
    row: dict[str, object],
) -> str:
    """Summarize one drain row; map any per-session error to ``"skipped"``.

    A module-level free function (not a method) so it stays off
    ``SessionLedgerSummarizeMixin``'s god-class LOC budget. It owns the
    per-session ``try/except`` so the drain's counting never sees an
    exception leak past here — one bad session cannot kill the drain.
    """
    existing_summary = row.get("summary_text")
    existing_summary_text = (
        existing_summary if isinstance(existing_summary, str) else None
    )
    row_source_kind = row.get("source_kind")
    source_kind = row_source_kind if isinstance(row_source_kind, str) else None
    try:
        return summarize_one(
            session_id,
            existing_summary_text=existing_summary_text,
            source_kind=source_kind,
        )
    except Exception:  # noqa: BLE001 — one bad session must not kill the drain
        logger.exception(
            "auto-summarize failed for session_id=%s; continuing drain",
            session_id,
        )
        return "skipped"


def _log_drain_stall(candidates: list[dict[str, object]]) -> None:
    """Warn that the drain stopped with un-summarizable candidates still queued.

    Fires when a page returns only sessions already attempted this drain — a
    persistent-skip clog (inference backend down, or a no-events / empty-
    transcript anomaly). Kept visible so a coverage gap is never a silent no-op.
    """
    logger.warning(
        "auto-summarize drain STALLED: %d candidate(s) remain but all were "
        "already attempted this drain (persistent skip / clog); ids=%s. "
        "Retried on the next cron-fired drain.",
        len(candidates),
        [str(row.get("id")) for row in candidates[:20]],
    )


def _mark_unsummarizable(repository: Any, session_id: str) -> str:
    """Sentinel-mark a DETERMINISTICALLY-unsummarizable session so it leaves the
    quiescent-eligibility set and returns ``"marked_trivial"``.

    Used for the genuinely-trivial floor AND the two deterministic no-content
    skips (empty timeline; all-blob-offloaded / empty transcript). Marking them
    the way the trivial floor does is what stops a no-content cluster from
    pinning the DESC (newest-first) head page and permanently stranding older
    backlog behind it (Reviewer-C MEDIUM, 2026-07-02). TRANSIENT skips
    (inference-returned-empty, caught exceptions) are deliberately NOT marked —
    they must stay eligible so a later drain re-picks and self-resolves them.
    """
    repository.mark_session_summary_text(
        session_id=session_id, summary_text=_AUTO_SUMMARIZE_TRIVIAL_SENTINEL,
    )
    return "marked_trivial"


def _assemble_transcript(events: list[dict[str, Any]], max_chars: int) -> str:
    """Render an event-row list into a bounded transcript for summarization.

    Output is intentionally simple — newline-separated ``role: text`` lines —
    so the summarizer prompt stays small and deterministic. Quarantined or
    blobbed events have no inline content; their column is null and we skip
    them rather than emitting empty lines. Stops when ``max_chars`` would be
    exceeded; the cap defends against a runaway-long session blowing out the
    inference prompt.
    """
    pieces: list[str] = []
    used = 0
    for event in events:
        role = str(event.get("role") or "")
        if role not in _CONVERSATION_ROLES:
            continue  # only user/assistant — skip tool, system, null-role noise
        text = event.get("content_text")
        if not isinstance(text, str) or not text:
            continue
        line = f"{role}: {text}\n"
        if used + len(line) > max_chars:
            break
        pieces.append(line)
        used += len(line)
    return "".join(pieces).strip()


def _is_trivial_session(events: list[dict[str, object]]) -> bool:
    """Trivial-session predicate (operator ruling 2026-06-01 D8; conversation-only 2026-06-30).

    Counts ONLY ``user``/``assistant`` conversation events — tool, system, and
    null-role noise is ignored (operator 2026-06-30: "I only care about user and
    assistant messages"). A session is trivial when **either** it has fewer than
    ``_AUTO_SUMMARIZE_TRIVIAL_MIN_EVENTS`` (4) conversation events **or** it
    contains zero ``role='assistant'`` events. Both make the transcript too thin
    to summarize usefully — the cron writes the sentinel and moves on rather than
    burning inference tokens on a noise-only session.
    """
    conversation = [
        event
        for event in events
        if str(event.get("role") or "") in _CONVERSATION_ROLES
    ]
    if len(conversation) < _AUTO_SUMMARIZE_TRIVIAL_MIN_EVENTS:
        return True
    assistant_value = MessageRole.ASSISTANT.value
    return not any(
        str(event.get("role") or "") == assistant_value for event in conversation
    )


def _request_inference_summary(*, inference_service: Any, transcript: str) -> str:
    """Call ``inference_service.generate_completion`` for a 2-4 sentence summary.

    Returns an empty string when the inference call fails or returns nothing
    usable — the caller treats that as a skip, not a fatal error. The cron
    re-tries on the next firing.
    """
    result = _call_inference_chat(inference_service, transcript)
    return _extract_summary_text(result)


def _call_inference_chat(inference_service: Any, transcript: str) -> Any:
    request = InferenceRequest(
        [
            {"role": "system", "content": _AUTO_SUMMARIZE_PROMPT},
            {
                "role": "user",
                "content": _AUTO_SUMMARIZE_USER_TEMPLATE.format(transcript=transcript),
            },
        ],
        temperature=_AUTO_SUMMARIZE_INFERENCE_TEMPERATURE,
        max_tokens=_AUTO_SUMMARIZE_INFERENCE_MAX_TOKENS,
        # Plain-prose summary — no action JSON, no response_format schema.
        use_structured_output=False,
        context_metadata={"purpose": "session_ledger_auto_summarize"},
    )
    try:
        return inference_service.generate_completion(request)
    except Exception:  # noqa: BLE001 — defensive boundary; logged here, treated as skip upstream
        logger.exception(
            "inference_service.generate_completion raised during auto-summarize",
        )
        return None


def _extract_summary_text(result: Any) -> str:
    """Pull the summary string out of the ActionResult envelope.

    Tracks the canonical extraction at ``inference_transaction._invoke_and_extract``:
    ``result.data.result.completion`` is the structured-output happy path.
    Falls back to the looser ``data.text`` / ``data.message.content`` shapes
    some providers return so the auto-summarize path stays vendor-tolerant.
    """
    data = _envelope_payload(result)
    if data is None:
        return ""
    for extractor in _SUMMARY_TEXT_EXTRACTORS:
        text = extractor(data)
        if text:
            return text
    return ""


def _envelope_payload(result: Any) -> dict[str, Any] | None:
    """Unwrap an ActionResult-shaped envelope to its inner data dict, or ``None``."""
    if not isinstance(result, dict):
        return None
    if result.get("error"):
        return None
    if result.get("action_status") not in (None, "completed"):
        return None
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return data if isinstance(data, dict) else None


def _strip_or_empty(value: Any) -> str:
    """Return ``value.strip()`` for non-empty strings; ``""`` otherwise."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return ""


def _extract_completion_field(data: dict[str, Any]) -> str:
    """Canonical structured-output shape: ``data.result.completion``."""
    inner = data.get("result")
    if not isinstance(inner, dict):
        return ""
    return _strip_or_empty(inner.get("completion"))


def _extract_flat_completion_field(data: dict[str, Any]) -> str:
    """Flat non-structured-output shape: ``data.completion``.

    ``_call_inference_chat`` requests ``use_structured_output=False``, so
    providers return the completion at the top level of ``data`` rather than
    nested under ``data.result``. ``mock_inference_plugin`` and the live
    Qwen path both follow this shape for non-structured calls.
    """
    return _strip_or_empty(data.get("completion"))


def _extract_text_field(data: dict[str, Any]) -> str:
    """Loose provider shape: ``data.text``."""
    return _strip_or_empty(data.get("text"))


def _extract_message_content(data: dict[str, Any]) -> str:
    """Chat-style provider shape: ``data.message.content``."""
    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    return _strip_or_empty(message.get("content"))


_SUMMARY_TEXT_EXTRACTORS: tuple[
    Callable[[dict[str, Any]], str], ...
] = (
    _extract_completion_field,
    _extract_flat_completion_field,
    _extract_text_field,
    _extract_message_content,
)


__all__ = ["SessionLedgerSummarizeMixin"]
