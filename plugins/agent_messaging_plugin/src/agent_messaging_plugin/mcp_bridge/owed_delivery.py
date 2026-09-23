"""Client-side at-least-once delivery of OWED ROLE messages (v10 Control #5).

The :class:`Forwarder` owns the bridge HTTP session and the live ``/events``
long-poll. This module owns the *owed-delivery* concern layered on top of it,
extracted so the already-large ``Forwarder`` stays coherent (the god-class gate
counts its full non-process surface).

* **single-flight emit ledger** (``_emits``, session-scoped): a per-id emit TASK
  keyed on the role ``external_id``, so a message is emitted to the MCP client
  at most once no matter how many paths surface it concurrently.
* **live path:** a role ``peer_message`` arriving on ``/events`` carries the
  Control #5 meta keys (:data:`META_KEY_RECIPIENT_KIND` etc.); the forwarder
  routes it here, which emits it (if new) and re-confirms ``delivered=true``.
* **repair drain:** a periodic, non-overlapping pass that POSTs ``/peer/drain``
  for the oldest owed role rows, emits + confirms each, and re-queries until
  empty — the at-least-once safety net for a role holder that was offline
  (``queued_for_replay``). A re-emit is visibly marked ``[re-emit n/cap ...]``
  (N3) and, past :data:`REEMIT_BODY_HEAD_CHARS`, carries only the head of the
  original prose plus a pointer to the persisted full text (N3-CONDENSE) — a
  re-read costs the whole body every time otherwise.

Both paths converge on :meth:`OwedDeliveryCoordinator._settle`: each caller
AWAITS the one shared per-id emit task and only then issues the ``delivered=true``
flip (POST ``/peer/delivered``), so the flip is issued **only after a confirmed-
successful emit** (M7 idempotent re-confirm) and NEVER on the basis of another
caller's in-flight or failed emit (Codex BLOCKER-2-deep). The flip is idempotent
+ ownership-fenced server-side, so a redundant confirm is a no-op.

The repair loop runs its OWN periodic timer (live ``/events`` traffic does not
suppress it) and is strictly sequential, so passes never overlap; failures back
off with a bounded cap.

A4 (2026-08-04): this module used to cover BOTH role and direct-IMPORTANT owed
kinds ("one delivery discipline"). The direct half (the REL-05 deaf-wake
insurance outbox) retired — sweep_overdue_sessions + _notify_steward_of_overdue
is its successor. This is a surgical extraction, not a module rewrite: every
role-only code path here is untouched byte-for-byte.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING, Any, Final, Protocol

from ananta.llm.agent_messaging.schema import (
    META_KEY_DELIVERY_EXTERNAL_ID,
    META_KEY_RECIPIENT_KEY,
    META_KEY_RECIPIENT_KIND,
    RECIPIENT_KIND_ROLE,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# Channel event type a drained owed delivery (role OR direct) is emitted as —
# identical to the live server-side wake (``peer_dispatch.EVENT_PEER_MESSAGE``)
# so a drained message routes through the forwarder's notification machinery the
# same way a live one does.
EVENT_PEER_MESSAGE: Final[str] = "peer_message"

# Repair-drain cadence. The loop's own timer is independent of ``/events`` so
# live traffic never starves the drain; on failure it backs off, capped.
REPAIR_DRAIN_INTERVAL_S: Final[float] = 15.0
REPAIR_DRAIN_MAX_BACKOFF_S: Final[float] = 120.0
# Per-pass page size handed to ``/peer/drain`` (server clamps to [1, 100]).
REPAIR_DRAIN_PAGE_LIMIT: Final[int] = 50
# Loud upper bound on re-queries within a single pass — a flip that never
# lands (ownership lost mid-pass) cannot return the same rows forever silently.
REPAIR_DRAIN_MAX_PASSES: Final[int] = 1000

# N3-CONDENSE: how much of the original prose a RE-EMIT head carries. An MCP
# session that stayed busy through the quiet-gap guard (TURN_BOUNDARY_QUIET_S)
# pays the FULL body once per re-emit up to the cap — the overwhelmingly common
# case is a recipient who did read the original and is now re-reading it, where
# the duplicate is cost without news. The head is kept (not elided outright) so
# a genuinely deaf recipient can still recognise WHAT they missed and judge
# urgency without a round-trip, and the marker names the bounded retrieval call.
#
# ⚠ The marker prose must claim only PERSISTENCE, never delivery. A DIRECT row's
# first emission is recorded optimistically at send time (REL-05), so
# ``emit_count == 1`` does NOT prove the bytes ever reached the client — that
# unproven case IS the deaf wake. Telling such a recipient "you already received
# this" would be false exactly when it costs something.
#
# A body small enough that condensing would not actually save bytes is carried
# whole (see :func:`_apply_reemit_marker`) — claiming a truncation that did not
# happen would be a lie in the prose.
REEMIT_BODY_HEAD_CHARS: Final[int] = 240


def _log(msg: str) -> None:
    """Write to stderr; stdout is reserved for MCP JSON-RPC framing."""
    print(f"[solet-bridge] {msg}", file=sys.stderr, flush=True)


class OwedDeliveryTransport(Protocol):
    """The slice of the bridge session the coordinator drives.

    Structurally satisfied by :class:`~.forwarder.Forwarder`; declared as a
    Protocol so this module has no concrete dependency back on the forwarder
    (no import cycle, and the coordinator is unit-testable against a fake).
    """

    @property
    def bridge_ready(self) -> bool:
        """True once a bridge session is open (a ``bridge_id`` exists)."""
        ...

    @property
    def running(self) -> bool:
        """True while the bridge session is active (poll loop not stopped)."""
        ...

    async def emit_event(self, event: dict[str, Any]) -> None:
        """Emit one bridge event to the MCP client as a notification."""
        ...

    async def drain_page(self, limit: int) -> dict[str, Any]:
        """POST ``/peer/drain``; return the full payload.

        ``{"undelivered": [...role rows], "re_emit_cap": N}``.
        """
        ...

    async def flip_delivered(self, *, external_id: str, recipient_key: str) -> None:
        """POST ``/peer/delivered`` to confirm a ROLE row's emission."""
        ...


