"""Declared parked-driver recovery helpers shared by lifecycle verbs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .session_hosts import ParkInterruptingDriverChannel

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from .session_hosts import DriverChannel


def interrupt_parked_channel(channel: DriverChannel) -> str | None:
    """Use the declared parked-pane recovery seam, never pane prose."""
    if not isinstance(channel, ParkInterruptingDriverChannel):
        return None
    return channel.interrupt_park()


def send_delivery_notice[OutcomeT](
    channel: DriverChannel,
    *,
    row: dict[str, Any],
    recipient_agent_instance_id: str,
    notice: str,
    detail_sink: Callable[[str], None] | None,
    park_detail: str | None,
    driver_sent: OutcomeT,
    driver_error: OutcomeT,
    record_error_detail: Callable[[Callable[[str], None] | None, Exception], None],
    logger: logging.Logger,
) -> OutcomeT:
    """Send one best-effort notice and expose only its measured outcome."""
    try:
        channel.send(notice)
    except Exception as exc:  # noqa: BLE001 -- delivery must not fail its durable parent send
        logger.warning(
            "drive_on_delivery: driver channel raised for %s",
            row.get("agent_instance_id") or recipient_agent_instance_id,
            exc_info=True,
        )
        record_error_detail(detail_sink, exc)
        return driver_error
    if detail_sink is not None and park_detail is not None:
        detail_sink(park_detail)
    return driver_sent


def drive_session_channel(
    channel: DriverChannel,
    *,
    current: str,
    parked_state: str,
    text: str,
    agent_instance_id: str,
    send_driver_text: Callable[[DriverChannel, str], None],
    verify_drive_effect: Callable[[DriverChannel, str, str], str],
) -> tuple[str, str | None]:
    """Interrupt a declared parked pane before one ordinary verified drive."""
    park_detail = interrupt_parked_channel(channel) if current == parked_state else None
    send_driver_text(channel, text)
    return verify_drive_effect(channel, agent_instance_id, text), park_detail
