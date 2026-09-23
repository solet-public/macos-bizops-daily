"""Polling-lease heartbeat helper.

The :class:`LeaseHeartbeat` extends the session-ledger importer's polling
lease across long walks. Each yielded event in the importer's
``_poll_one_session`` calls :meth:`check`; the check refreshes the lease
when the elapsed time since the last refresh reaches half the TTL.

Per v8 §D14.B / Codex v7 B2: ChatGPT export's ``_poll_one_session`` walks
the entire ``mapping`` tree of one conversation in a single ``read_events``
loop. With session-boundary-only heartbeat, a multi-hour conversation walk
would not heartbeat once. Elapsed-time check inside the loop closes that.

Extracted from :mod:`ananta.llm.session_ledger.importer` per 2026-06-13
god-class gate (Cycle 4a) — the class is pure policy and has no
filesystem / blob / cursor dependencies; collocating it with the
``SessionLedgerImporter`` class pushed the importer over the 500-LOC
coherence-aware god-class threshold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ananta.llm.session_ledger.repository import LeaseLostError

if TYPE_CHECKING:
    from ananta.llm.session_ledger.repository import (
        PollingLeaseHandle,
        SessionLedgerRepository,
    )


# Module-level RELOAD_SAFE marker — pure class with no held service references.
RELOAD_SAFE = True


class LeaseHeartbeat:
    """Elapsed-time polling-lease refresher.

    ``check()`` is called once per yielded event (cheap when not due). It
    refreshes the lease when ``(now - last_refresh_at) >= ttl_seconds // 2``
    so a single-conversation read loop that takes longer than the TTL still
    keeps the lease alive without depending on session-boundary cadence.

    Raises :class:`LeaseLostError` when
    :meth:`SessionLedgerRepository.refresh_polling_lease` returns ``None``
    (ownership lost to another poller).
    """

    __slots__ = ("_repository", "_handle", "_ttl_seconds", "_last_refresh_at")

    def __init__(
        self,
        repository: SessionLedgerRepository,
        handle: PollingLeaseHandle,
        ttl_seconds: int,
    ) -> None:
        self._repository = repository
        self._handle = handle
        self._ttl_seconds = ttl_seconds
        # Floor is wall-clock acquire moment, NOT handle.lease_until - ttl
        # (avoids any Postgres/Python clock skew in the arithmetic).
        self._last_refresh_at = datetime.now(UTC)

    def check(self) -> None:
        now = datetime.now(UTC)
        elapsed = (now - self._last_refresh_at).total_seconds()
        if elapsed < (self._ttl_seconds / 2):
            return
        new_handle = self._repository.refresh_polling_lease(
            self._handle, ttl_seconds=self._ttl_seconds,
        )
        if new_handle is None:
            raise LeaseLostError(
                f"polling lease lost for source {self._handle.source_id} "
                f"during heartbeat (last_refresh_at={self._last_refresh_at}, "
                f"elapsed={elapsed:.1f}s, ttl={self._ttl_seconds}s)"
            )
        self._handle = new_handle
        self._last_refresh_at = now

    @property
    def handle(self) -> PollingLeaseHandle:
        return self._handle


__all__ = ["LeaseHeartbeat"]