class OwedDeliveryCoordinator:
    """Owns the single-flight emit ledger, the settle, and the repair-drain loop."""

    def __init__(self, transport: OwedDeliveryTransport) -> None:
        self._transport = transport
        # Single-flight per external_id (Codex BLOCKER-2-deep): a per-id emit
        # TASK, NOT a bare claimed-set. Concurrent settles for the same id share
        # the ONE emit task and only flip after it COMPLETES SUCCESSFULLY — so a
        # duplicate can never flip ``delivered=true`` on the basis of another
        # caller's still-in-flight (or failed) emit. A done-success task stays
        # in the map as the "already emitted" marker (a later duplicate flips
        # again — idempotent M7); a failed task is popped so a retry re-emits.
        self._emits: dict[str, asyncio.Task[None]] = {}
        self._repair_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle (driven by Forwarder.open_bridge / Forwarder.close)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background repair-drain loop."""
        self._repair_task = asyncio.create_task(self._repair_loop())

    async def stop(self) -> None:
        """Cancel and await the repair-drain loop (best-effort)."""
        if self._repair_task is not None:
            self._repair_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._repair_task
            self._repair_task = None

    # ------------------------------------------------------------------
    # Live path — called by the forwarder's /events drain
    # ------------------------------------------------------------------

    @staticmethod
    def role_delivery_keys(event: Mapping[str, Any]) -> tuple[str, str] | None:
        """Return ``(external_id, recipient_key)`` if ``event`` is a role delivery.

        A live ``peer_message`` is a role delivery iff its ``meta`` carries the
        Control #5 keys with a ``role`` recipient_kind. Returns ``None`` for any
        non-role event so the forwarder emits it on the normal path untouched.
        """
        raw_meta = event.get("meta")
        if not isinstance(raw_meta, dict):
            return None
        if raw_meta.get(META_KEY_RECIPIENT_KIND) != RECIPIENT_KIND_ROLE:
            return None
        external_id = raw_meta.get(META_KEY_DELIVERY_EXTERNAL_ID)
        recipient_key = raw_meta.get(META_KEY_RECIPIENT_KEY)
        if not isinstance(external_id, str) or not external_id:
            return None
        if not isinstance(recipient_key, str) or not recipient_key:
            return None
        return external_id, recipient_key

    async def settle_live(
        self,
        *,
        event: dict[str, Any],
        external_id: str,
        recipient_key: str,
    ) -> None:
        """Settle a role delivery that arrived live on ``/events``."""
        await self._settle(
            external_id=external_id, recipient_key=recipient_key, event=event,
        )

    # ------------------------------------------------------------------
    # Repair drain — periodic, sequential, bounded backoff
    # ------------------------------------------------------------------

    async def _repair_loop(self) -> None:
        """Run a drain pass on its own timer until the session stops.

        Sequential by construction (one pass at a time → no overlap). The sleep
        is independent of ``/events`` so live traffic never suppresses it; a
        failed pass backs off geometrically up to a cap, resetting on success.
        """
        backoff = REPAIR_DRAIN_INTERVAL_S
        while self._transport.running:
            if self._transport.bridge_ready:
                try:
                    await self._repair_pass()
                    backoff = REPAIR_DRAIN_INTERVAL_S
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — loop must survive transients
                    backoff = min(backoff * 2, REPAIR_DRAIN_MAX_BACKOFF_S)
                    _log(f"repair drain pass failed (backoff {backoff:.0f}s): {exc}")
            await asyncio.sleep(backoff)

    async def _repair_pass(self) -> None:
        """Drain owed ROLE rows oldest-first, settling each, until empty.

        Confirming each settled row drops it from the server's undelivered
        filter — an empty page ends the pass. The ``MAX_PASSES`` guard
        converts a pathological never-draining loop into a loud bounded log
        instead of a hang.
        """
        for _ in range(REPAIR_DRAIN_MAX_PASSES):
            payload = await self._transport.drain_page(REPAIR_DRAIN_PAGE_LIMIT)
            role_rows = _rows(payload.get("undelivered"))
            if not role_rows:
                return
            cap = payload.get("re_emit_cap")
            cap_int = cap if isinstance(cap, int) else 0
            for row in role_rows:
                await self._deliver_drain_row(row, cap=cap_int)
        _log(
            f"repair drain hit {REPAIR_DRAIN_MAX_PASSES}-pass cap; "
            "rows still pending (will retry next interval)",
        )

    async def _deliver_drain_row(self, row: dict[str, Any], *, cap: int) -> None:
        """Convert one ROLE drain row to an event and settle it (N3-marked)."""
        external_id = str(row.get("external_id") or "")
        recipient_key = str(row.get("recipient_key") or "")
        if not external_id or not recipient_key:
            _log(f"drain row missing external_id/recipient_key, skipping: {row!r}")
            return
        event = _drain_row_to_event(
            row, external_id=external_id, recipient_key=recipient_key,
        )
        _apply_reemit_marker(event, row, message_id=external_id, cap=cap)
        await self._settle(
            external_id=external_id, recipient_key=recipient_key, event=event,
        )

    async def _settle(
        self,
        *,
        external_id: str,
        recipient_key: str,
        event: dict[str, Any],
    ) -> None:
        """Emit the role message at most once, then always confirm delivery.

        SINGLE-FLIGHT (Codex BLOCKER-2-deep): the emit is owned by ONE per-id
        task. The first caller creates + awaits it; a concurrent caller (live
        path vs repair drain) for the same id AWAITS THE SAME task rather than
        re-emitting — and crucially does NOT reach the flip until that shared
        task COMPLETES SUCCESSFULLY. So a duplicate can never mark
        ``delivered=true`` on the basis of an in-flight or failed emit. If the
        shared emit FAILS, every waiter propagates the exception (the flip is
        never reached) and the task is popped so a later retry re-emits — the
        row stays ``delivered=false`` and the repair loop re-delivers. The flip
        (POST ``/peer/delivered``) is then issued by every caller AFTER the
        confirmed-successful emit (idempotent M7 re-confirm).
        """
        await self._emit_once(external_id, event)
        await self._transport.flip_delivered(
            external_id=external_id, recipient_key=recipient_key,
        )

    async def _emit_once(self, external_id: str, event: dict[str, Any]) -> None:
        """Emit ``event`` exactly once across concurrent callers (single-flight).

        Returns only after the SHARED emit task has completed SUCCESSFULLY;
        raises (without flipping) if it failed. A failed/own-cancelled task is
        popped so a retry re-emits; a successful task stays as the marker.
        """
        task = self._emits.get(external_id)
        if task is None:
            task = asyncio.ensure_future(self._transport.emit_event(event))
            self._emits[external_id] = task
        try:
            # ``shield`` isolates THIS awaiter's cancellation from the shared
            # emit task (Codex MINOR-1): cancelling one settle must NOT cancel
            # the emit the other concurrent waiters depend on — it only cancels
            # this await.
            await asyncio.shield(task)
        except BaseException:
            # Roll back ONLY if the shared task itself finished (failed/
            # cancelled) — not if merely THIS awaiter was cancelled while the
            # task is still in flight for the others. Identity-safe: never let a
            # stale waiter evict a NEWER retry task registered under the same id.
            if task.done() and self._emits.get(external_id) is task:
                self._emits.pop(external_id, None)
            raise


def _drain_row_to_event(
    row: dict[str, Any], *, external_id: str, recipient_key: str,
) -> dict[str, Any]:
    """Shape a ``/peer/drain`` row into the event the forwarder emits.

    Mirrors a live role ``peer_message`` event: ``event_type`` ``peer_message``
    (so the forwarder routes it through the same notification machinery), the
    already-marker-stripped prose as ``content`` (the server's
    ``_serialize_role_drain_row`` strips it for delivery parity), the sender
    provenance for the targeted-reply meta, and the Control #5 role keys (so a
    drain-built event is indistinguishable from a live one downstream). No
    ``cursor`` key — a drain row is not part of the ``/events`` stream, so it
    must not advance the long-poll cursor.
    """
    message_id = str(row.get("message_id") or "")
    return {
        "event_type": EVENT_PEER_MESSAGE,
        "content": str(row.get("content") or ""),
        # Match the native wake producer: Claude Code rejects an empty flow_id
        # even though replay rows are not themselves part of /events.
        "flow_id": f"peer-wake-{message_id}",
        "meta": {
            "from_agent_id": row.get("sender_agent_id") or "",
            "from_agent_instance_id": row.get("sender_agent_instance_id") or "",
            "from_session_label": row.get("sender_session_label") or "",
            "thread_id": row.get("thread_id") or "",
            "message_id": message_id,
            "important": bool(row.get("important", False)),
            META_KEY_RECIPIENT_KIND: RECIPIENT_KIND_ROLE,
            META_KEY_RECIPIENT_KEY: recipient_key,
            META_KEY_DELIVERY_EXTERNAL_ID: external_id,
        },
    }


def _rows(value: object) -> list[dict[str, Any]]:
    """Coerce a drain-payload list field to a list of row dicts."""
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _apply_reemit_marker(
    event: dict[str, Any],
    row: dict[str, Any],
    *,
    message_id: str,
    cap: int,
) -> None:
    """Prepend the N3 ``[re-emit n/cap ...]`` marker to a re-emitted event.

    A re-emit is any drain row already emitted at least once (``emit_count >=
    1``): the marker lets a model reader anchor the duplicate to its prior copy
    (original message_id + send timestamp) instead of treating it as news. A
    first drain of an owed row that was never emitted (a role original,
    ``emit_count == 0``) is left unmarked.

    A re-emit whose body exceeds :data:`REEMIT_BODY_HEAD_CHARS` is CONDENSED to
    the marker plus that head (N3-CONDENSE). This is a cost fix ONLY — it does
    not touch the server-side consumption guards, so the row stays owed and
    keeps re-emitting until the holder's forwarder confirms it. Nothing is
    dropped: the full text stays PERSISTED in the thread and retrievable via
    ``solet-bridge inbox``.
    """
    emit_count = row.get("emit_count")
    n = emit_count if isinstance(emit_count, int) else 0
    if n < 1:
        return
    created = str(row.get("created_at") or "")
    body = str(event.get("content") or "")
    prefix = f"[re-emit {n}/{cap} of message_id={message_id} originally sent {created}"
    whole = f"{prefix}]\n\n{body}"
    condensed = (
        f"{prefix} — body truncated here to save context; the FULL text is "
        "persisted in the thread. Read it whole with solet-bridge inbox, "
        f"matching message_id={message_id}.]"
        f"\n\n{body[:REEMIT_BODY_HEAD_CHARS]}…"
    )
    # Condense only when it actually SAVES: the pointer prose is itself a few
    # hundred characters, so on a mid-sized body the "condensed" form can be
    # LONGER than simply re-sending it. Comparing the two rendered forms makes
    # that impossible by construction (and keeps short bodies byte-whole)
    # without a second hand-tuned threshold to drift out of sync with the prose.
    event["content"] = condensed if len(condensed) < len(whole) else whole


__all__ = [
    "EVENT_PEER_MESSAGE",
    "REPAIR_DRAIN_INTERVAL_S",
    "REPAIR_DRAIN_MAX_PASSES",
    "REPAIR_DRAIN_PAGE_LIMIT",
    "REEMIT_BODY_HEAD_CHARS",
    "OwedDeliveryCoordinator",
    "OwedDeliveryTransport",
]
