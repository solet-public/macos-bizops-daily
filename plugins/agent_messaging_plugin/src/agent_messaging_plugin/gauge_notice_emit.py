"""GAU-21 — the one path a gauge notice takes from "the sweep decided to fire"
to "delivered, and durably recorded either way".

Both gauge legs (staleness and coverage) route through here, and the shape is
the fix rather than a refactor:

★ THE RECORD IS NOT GATED ON THE STEWARD BINDING. Both legs used to resolve the
steward FIRST and return early when it was ``None``, so an alarm about a session
whose steward is unbound reached nobody AND left nothing behind. From outside
that is identical to a detector that never fired — and it is invisible
precisely to the steward who would otherwise have to notice, because noticing
is what the missing notice was for. ``no_steward_binding`` is a RECORDED
OUTCOME here rather than an early return.

★ DELIVERY STAYS BEST-EFFORT. A delivery fault warns and returns ``False``
rather than raising into the sweep loop, so one unreachable steward never costs
the other rows in that tick their notice. The durable record is attempted
regardless, for the same reason.

Kept out of ``session_sweep`` deliberately: that module sits close to the
maintainability bound, and the composition of a notice record is not sweep
control flow. The sweep resolves the steward — which is its own concern, with
its own fallback path — and hands the resolved binding here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .gauge_notice_record_store import record_notice_best_effort
from .schema import (
    NOTICE_DELIVERY_APPEND_FAILED,
    NOTICE_DELIVERY_APPENDED,
    NOTICE_DELIVERY_NO_STEWARD_BINDING,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .models import BridgeBinding

logger = logging.getLogger(__name__)


def deliver_and_record_gauge_notice(
    state: StateManagementInterface,
    *,
    bridge_manager: BridgeSessionManager,
    binding: BridgeBinding | None,
    agent_instance_id: str,
    notice_type: str,
    prose: Callable[[], str],
    flow_id: str,
    clock: datetime,
    threshold_s: float,
    observed_s: float,
    last_report_alive_at: datetime | None = None,
    gauge_measured_at: datetime | None = None,
) -> bool:
    """Deliver one gauge notice if there is anywhere to deliver it, record that
    it fired either way, and report whether delivery happened.

    ``binding`` of ``None`` is a real outcome rather than a reason to skip: it
    means the sweep could not resolve a steward for this session's spawner, and
    recording that is the entire point.

    ``prose`` is a CALLABLE rather than a string so the notice text — which is
    long, and composed per row — is built only when there is actually a steward
    to send it to.
    """
    delivered = False
    if binding is None:
        outcome = NOTICE_DELIVERY_NO_STEWARD_BINDING
    else:
        try:
            bridge_manager.append_event(
                binding.bridge_id, notice_type, prose(), {"flow_id": flow_id},
            )
        except Exception:  # noqa: BLE001 — best-effort notify, never fails the sweep
            logger.warning(
                "session %s %s append failed", agent_instance_id, notice_type,
                exc_info=True,
            )
            outcome = NOTICE_DELIVERY_APPEND_FAILED
        else:
            outcome = NOTICE_DELIVERY_APPENDED
            delivered = True
    record_notice_best_effort(
        state,
        notice_type=notice_type,
        agent_instance_id=agent_instance_id,
        delivery_outcome=outcome,
        steward_instance_id=None if binding is None else binding.agent_instance_id,
        clock=clock,
        threshold_s=threshold_s,
        observed_s=observed_s,
        last_report_alive_at=last_report_alive_at,
        gauge_measured_at=gauge_measured_at,
    )
    return delivered


__all__: list[str] = ["deliver_and_record_gauge_notice"]
