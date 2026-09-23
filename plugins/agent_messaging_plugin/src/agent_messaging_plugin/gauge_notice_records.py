"""GAU-21 — read the durable record of which gauge notices actually fired.

The sweep's gauge notices exist, operationally, only as in-memory bridge
events: a restart loses them, reading them removes them, and nothing is keyed
on type. So "did the detector alarm on this session?" has had no answer that
survives the moment, and "it never alarmed" and "it alarmed and reached nobody"
have been the same silence.

This verb answers it from the durable record instead — by type, by subject, and
over a time window, newest first, WITHOUT consuming anything. The
non-destructive property is the load-bearing one: a verifier built on the
bridge queue would race the steward and could swallow the very notice the
steward needed, which is why the GAU-15 tamper canary could not be verified
against that queue and can be verified against this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .context_status_verbs import VerbError, resolve_status_row
from .gauge_notice_record_store import (
    GAUGE_NOTICE_RETENTION,
    MAX_READ_ROWS,
    read_gauge_notice_records,
)
from .schema import NOTICE_DELIVERY_OUTCOMES
from .session_context_status_store import AmbiguousAgentSessionIdError
from .session_sweep import EVENT_GAUGE_COVERAGE_NOTICE, EVENT_GAUGE_STALE_NOTICE

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

NOTICE_TYPES = (EVENT_GAUGE_STALE_NOTICE, EVENT_GAUGE_COVERAGE_NOTICE)
"""The notice families this verb reads.

Imported from the sweep rather than re-spelled as string literals: these ARE
the emit site's own event types, and two copies of a type name drift silently
in exactly the direction that makes a by-type read return nothing while
looking correct.
"""


def _entry(row: dict[str, Any]) -> dict[str, Any]:
    """One record, with every column surfaced explicitly.

    Absent values stay ``None`` rather than becoming 0 or "": a coverage notice
    genuinely has no gauge timestamp to report, and a row whose release could
    not be self-identified genuinely has no release id. Both are facts a reader
    must be able to see, and both would be destroyed by a tidy default.
    """
    return {
        "notice_type": row.get("notice_type"),
        "agent_instance_id": row.get("agent_instance_id"),
        "emitted_at": row.get("emitted_at"),
        "steward_instance_id": row.get("steward_instance_id"),
        "delivery_outcome": row.get("delivery_outcome"),
        "release_id": row.get("release_id"),
        "threshold_s": row.get("threshold_s"),
        "observed_s": row.get("observed_s"),
        "last_report_alive_at": row.get("last_report_alive_at"),
        "gauge_measured_at": row.get("gauge_measured_at"),
    }


def _resolved_subject(
    state: StateManagementInterface, agent_instance_id: str,
) -> tuple[str, str]:
    """``(the id notices are keyed on, how it was reached)``.

    Uses the SAME GAU-07 watch-id join every other gauge verb uses, through the
    ONE copy of it, because a caller holding the id a session is LISTED under
    (``peer_list`` publishes the watch id for any watcher-held session) must not
    have to know which of the two live id schemes the sweep happened to write.

    ``resolve_status_row`` returns the cache ROW and how it was reached — not an
    id — so the id is taken from the row. Falls back to the id as given when no
    cache row resolves: notices are keyed on the subject the SWEEP saw, and a
    session with recorded notices but no current cache row (the arrested case
    this whole family exists for) must stay readable rather than silently
    resolving to empty and matching nothing.
    """
    try:
        row, id_resolution = resolve_status_row(state, agent_instance_id)
    except AmbiguousAgentSessionIdError as exc:
        raise VerbError("ambiguous_agent_session_id", str(exc)) from exc
    keyed_id = str(row.get("agent_instance_id") or "") if row else ""
    return (keyed_id or agent_instance_id, id_resolution)


def gauge_notice_records(
    state: StateManagementInterface,
    *,
    notice_type: str | None = None,
    agent_instance_id: str | None = None,
    since: str | None = None,
    limit: int = MAX_READ_ROWS,
) -> dict[str, Any]:
    """Durable gauge notices matching this filter, newest first.

    Every filter is optional and an omitted one does NOT narrow — an unfiltered
    call is "every notice on file", not "notices with a NULL subject". That
    distinction is worth stating because the natural mis-implementation
    (matching ``None`` against the column) returns a plausible, small, wrong
    answer rather than an error.

    ``truncated`` is published rather than implied, on the same argument as its
    sibling verb: a reader asking "when did this last alarm" is reading the
    OLDEST row on the page, and a silently capped page answers with a boundary
    the reader chose by accident.
    """
    if notice_type is not None and notice_type not in NOTICE_TYPES:
        raise VerbError(
            "invalid_argument",
            f"notice_type must be one of {list(NOTICE_TYPES)}, got {notice_type!r}.",
        )
    subject: str | None = None
    id_resolution: str | None = None
    if agent_instance_id is not None:
        if not agent_instance_id.strip():
            raise VerbError(
                "missing_argument",
                "agent_instance_id was supplied but empty; omit it to read all "
                "subjects, rather than passing a blank that matches nothing.",
            )
        subject, id_resolution = _resolved_subject(state, agent_instance_id)
    rows, truncated = read_gauge_notice_records(
        state,
        notice_type=notice_type,
        agent_instance_id=subject,
        since=since,
        limit=limit,
    )
    entries = [_entry(r) for r in rows]
    return {
        "entries": entries,
        "returned": len(entries),
        "truncated": truncated,
        "queried_agent_instance_id": agent_instance_id,
        "agent_instance_id": subject,
        "id_resolution": id_resolution,
        "notice_type": notice_type,
        "since": since,
        "retention": GAUGE_NOTICE_RETENTION,
        "delivery_outcomes": list(NOTICE_DELIVERY_OUTCOMES),
        "notice_types": list(NOTICE_TYPES),
    }


__all__ = [
    "NOTICE_TYPES",
    "gauge_notice_records",
]
