#!/usr/bin/env python3
"""REL-08 read-side inbox visibility across instance rotation (Fork-1a) — no DB.

The direct-wake re-home cures the WAKE orphan; this cures the sibling INBOX-
visibility class (07-07 §4.7): peer threads are keyed on recipient_agent_instance_id,
so a recipient whose instance rotated on reconnect 'loses sight of pre-reconnect
threads'. The fix is READ-SIDE ONLY — stamp a stable recipient_agent_session_id
at create_thread, then UNION it into the inbox thread resolution — NEVER a re-home
of the durable thread rows (recipient_agent_instance_id stays the find_peer_thread
dedup key, so the write-side collision stays shut).

Architect-mandated red-first cases:
  * T0  STAMP: create_thread persists recipient_agent_session_id on the row.
  * Ti  CHURN VISIBILITY (the live class replayed): a thread created to instance
        A (session S); the recipient restarts as B (same session S). Inbox on B
        lists the pre-reconnect thread via the session disjunct (GREEN). RED
        anchor: without the session key, the successor is blind (today's bug).
  * Tii LEGACY no-regression: a legacy NULL-session thread on the CURRENT instance
        is still listed (the instance disjunct is preserved).
  * Tiii NEGATIVE: a DIFFERENT session id does NOT see the thread (this is also
        the Fork-1a scope boundary — a restart that mints a NEW session id is the
        residual that needs role-addressed delivery, not this read).

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_inbox_session_visibility_smoke.py
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.models import (  # noqa: E402
    MessageKind,
    MessageRole,
    OriginatorType,
    ThreadStatus,
)
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    ID_PREFIX_MESSAGE,
    NAMESPACE,
    TABLE_AGENT_MESSAGE,
    TABLE_AGENT_THREAD,
)

_A = "agi-A"  # the recipient's pre-restart instance
_B = "agi-B"  # its post-reconnect successor (same session)
_C = "agi-C"  # an unrelated instance
_S = "ases-sess-1"  # the stable session id that spans A and B
_OTHER = "ases-sess-2"  # a different session

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


def _repo(state: RealShapeState) -> AgentMessagingRepository:
    return AgentMessagingRepository(cast(StateManagementInterface, state))


def _seed_thread(
    state: RealShapeState,
    *,
    thread_id: str,
    instance: str | None,
    session: str | None,
) -> None:
    state.rows(NAMESPACE, TABLE_AGENT_THREAD).append(
        {
            "id": thread_id,
            "namespace": "core",
            "target_backend": "peer:claude_code",
            "recipient_agent_instance_id": instance,
            "recipient_agent_session_id": session,
            "is_deleted": 0,
        },
    )


def _seed_msg(
    state: RealShapeState, *, thread_id: str, cursor: int, suffix: str,
) -> None:
    state.rows(NAMESPACE, TABLE_AGENT_MESSAGE).append(
        {
            "id": f"{ID_PREFIX_MESSAGE}_{suffix}",
            "namespace": "core",
            "thread_id": thread_id,
            "cursor": cursor,
            "role": MessageRole.ORIGINATOR.value,
            "kind": MessageKind.MESSAGE.value,
            "content": [{"type": "text", "text": f"m{cursor}"}],
            "action_id": None,
            "backend_session_id": None,
            "error": None,
            "artifacts": [],
            "metadata": {},
            "important": False,
            "created_at": "2026-07-11T16:00:00",
            "is_deleted": 0,
        },
    )


def _inbox_cursors(
    repo: AgentMessagingRepository, *, instance: str, session: str,
) -> list[int]:
    rows, _ = repo.list_peer_messages_for(
        recipient_agent_id="claude_code",
        recipient_agent_instance_id=instance,
        recipient_agent_session_id=session,
        after_created_at=None,
        limit=50,
    )
    return [
        m.cursor
        for m in rows
    ]


# ---------------------------------------------------------------------------
# T0 — create_thread stamps the session id
# ---------------------------------------------------------------------------


def test_t0_create_thread_stamps_session() -> None:
    state = RealShapeState()
    repo = _repo(state)
    # The write lands the row; the in-memory fake's get_thread readback lacks the
    # DB-DEFAULT created_at (real Postgres supplies it), so suppress that readback
    # and assert the WRITTEN record carries the stamp.
    with contextlib.suppress(Exception):
        repo.create_thread(
            originator_type=OriginatorType.MCP_BRIDGE,
            target_backend="peer:claude_code",
            target_plugin_name="agent_messaging_plugin",
            status=ThreadStatus.IDLE,
            recipient_agent_instance_id=_A,
            recipient_agent_session_id=_S,
        )
    rows = state.rows(NAMESPACE, TABLE_AGENT_THREAD)
    _check(
        len(rows) == 1
        and rows[0]["recipient_agent_session_id"] == _S
        and rows[0]["recipient_agent_instance_id"] == _A,
        "T0: create_thread WRITES recipient_agent_session_id onto the row (+ keeps the instance key)",
    )


# ---------------------------------------------------------------------------
# Ti — churn visibility (the live 07-07 §4.7 class replayed)
# ---------------------------------------------------------------------------


def test_ti_churn_visibility() -> None:
    state = RealShapeState()
    repo = _repo(state)
    # A thread created while the recipient was instance A (session S), with a msg.
    _seed_thread(state, thread_id="agt-pre", instance=_A, session=_S)
    _seed_msg(state, thread_id="agt-pre", cursor=0, suffix="aa")
    # RED anchor: the successor instance B WITHOUT the session key sees nothing —
    # the thread is instance-keyed to the dead A. This is the pre-fix orphan.
    _check(
        _inbox_cursors(repo, instance=_B, session="") == [],
        "Ti red: successor instance B (no session key) is blind to the pre-reconnect thread",
    )
    # GREEN: B WITH the stable session key sees the pre-reconnect thread's message
    # via the session disjunct of the UNION.
    _check(
        _inbox_cursors(repo, instance=_B, session=_S) == [0],
        "Ti green: successor B (same session) lists the pre-reconnect thread (UNION)",
    )


# ---------------------------------------------------------------------------
# Tii — legacy NULL-session thread on the current instance, no regression
# ---------------------------------------------------------------------------


def test_tii_legacy_no_regression() -> None:
    state = RealShapeState()
    repo = _repo(state)
    # A legacy thread with NO session id, keyed to the caller's CURRENT instance B.
    _seed_thread(state, thread_id="agt-legacy", instance=_B, session=None)
    _seed_msg(state, thread_id="agt-legacy", cursor=0, suffix="bb")
    _check(
        _inbox_cursors(repo, instance=_B, session=_S) == [0],
        "Tii: a legacy NULL-session thread on the current instance is still listed (instance disjunct)",
    )


# ---------------------------------------------------------------------------
# Tiii — negative: a different session does not see the thread
# ---------------------------------------------------------------------------


def test_tiii_negative_different_session() -> None:
    state = RealShapeState()
    repo = _repo(state)
    _seed_thread(state, thread_id="agt-priv", instance=_A, session=_S)
    _seed_msg(state, thread_id="agt-priv", cursor=0, suffix="cc")
    # An unrelated instance C on a DIFFERENT session sees nothing — neither
    # disjunct matches. (This is also the Fork-1a scope boundary: a restart that
    # mints a NEW session id is the residual class, not this read.)
    _check(
        _inbox_cursors(repo, instance=_C, session=_OTHER) == [],
        "Tiii: a DIFFERENT session (+ instance) sees nothing (isolation preserved)",
    )


def main() -> None:
    print("=== REL-08 read-side peer-inbox session-visibility (Fork-1a) smoke ===")
    test_t0_create_thread_stamps_session()
    test_ti_churn_visibility()
    test_tii_legacy_no_regression()
    test_tiii_negative_different_session()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
