#!/usr/bin/env python3
"""v10 Control #5 S5 — client-side ROLE repair drain (single-flight + N3 marker).

A4 (2026-08-04): this smoke used to drive the coordinator's shared repair pass
against BOTH owed kinds (role + direct); the direct half retired with the
direct-wake outbox it drained. Rewritten to the surviving role-only path —
same coordinator logic (:meth:`OwedDeliveryCoordinator._repair_pass`,
``_settle``, the N3 marker/condense helpers), proven with role fixtures
instead of a role+direct mix:

  * **drain** — a ``/peer/drain`` payload carrying role rows settles each via
    ``flip_delivered``.
  * **single-flight** — two concurrent settles for the same role external_id
    emit to the client EXACTLY once.
  * **N3 re-emit marker** — a role row already emitted at least once
    (``emit_count>=1``) carries the ``[re-emit n/cap of message_id=...
    originally sent ...]`` marker; a role ORIGINAL (``emit_count=0``) is left
    unmarked.
  * **N3-CONDENSE** — a re-emit body past ``REEMIT_BODY_HEAD_CHARS`` is
    truncated to its head plus a retrieval pointer (the full bytes were
    already emitted once); a body under the budget is carried whole and
    never claims a truncation that did not happen.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/direct_wake_delivery_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.mcp_bridge.owed_delivery import (  # noqa: E402
    REEMIT_BODY_HEAD_CHARS,
    OwedDeliveryCoordinator,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


class _FakeTransport:
    """Records emits + role flips; serves one drain payload."""

    def __init__(self, *, payload: dict[str, Any] | None = None) -> None:
        self._payloads = [payload] if payload is not None else []
        self.emitted: list[dict[str, Any]] = []
        self.flipped: list[str] = []
        self._running = True
        self._ready = True

    @property
    def bridge_ready(self) -> bool:
        return self._ready

    @property
    def running(self) -> bool:
        return self._running

    async def emit_event(self, event: dict[str, Any]) -> None:
        self.emitted.append(event)

    async def drain_page(self, limit: int) -> dict[str, Any]:
        if self._payloads:
            return self._payloads.pop(0)
        return {"undelivered": [], "re_emit_cap": 3}

    async def flip_delivered(self, *, external_id: str, recipient_key: str) -> None:
        self.flipped.append(external_id)


def _role_row(external_id: str, *, emit_count: int, content: str = "role ping") -> dict[str, Any]:
    return {
        "external_id": external_id,
        "recipient_key": "R",
        "message_id": external_id.split(":")[-1],
        "sender_agent_id": "claude_code",
        "sender_agent_instance_id": "agi-S",
        "sender_session_label": "Sender",
        "thread_id": "role:R",
        "important": True,
        "emit_count": emit_count,
        "created_at": "2026-07-07T12:00:00+00:00",
        "content": content,
    }


def test_drain_settles_role_rows() -> None:
    transport = _FakeTransport(
        payload={
            "undelivered": [_role_row("role:R:agm-r", emit_count=0)],
            "re_emit_cap": 3,
        },
    )
    coord = OwedDeliveryCoordinator(transport)
    asyncio.run(coord._repair_pass())  # noqa: SLF001
    _check(
        transport.flipped == ["role:R:agm-r"],
        "S5: the role row is confirmed via flip_delivered",
    )
    _check(len(transport.emitted) == 1, "S5: the row emits once in the repair pass")


def test_role_single_flight() -> None:
    transport = _FakeTransport()
    coord = OwedDeliveryCoordinator(transport)
    event = {"event_type": "peer_message", "content": "x", "meta": {}}

    async def _run() -> None:
        a = asyncio.ensure_future(
            coord._settle(external_id="role:R:agm-r", recipient_key="R", event=event),  # noqa: SLF001
        )
        b = asyncio.ensure_future(
            coord._settle(external_id="role:R:agm-r", recipient_key="R", event=event),  # noqa: SLF001
        )
        await asyncio.gather(a, b)

    asyncio.run(_run())
    _check(
        len(transport.emitted) == 1,
        "S5: concurrent settles for the same role external_id emit EXACTLY once",
    )
    _check(
        transport.flipped == ["role:R:agm-r", "role:R:agm-r"],
        "S5: both settles confirm (idempotent) after the one shared emit",
    )


def test_n3_reemit_marker() -> None:
    transport = _FakeTransport(
        payload={
            "undelivered": [
                _role_row("role:R:agm-orig", emit_count=0),
                _role_row("role:R:agm-again", emit_count=2),
            ],
            "re_emit_cap": 3,
        },
    )
    coord = OwedDeliveryCoordinator(transport)
    asyncio.run(coord._repair_pass())  # noqa: SLF001
    orig_event = next(
        e for e in transport.emitted if e["meta"].get("message_id") == "agm-orig"
    )
    again_event = next(
        e for e in transport.emitted if e["meta"].get("message_id") == "agm-again"
    )
    _check(
        not orig_event["content"].startswith("[re-emit"),
        "S5/N3: a role ORIGINAL (emit_count=0) drained is NOT marked as a re-emit",
    )
    _check(
        again_event["content"].startswith(
            "[re-emit 2/3 of message_id=role:R:agm-again originally sent ",
        ),
        "S5/N3: a role re-emit (emit_count>=1) carries the [re-emit n/cap ...] marker",
    )
    _check(
        "role ping" in again_event["content"],
        "S5/N3: the original prose is preserved after the marker",
    )


# N3-CONDENSE fixtures, shared by the two cases below. ``_LONG`` is sized like
# the specimen that prompted the fix (a ~1.1k-token operator brief); ``_MID`` is
# over the head budget but small enough that the pointer prose would not shrink
# it, which is the "condense only when it saves" case.
_LONG_BODY = "PARAGRAPH OF A LONG OPERATOR BRIEF. " * 120
_MID_BODY = "JUST OVER THE HEAD BUDGET. " * 12


def _condense_emits() -> dict[str, str]:
    """Drain one long / one mid / one short role re-emit; return contents by id."""
    transport = _FakeTransport(
        payload={
            "undelivered": [
                _role_row("role:R:agm-long", emit_count=1, content=_LONG_BODY),
                _role_row("role:R:agm-mid", emit_count=1, content=_MID_BODY),
                _role_row("role:R:agm-short", emit_count=1),
            ],
            "re_emit_cap": 3,
        },
    )
    asyncio.run(OwedDeliveryCoordinator(transport)._repair_pass())  # noqa: SLF001
    return {
        str(e["meta"].get("message_id")): str(e["content"]) for e in transport.emitted
    }


def _plain_form_len(message_id: str, body: str) -> int:
    """Length of the marker form this re-emit would have had BEFORE N3-CONDENSE."""
    return len(
        f"[re-emit 1/3 of message_id={message_id} originally sent "
        f"2026-07-07T12:00:00+00:00]\n\n{body}",
    )


def test_n3_condense_long_body() -> None:
    """N3-CONDENSE: a LONG re-emit body is cut to its head plus a retrieval pointer.

    The fixture is MEASURED first, per the standing rule that a regression test
    must be shown capable of emitting the signal — the pre-existing ``ping from
    sender`` body is far under the budget, so a condense assertion written
    against it would pass vacuously forever.
    """
    _check(
        len(_LONG_BODY) > REEMIT_BODY_HEAD_CHARS,
        "S5/N3-CONDENSE: fixture body EXCEEDS the head budget (signal is emittable)",
    )
    content = _condense_emits()["agm-long"]
    _check(
        content.startswith(
            "[re-emit 1/3 of message_id=role:R:agm-long originally sent ",
        ),
        "S5/N3-CONDENSE: the condensed form keeps the N3 marker prefix verbatim",
    )
    _check(
        "solet-bridge inbox" in content and "message_id=role:R:agm-long" in content,
        "S5/N3-CONDENSE: the marker names the complete merged drain for the full text",
    )
    _check(
        content.endswith("…") and _LONG_BODY[:REEMIT_BODY_HEAD_CHARS] in content,
        "S5/N3-CONDENSE: the HEAD of the original prose survives (recognisable)",
    )
    _check(
        _LONG_BODY not in content and len(content) < len(_LONG_BODY),
        "S5/N3-CONDENSE: the full body is NOT duplicated and the re-emit is cheaper",
    )


def test_n3_condense_never_worse_than_plain() -> None:
    """N3-CONDENSE: bodies the pointer prose would not shrink stay byte-whole."""
    emits = _condense_emits()
    _check(
        REEMIT_BODY_HEAD_CHARS < len(_MID_BODY) < len(_LONG_BODY),
        "S5/N3-CONDENSE: mid fixture is over the head budget but small (no-saving case)",
    )
    _check(
        _MID_BODY in emits["agm-mid"] and "truncated" not in emits["agm-mid"],
        "S5/N3-CONDENSE: a body the pointer prose would not shrink is carried WHOLE",
    )
    _check(
        emits["agm-short"].endswith("\n\nrole ping")
        and "truncated" not in emits["agm-short"],
        "S5/N3-CONDENSE: a short body is carried WHOLE and claims no truncation",
    )
    _check(
        len(emits["agm-long"]) <= _plain_form_len("role:R:agm-long", _LONG_BODY),
        "S5/N3-CONDENSE: a long re-emit is never LONGER than a plain re-send",
    )
    _check(
        len(emits["agm-mid"]) <= _plain_form_len("role:R:agm-mid", _MID_BODY),
        "S5/N3-CONDENSE: a mid re-emit is never LONGER than a plain re-send",
    )


def main() -> None:
    print("=== v10 Control #5 S5 role-wake client delivery smoke ===")
    test_drain_settles_role_rows()
    test_role_single_flight()
    test_n3_reemit_marker()
    test_n3_condense_long_body()
    test_n3_condense_never_worse_than_plain()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
