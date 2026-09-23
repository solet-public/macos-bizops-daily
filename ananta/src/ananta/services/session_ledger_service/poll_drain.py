"""Background importer-poll drain runner for the ``trigger_poll`` heartbeat.

Module-level (no state), factored out of ``service.py`` so the ``trigger_poll``
cron heartbeat can delegate the fire-and-forget submit + background poll body
without inflating the service module (mirrors how ``summary_executor.py`` factors
out the single-slot executor concern). Same drain-runner shape as
``_start_event_embedding_drain`` / the auto-summarize drain. The full contract is
on the ``trigger_poll`` ABC and KB
``21_scheduling_service/02_action_queue_fast_return_contract.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.llm.session_ledger.importer import SessionLedgerImporter
    from ananta.services.session_ledger_service.summary_executor import (
        SummaryExecutor,
    )

logger = logging.getLogger(__name__)


def start_importer_poll_drain(
    executor: SummaryExecutor, importer: SessionLedgerImporter,
) -> dict[str, Any]:
    """Submit the importer poll pass to its single slot; report started/no-op.

    Does ZERO importing on the calling (action-queue) thread — an inline pass parks fleet-wide
    dispatch for its whole duration (the 2026-07-10 wedge). Returns ``{"poller": "started"}`` or
    ``{"poller": "already_running"}`` (slot held → no-op); per-pass counts are logged at drain
    completion (async).
    """
    started = executor.submit(lambda: run_importer_poll_drain(importer))
    outcome = "started" if started else "already_running"
    logger.info("ledger poll drain %s", outcome)
    return {"poller": outcome}


def run_importer_poll_drain(importer: SessionLedgerImporter) -> None:
    """Background body of the ``trigger_poll`` heartbeat: one full ``poll_once`` pass on the
    single-slot poll-drain thread (off the action queue), restart-safe via per-source cursors +
    the self-expiring lease. All lease/batch/per-session-failure isolation lives in ``poll_once``.
    """
    report = importer.poll_once()
    logger.info("ledger poll DRAIN complete: %s", report)
