"""In-process spool drainer for the session dispatch bridge (W4 M1.5).

Reads the session-singleton dispatch spool and writes each event into one
solet's ``memory_service`` via the canonical in-process service. M1.5
replaces M1's delete-after-drain with **cursor-based ack** (design §3 D2.2):
once a second reader can exist, a deleting reader would starve the others, so
each drainer instead advances its own ``cursors/<drainer_id>.cursor`` and the
shared janitor (``janitor.py``) deletes only files every live drainer has
passed. The drainer is the plugin's write-surface adapter: it owns the mapping
from spool record to the §5 tag conventions.

**Ack granularity is per-file, not per-line** (design §3 D2.2 / brief D6). M1.5
keeps the producer's one-line-per-file invariant, so per-file == per-line in
practice; and even for a hypothetical multi-line file, re-draining the whole
file on a mid-file failure is safe because the consumer is idempotent (design
§6: lifecycle ``remember`` dedupes by ``(task id, event)``; status
``upsert_memory_by_tag`` is last-write-wins). So on any-line failure the cursor
stays at the prior position and the next tick retries the whole file from line
one — strictly simpler than line-granular ack, with no correctness cost.

**Heartbeat-every-tick** (design §3 D2.2 / brief D3): ``drain_once`` rewrites the
cursor on *every* tick — refreshing the heartbeat — whether or not the position
advanced, so a stuck-but-alive drainer still proves liveness and a genuinely
dead one goes stale and is retired by the janitor.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from ananta.interfaces.memory_service_interface import MemoryServiceInterface

from .cursor import INITIAL_POSITION, next_cursor, read_cursor, write_cursor
from .janitor import (
    RETENTION_CEILING_BYTES,
    RETENTION_CEILING_SECONDS,
    RETIREMENT_THRESHOLD_SECONDS,
    list_spool_files,
    run_janitor,
)
from .spool_schema import (
    EVENT_TASK_COMPLETED,
    EVENT_TASK_CREATED,
    SpoolRecord,
    agent_tag,
    event_tag,
    in_flight_tag,
    parse_spool_line,
    source_tag,
)


class SpoolDrainer:
    """Drains dispatch spool files into ``memory_service`` via cursor-based ack."""

    def __init__(
        self,
        drainer_id: str,
        spool_dir: Path,
        cursor_dir: Path,
        lock_path: Path,
        logger: logging.Logger,
        *,
        retirement_threshold_seconds: float = RETIREMENT_THRESHOLD_SECONDS,
        retention_ceiling_bytes: int = RETENTION_CEILING_BYTES,
        retention_ceiling_seconds: float = RETENTION_CEILING_SECONDS,
    ) -> None:
        self._drainer_id = drainer_id
        self._spool_dir = spool_dir
        self._cursor_dir = cursor_dir
        self._lock_path = lock_path
        self._logger = logger
        self._retirement_threshold_seconds = retirement_threshold_seconds
        self._retention_ceiling_bytes = retention_ceiling_bytes
        self._retention_ceiling_seconds = retention_ceiling_seconds

    def drain_once(self, memory_service: MemoryServiceInterface) -> int:
        """Drain everything past this drainer's cursor, then run the janitor.

        Returns the number of files drained this tick. Reads the cursor (rebuilds
        from the oldest retained file on a corrupt/missing cursor — design §6),
        drains spool files strictly after the cursor position in chronological
        filename order, advances the cursor (and always refreshes the heartbeat),
        then runs the shared janitor under its host-level flock.
        """
        now = datetime.now(UTC)
        previous = read_cursor(self._cursor_dir, self._drainer_id)
        position = previous["position"] if previous is not None else INITIAL_POSITION
        new_position, drained = self._drain_after(position, memory_service)
        write_cursor(
            self._cursor_dir,
            next_cursor(previous, self._drainer_id, new_position, now.isoformat()),
        )
        run_janitor(
            self._spool_dir,
            self._cursor_dir,
            self._lock_path,
            now=now,
            retirement_threshold_seconds=self._retirement_threshold_seconds,
            retention_ceiling_bytes=self._retention_ceiling_bytes,
            retention_ceiling_seconds=self._retention_ceiling_seconds,
        )
        return drained

    def _drain_after(self, position: str, memory_service: MemoryServiceInterface) -> tuple[str, int]:
        """Drain files strictly after ``position`` in filename order.

        Stops (``break``) at the first file that fails to drain so the cursor never
        advances past an undelivered file — the next tick retries that file from
        line one. Returns (new cursor position, files drained)."""
        new_position = position
        drained = 0
        for spool_file in list_spool_files(self._spool_dir):
            if spool_file.name <= position:
                continue
            if not self._drain_file(spool_file, memory_service):
                break
            new_position = spool_file.name
            drained += 1
        return new_position, drained

    def _drain_file(self, spool_file: Path, memory_service: MemoryServiceInterface) -> bool:
        """Write every line of one file to ``memory_service``; True iff all succeed.

        A file with any unparseable line (e.g. a torn mid-append write) or any
        failed write is left un-acked for retry. Does NOT delete the file — the
        janitor deletes it once every live drainer has advanced past it."""
        try:
            lines = spool_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            self._logger.warning("dispatch drainer: cannot read %s: %s", spool_file.name, exc)
            return False
        records = [parse_spool_line(line) for line in lines]
        if not records or any(record is None for record in records):
            return False  # empty / torn / invalid — leave un-acked for retry
        valid = [record for record in records if record is not None]
        try:
            for record in valid:
                self._write_record(record, memory_service)
        except Exception as exc:  # target unreachable / write error — leave un-acked
            self._logger.warning(
                "dispatch drainer: write failed for %s, leaving un-acked for retry: %s",
                spool_file.name,
                exc,
            )
            return False
        return True

    def _write_record(self, record: SpoolRecord, memory_service: MemoryServiceInterface) -> None:
        memory_service.remember(
            content=self._audit_content(record),
            tags=[
                event_tag(record.session_id),
                agent_tag(record.agent),
                source_tag(record.source),
            ],
            session_id=record.session_id,
            embed=False,  # tag-retrieved audit trail: keep semantic recall clean + skip embed cost
        )
        if record.event == EVENT_TASK_CREATED:
            memory_service.upsert_memory_by_tag(
                content=self._in_flight_content(record),
                tag=in_flight_tag(record.session_id),
                session_id=record.session_id,
            )
        elif record.event == EVENT_TASK_COMPLETED:
            memory_service.delete_memories_by_tag(in_flight_tag(record.session_id))

    @staticmethod
    def _audit_content(record: SpoolRecord) -> str:
        return (
            f"{record.event} session={record.session_id} task={record.task_id} "
            f"@{record.received_at} | {record.summary} | payload={record.payload_json}"
        )

    @staticmethod
    def _in_flight_content(record: SpoolRecord) -> str:
        return (
            f"{record.session_id} in flight: task={record.task_id} "
            f"{record.summary} since @{record.received_at}"
        )
