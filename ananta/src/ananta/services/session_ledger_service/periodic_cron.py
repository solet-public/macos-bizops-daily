"""Shared periodic-cron-installer plumbing for the ledger's EDGE_SINK crons.

Schema-debt-external-id lane, service.py decomposition seam (2026-08-07).
Extracted to a neutral shared module — not left in ``service.py`` — after
measuring that ``ensure_periodic_poll_schedule`` (which stays in
``service.py``) has its own separate inline pre/post step logic and reuses
only ``extract_schedule_id``, not ``clear_and_prep_periodic_cron`` /
``periodic_cron_result``. Those two full pre/post steps are used exclusively
by ``summarize.py``'s ``ensure_periodic_summarize_schedule`` and
``embedding_drain.py``'s ``ensure_periodic_embed_schedule``. A shared neutral
module (rather than one mixin importing from the other, or from service.py)
keeps both mixins self-contained and avoids a circular import with
``service.py`` (which imports both mixins for its own class bases), while
still letting ``service.py`` import the one function (``extract_schedule_id``)
it genuinely shares.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

RELOAD_SAFE = True


def extract_schedule_id(envelope: Any) -> str:
    """Pull schedule_id from either an action-envelope or a raw data dict.

    The scheduling plugin returns ``{"data": {"schedule_id": "..."}, ...}``;
    the service-interface direct-dispatch surface may return the raw inner
    dict. Both shapes resolve cleanly to the same string here.
    """
    if not isinstance(envelope, dict):
        return ""
    if "schedule_id" in envelope:
        return str(envelope["schedule_id"])
    data = envelope.get("data")
    if isinstance(data, dict) and "schedule_id" in data:
        return str(data["schedule_id"])
    return ""


def clear_and_prep_periodic_cron(
    scheduling_service: Any, *, cadence_minutes: int, tag: str,
) -> tuple[str, int]:
    """Shared PRE step for the ledger's periodic EDGE_SINK cron installers.

    Fails fast on missing scheduling / out-of-range cadence, builds the cron
    expression, and clears any existing schedules for ``tag``; returns
    ``(cron_expression, cleared_count)``. The ``create_cron_schedule`` call
    itself deliberately stays in each installer so its LITERAL ``process_key``
    is AST-visible to the whole-tree C5.1 cron-target gate (which grants the
    EDGE_SINK exemption only for a resolvable literal — a variable process_key
    reads as an un-exempt EDGE target).
    """
    if scheduling_service is None:
        raise RuntimeError(
            "ensure periodic schedule requires scheduling_service to be bound "
            "at session_ledger_service construction",
        )
    if not 1 <= int(cadence_minutes) <= 59:
        raise ValueError(
            f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
        )
    clear_result = scheduling_service.clear_scheduled_actions_by_tag(tag=tag)
    cleared_count = (
        int((clear_result or {}).get("data", {}).get("cleared_count", 0))
        if isinstance(clear_result, dict)
        else 0
    )
    return f"*/{int(cadence_minutes)} * * * *", cleared_count


def periodic_cron_result(
    create_result: Any, *, tag: str, cadence_minutes: int, cleared_count: int,
) -> dict[str, Any]:
    """Shared POST step: extract schedule_id, report created-vs-normalized, log."""
    schedule_id = extract_schedule_id(create_result)
    outcome = "normalized" if cleared_count > 0 else "created"
    logger.info(
        "session_ledger periodic schedule %s: schedule_id=%s tag=%s cadence=%dm",
        outcome, schedule_id, tag, int(cadence_minutes),
    )
    return {
        "outcome": outcome,
        "schedule_id": schedule_id,
        "tag": tag,
        "cadence_minutes": int(cadence_minutes),
        "cleared_count": cleared_count,
    }


__all__ = [
    "clear_and_prep_periodic_cron",
    "extract_schedule_id",
    "periodic_cron_result",
]
