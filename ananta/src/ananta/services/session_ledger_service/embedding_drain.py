"""EmbeddingDrain domain mixin for ``SessionLedgerService`` (LED-01 lane).

Schema-debt-external-id lane, service.py decomposition seam (2026-08-07).
Split out of ``service.py`` alongside ``summarize.py`` — see that module's
docstring for the full rationale (seat-ratified per-ABC-family split,
mirroring the repository layer's mixin precedent). A separate module from
``summarize.py`` per :class:`SessionLedgerEmbeddingDrainAPI`'s own docstring:
"a sibling ABC ... so the autonomous drain heartbeat and its boot-time
schedule installer form one coherent, self-contained group."
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ananta.llm.session_ledger.event_embeddings import (
    EventEmbeddingServicesUnavailableError,
)
from ananta.services.session_ledger_service.interfaces.public import (
    SessionLedgerEmbeddingDrainAPI,
)
from ananta.services.session_ledger_service.periodic_cron import (
    clear_and_prep_periodic_cron,
    periodic_cron_result,
)

if TYPE_CHECKING:
    from ananta.llm.session_ledger.event_embeddings import EventEmbeddingWriter
    from ananta.services.session_ledger_service.summary_executor import (
        SummaryExecutor,
    )

logger = logging.getLogger(__name__)

RELOAD_SAFE = True

# System-owned scheduler identifiers for the periodic-embed cron. Mirrors
# the pattern at default_scheduling_plugin.constants.HEARTBEAT_FLOW_ID.
_LEDGER_PERIODIC_EMBED_FLOW_ID = "flow-ledger-periodic-embed"
_LEDGER_PERIODIC_EMBED_SESSION_ID = "sess-ledger-periodic-embed"


class SessionLedgerEmbeddingDrainMixin(SessionLedgerEmbeddingDrainAPI):
    """LED-01 event-embedding drain lane — the cron drainer + its schedule.

    Depends on attributes owned by :class:`SessionLedgerService`'s
    ``__slots__`` (``_event_embedding_writer``, ``_embedding_executor``,
    ``_scheduling_service``) — declared below under ``TYPE_CHECKING`` only,
    same idiom as ``summarize.py``'s ``SessionLedgerSummarizeMixin``.
    """

    __slots__ = ()

    if TYPE_CHECKING:
        _event_embedding_writer: EventEmbeddingWriter
        _embedding_executor: SummaryExecutor
        _scheduling_service: Any

    def ensure_periodic_embed_schedule(
        self,
        cadence_minutes: int = 10,
        tag: str = "ledger:periodic_embed",
    ) -> dict[str, Any]:
        """Idempotently install a cron firing drain_event_embeddings every N minutes."""
        cron_expression, cleared_count = clear_and_prep_periodic_cron(
            self._scheduling_service, cadence_minutes=int(cadence_minutes), tag=tag,
        )
        # Literal process_key inline for the same C5.1 gate-visibility reason as
        # ``ensure_periodic_summarize_schedule``.
        create_result = self._scheduling_service.create_cron_schedule(
            cron_expression=cron_expression,
            actions=[{
                "process_key": (
                    "service_interface::session_ledger_service::drain_event_embeddings"
                ),
                "arguments": {},
            }],
            label="Ledger periodic event-embedding drain",
            tags=[tag],
            state={
                "flow_id": _LEDGER_PERIODIC_EMBED_FLOW_ID,
                "session_id": _LEDGER_PERIODIC_EMBED_SESSION_ID,
            },
        )
        return periodic_cron_result(
            create_result, tag=tag, cadence_minutes=int(cadence_minutes),
            cleared_count=cleared_count,
        )

    def drain_event_embeddings(self, page_size: int = 100) -> dict[str, Any]:
        """Cron heartbeat that (re)starts the singleton event-embedding drainer.

        Thin delegate; the full contract is on the ABC in ``interfaces/public.py``.
        """
        return _start_event_embedding_drain(
            self._embedding_executor,
            self._event_embedding_writer,
            max(1, min(int(page_size), 100)),
        )


def _start_event_embedding_drain(
    executor: SummaryExecutor, writer: Any, page_size: int,
) -> dict[str, Any]:
    """Submit the event-embedding drain to its single slot; report started/no-op.

    Does ZERO embedding on the calling (action-queue) thread — it submits the
    whole cursor-forward drain (:meth:`EventEmbeddingWriter.drain_missing_events`)
    to the single-slot executor and returns in milliseconds with
    ``{"drainer": "started"}`` (this fire launched it) or
    ``{"drainer": "already_running"}`` (slot held → no-op). Per-fire counts are
    logged by the drain when it completes (async), so they cannot ride this
    return.
    """
    started = executor.submit(lambda: _run_event_embedding_drain(writer, page_size))
    outcome = "started" if started else "already_running"
    logger.info("event-embedding drain %s (page_size=%d)", outcome, page_size)
    return {"drainer": outcome, "page_size": page_size}


def _run_event_embedding_drain(writer: Any, page_size: int) -> None:
    """Background body of the ``drain_event_embeddings`` heartbeat.

    Runs on the single-slot embedding-drain thread (off the action queue) so
    the synchronous embedder/vector calls cannot park the queue. Fails soft on
    a vacant-embeddings profile: logs and returns rather than spamming a stack
    trace every cron fire (the drain's own per-page halt handles transient
    embedder outages).
    """
    try:
        result = writer.drain_missing_events(page_size=page_size)
    except EventEmbeddingServicesUnavailableError:
        logger.warning(
            "drain_event_embeddings: embedding/vector services unavailable in "
            "this profile — skipping the event-embedding drain",
        )
        return
    logger.info("event-embedding DRAIN complete: %s", result)


__all__ = ["SessionLedgerEmbeddingDrainMixin"]
