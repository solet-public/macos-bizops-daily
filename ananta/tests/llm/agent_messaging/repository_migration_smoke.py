#!/usr/bin/env python3
"""Unit smoke for the agent_messaging Phase-0 SQL-lockdown migration.

Slice 1 = R1: ``AgentMessagingRepository.get_thread`` migrated from the raw
autocommit ``execute_sql`` (``SELECT … WHERE id = %s``) to the state-interface
``query_state`` primitive.

``query_state`` is itself a landed + independently-tested primitive (its own
suite covers the SQL composition + the ``data.records`` shape over postgres/rds).
So the ONLY new wiring this slice introduces is, at the repository boundary:
  (a) the EXACT ``query_state`` call args, and
  (b) marshalling its ``data.records[0]`` dict through the (UNCHANGED)
      ``_row_to_thread``.
A spy state captures both — the established faithful-fake pattern from
``role_inbox_smoke.py`` (no DB; query_state's own suite owns the SQL).

Run:
    .venv/bin/python3 ananta/tests/llm/agent_messaging/repository_migration_smoke.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.models import (  # noqa: E402
    ID_PREFIX_MESSAGE,
    MessageKind,
    MessageRole,
    OriginatorType,
    ThreadStatus,
)
from ananta.llm.agent_messaging.repository import (  # noqa: E402
    AgentMessagingRepository,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    apply_ordered_query_in_memory,
    parse_ordered_query,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# A faithful ``query_state`` record: a ``SELECT *`` dict with ISO-string
# datetimes (the read layer's ``_serialize_for_json`` output) and JSONB
# ``metadata`` as a dict — exactly the shape the live primitive returns.
_THREAD_RECORD: dict[str, Any] = {
    "id": "agt-probe1",
    "namespace": "core",
    "originator_type": OriginatorType.MCP_BRIDGE.value,
    "originator_id": "orig-1",
    "originator_session_id": "sess-1",
    "originator_bridge_id": "agc-1",
    "target_backend": "peer:claude_code",
    "target_plugin_name": "agent_messaging_plugin",
    "title": "probe thread",
    "working_directory": None,
    "status": ThreadStatus.OPEN.value,
    "backend_session_id": None,
    "active_action_id": None,
    "active_flow_id": None,
    "last_message_cursor": 3,
    "metadata": {"k": "v"},
    "recipient_agent_instance_id": "agi-r",
    "originator_session_label": "Claude-A",
    "originator_agent_instance_id": "agi-o",
    "recipient_session_label": "Claude-B",
    "created_at": "2026-06-20T12:00:00",
    "updated_at": "2026-06-20T12:05:00",
    "closed_at": None,
    "is_deleted": 0,
}


def _msg(
    thread_id: str,
    cursor: int,
    suffix: str,
    *,
    created_at: str = "2026-06-20T12:00:00",
    important: bool = False,
    role: str = MessageRole.ORIGINATOR.value,
    is_deleted: int = 0,
) -> dict[str, Any]:
    """A faithful query_ordered message record. The id uses the REAL message
    prefix so the hex-aware sentinel comparison (`{prefix}_g` > any hex suffix)
    is meaningful; ``suffix`` must be lowercase hex (< 'g')."""
    return {
        "id": f"{ID_PREFIX_MESSAGE}_{suffix}",
        "namespace": "core",
        "thread_id": thread_id,
        "cursor": cursor,
        "role": role,
        "kind": MessageKind.MESSAGE.value,
        "content": [{"type": "text", "text": f"m{cursor}"}],
        "action_id": None,
        "backend_session_id": None,
        "error": None,
        "artifacts": [],
        "metadata": {},
        "important": important,
        "created_at": created_at,
        "is_deleted": is_deleted,
    }


def _peer_thread(thread_id: str, recipient: str) -> dict[str, Any]:
    """A minimal core__agent_thread record for the R3a 2-eq resolve. Only the
    fields R3a filters on + ``id`` matter (R3a extracts ``id`` only, no marshal)."""
    return {
        "id": thread_id,
        "namespace": "core",
        "target_backend": "peer:claude_code",
        "recipient_agent_instance_id": recipient,
        "is_deleted": 0,
    }


class _SpyState:
    """Records the exact ``query_state`` call + returns id-matched records.

    Implements only the verbs ``get_thread`` exercises; passed where a
    ``StateManagementInterface`` is expected (duck-typed, per role_inbox_smoke).
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ordered_calls: list[tuple[str, dict[str, Any]]] = []

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        self.calls.append((namespace, cast("dict[str, Any]", filters)))
        inner = cast("dict[str, Any]", filters.get("filters", {}))
        # Single-namespace equality semantics: a record matches when every
        # filter column equals the record's value (covers R1's {id} and R5's
        # {thread_id}). UNCAPPED — recent_messages relies on this.
        matched = [
            r for r in self._rows
            if all(r.get(col) == val for col, val in inner.items())
        ]
        return _envelope(matched)

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        self.ordered_calls.append((namespace, cast("dict[str, Any]", data)))
        # Reuse the REAL ordered-query semantics (filter + composite order +
        # tie-safe `after` cursor + limit) so the strict-cursor boundary and
        # the hex-aware id sentinel behave exactly as they will over
        # postgres/rds (the role_inbox_smoke precedent).
        spec = parse_ordered_query(data)
        rows = apply_ordered_query_in_memory(
            cast("list[dict[str, object]]", self._rows), spec
        )
        return _envelope(cast("list[dict[str, Any]]", rows))


def _envelope(records: list[dict[str, Any]]) -> ActionResult:
    return cast(
        "ActionResult",
        {
            "action_status": "completed",
            "data": {"records": records, "count": len(records)},
            "error": None,
        },
    )


def main() -> int:
    spy = _SpyState([_THREAD_RECORD])
    repo = AgentMessagingRepository(cast("StateManagementInterface", spy))

    thread = repo.get_thread("agt-probe1")
    call = spy.calls[-1] if spy.calls else None
    _check(
        call == ("core", {"table": "agent_thread", "filters": {"id": "agt-probe1"}}),
        f"get_thread issues query_state('core', {{table:agent_thread, "
        f"filters:{{id}}}}) — single-namespace equality, no raw SQL (call={call!r})",
    )
    _check(
        thread is not None
        and thread.id == "agt-probe1"
        and thread.status is ThreadStatus.OPEN
        and thread.originator_type is OriginatorType.MCP_BRIDGE
        and thread.last_message_cursor == 3
        and thread.metadata == {"k": "v"}
        and thread.target_backend == "peer:claude_code"
        and thread.recipient_session_label == "Claude-B"
        and thread.originator_bridge_id == "agc-1"
        and thread.created_at.isoformat() == "2026-06-20T12:00:00"
        and thread.closed_at is None,
        f"get_thread marshals the query_state record -> correct AgentThreadRow "
        f"(unchanged _row_to_thread; ISO datetimes + dict metadata) (thread={thread!r})",
    )

    missing = repo.get_thread("agt-does-not-exist")
    _check(
        missing is None,
        "get_thread(absent id) -> None (empty records, no row)",
    )

    # --- R2: find_peer_thread -> query_ordered -------------------------------
    peer = repo.find_peer_thread(
        originator_bridge_id="agc-1",
        peer_agent_id="claude_code",
        peer_agent_instance_id="agi-r",
    )
    ocall = spy.ordered_calls[-1] if spy.ordered_calls else None
    expected = (
        "core",
        {
            "table": "agent_thread",
            "filters": {
                "originator_bridge_id": "agc-1",
                "target_backend": "peer:claude_code",
                "recipient_agent_instance_id": "agi-r",
            },
            "order_by": [["created_at", "desc"], ["id", "desc"]],
            "limit": 1,
        },
    )
    _check(
        ocall == expected,
        f"find_peer_thread issues query_ordered(filters{{3 eq}}, "
        f"order_by[[created_at,desc],[id,desc]], limit 1) — the (id,desc) "
        f"tie-break is FORCED by the >=2-col contract + STRENGTHENS the raw "
        f"bare `created_at DESC` (no behavior change) (ocall={ocall!r})",
    )
    _check(
        peer is not None
        and peer.id == "agt-probe1"
        and peer.target_backend == "peer:claude_code"
        and peer.recipient_agent_instance_id == "agi-r",
        f"find_peer_thread marshals the query_ordered record -> AgentThreadRow "
        f"(peer={peer!r})",
    )
    none_peer = repo.find_peer_thread(
        originator_bridge_id="nope",
        peer_agent_id="x",
        peer_agent_instance_id="y",
    )
    _check(none_peer is None, "find_peer_thread(no match) -> None")

    # --- R4: list_messages -> query_ordered (cursor pagination + strict bound)
    msg_spy = _SpyState([_msg("agt-t", 5, "a" * 8), _msg("agt-t", 6, "b" * 8)])
    msg_repo = AgentMessagingRepository(cast("StateManagementInterface", msg_spy))

    page = msg_repo.list_messages("agt-t", after_cursor=5, limit=10)
    mcall = msg_spy.ordered_calls[-1] if msg_spy.ordered_calls else None
    expected_m = (
        "core",
        {
            "table": "agent_message",
            "filters": {"thread_id": "agt-t"},
            "order_by": [["cursor", "asc"], ["id", "asc"]],
            "after": [5, f"{ID_PREFIX_MESSAGE}_g"],
            "limit": 10,
        },
    )
    _check(
        mcall == expected_m,
        f"list_messages issues query_ordered(filters{{thread_id}}, "
        f"order_by[[cursor,asc],[id,asc]], after=[after_cursor, '<prefix>_g'], "
        f"limit) (mcall={mcall!r})",
    )
    _check(
        [m.cursor for m in page] == [6],
        f"list_messages(after_cursor=5) -> STRICT boundary: ONLY cursor 6 (the "
        f"cursor==5 row EXCLUDED via the hex-aware sentinel; no off-by-one) "
        f"(cursors={[m.cursor for m in page]!r})",
    )
    full = msg_repo.list_messages("agt-t", after_cursor=0, limit=10)
    _check(
        [m.cursor for m in full] == [5, 6]
        and full[0].thread_id == "agt-t"
        and full[0].role is MessageRole.ORIGINATOR,
        f"list_messages(after_cursor=0) -> both messages, cursor ASC, "
        f"marshalled to AgentMessageRow (cursors={[m.cursor for m in full]!r})",
    )

    # R4 MULTI-DIGIT boundary (the bug the 5->6 case missed): cursors 9/10/11.
    md_spy = _SpyState(
        [_msg("agt-t", 9, "9" * 8), _msg("agt-t", 10, "a" * 8), _msg("agt-t", 11, "b" * 8)]
    )
    md_repo = AgentMessagingRepository(cast("StateManagementInterface", md_spy))
    md_page = md_repo.list_messages("agt-t", after_cursor=9, limit=10)
    _check(
        [m.cursor for m in md_page] == [10, 11],
        f"list_messages(after_cursor=9) -> [10,11] across the 9→10 DIGIT boundary "
        f"(pre-fix lexical comparator gave []) (cursors={[m.cursor for m in md_page]!r})",
    )
    md_full = md_repo.list_messages("agt-t", after_cursor=0, limit=10)
    _check(
        [m.cursor for m in md_full] == [9, 10, 11],
        f"list_messages numeric ASC = [9,10,11], not lexical [10,11,9] "
        f"(cursors={[m.cursor for m in md_full]!r})",
    )

    # --- R5: recent_messages -> query_state (UNCAPPED) + Python numeric DESC -
    # Gap-C sidestep: a caller requests limit=128 > the query_ordered 100-cap,
    # so query_state(uncapped) + in-Python sort. Rows are SCRAMBLED so the
    # numeric DESC sort (not insertion order, not lexical) is what's asserted.
    r5_spy = _SpyState(
        [
            _msg("agt-r5", 10, "a" * 8),
            _msg("agt-r5", 5, "5" * 8),
            _msg("agt-r5", 11, "b" * 8),
            _msg("agt-r5", 9, "9" * 8),
            _msg("agt-r5", 6, "6" * 8),
        ]
    )
    r5_repo = AgentMessagingRepository(cast("StateManagementInterface", r5_spy))

    recent = r5_repo.recent_messages("agt-r5", limit=128)
    r5_call = r5_spy.calls[-1] if r5_spy.calls else None
    _check(
        r5_call
        == ("core", {"table": "agent_message", "filters": {"thread_id": "agt-r5"}}),
        f"recent_messages issues query_state('core', {{table:agent_message, "
        f"filters:{{thread_id}}}}) — UNCAPPED (a caller needs limit=128 > the "
        f"query_ordered 100-cap) (r5_call={r5_call!r})",
    )
    _check(
        not r5_spy.ordered_calls,
        "recent_messages does NOT use query_ordered (Gap-C sidestep avoids the "
        "100-cap silent truncation)",
    )
    _check(
        [m.cursor for m in recent] == [11, 10, 9, 6, 5],
        f"recent_messages -> NEWEST-first, NUMERIC cursor DESC across the 9→10 "
        f"digit boundary (not lexical, not insertion order) "
        f"(cursors={[m.cursor for m in recent]!r})",
    )
    capped = r5_repo.recent_messages("agt-r5", limit=2)
    _check(
        [m.cursor for m in capped] == [11, 10],
        f"recent_messages(limit=2) -> head(2) of the DESC order = [11,10] "
        f"(cursors={[m.cursor for m in capped]!r})",
    )
    zero = r5_repo.recent_messages("agt-r5", limit=0)
    _check(
        zero == [],
        "recent_messages(limit=0) -> [] (matches raw `LIMIT max(0,limit)`)",
    )

    # --- R3a+R3b: list_peer_messages_for -> query_state(2-eq) + per-thread
    #     query_ordered + Python k-way merge (Option A, NOT thread_id =ANY) ---
    # Two peer threads to this instance (agt-1, agt-2) + a decoy thread to a
    # DIFFERENT instance (agt-3) that R3a must exclude. Messages span both
    # threads with mixed important/role/created_at so the merge, the silent
    # (important=False) filter, the role filter, and the strict created_at
    # boundary are all exercised.
    peer_rows: list[dict[str, Any]] = [
        _peer_thread("agt-1", "agi-r"),
        _peer_thread("agt-2", "agi-r"),
        _peer_thread("agt-3", "agi-other"),
        _msg("agt-1", 0, "0" * 8, created_at="2026-06-20T12:00:00"),
        _msg("agt-1", 1, "1" * 8, created_at="2026-06-20T12:00:01"),
        _msg("agt-2", 2, "2" * 8, created_at="2026-06-20T12:00:02"),
        _msg("agt-1", 3, "3" * 8, created_at="2026-06-20T12:00:03", important=True),
        _msg("agt-2", 4, "4" * 8, created_at="2026-06-20T12:00:04"),
        _msg("agt-1", 5, "5" * 8, created_at="2026-06-20T12:00:05"),
        _msg(
            "agt-1", 6, "6" * 8,
            created_at="2026-06-20T12:00:06", role=MessageRole.AGENT.value,
        ),
        _msg("agt-3", 7, "7" * 8, created_at="2026-06-20T12:00:07"),
        # Soft-deleted originator message — INVARIANT TRIPWIRE: core__agent_message
        # is append-only today (no soft-delete write path), so query_ordered's
        # implicit is_deleted=0 exclusion is a no-op. If a future path soft-deletes
        # a message, this row would be silently dropped from the peer-inbox; the
        # assertion below pins that so the drop can't go unnoticed.
        _msg("agt-1", 8, "8" * 8, created_at="2026-06-20T12:00:08", is_deleted=1),
    ]
    p_spy = _SpyState(peer_rows)
    p_repo = AgentMessagingRepository(cast("StateManagementInterface", p_spy))

    silent, _ = p_repo.list_peer_messages_for(
        recipient_agent_id="claude_code",
        recipient_agent_instance_id="agi-r",
        after_created_at=None,
        limit=50,
    )
    r3a_call = p_spy.calls[0] if p_spy.calls else None
    _check(
        r3a_call
        == (
            "core",
            {
                "table": "agent_thread",
                "filters": {
                    "target_backend": "peer:claude_code",
                    "recipient_agent_instance_id": "agi-r",
                },
            },
        ),
        f"R3a: list_peer_messages_for issues query_state(2-eq "
        f"{{target_backend, recipient_agent_instance_id}}) — no JOIN, no raw SQL "
        f"(r3a_call={r3a_call!r})",
    )
    queried_threads = sorted(
        cast("dict[str, Any]", d["filters"])["thread_id"]
        for _, d in p_spy.ordered_calls
    )
    _check(
        queried_threads == ["agt-1", "agt-2"],
        f"R3a: resolved threads = the 2 agi-r peer threads (agt-3/agi-other "
        f"EXCLUDED); one query_ordered per thread (queried={queried_threads!r})",
    )
    sample = next(
        (
            d for _, d in p_spy.ordered_calls
            if cast("dict[str, Any]", d["filters"])["thread_id"] == "agt-1"
        ),
        None,
    )
    _check(
        sample is not None
        and sample["filters"]
        == {"thread_id": "agt-1", "role": "originator", "important": False}
        and sample["order_by"] == [["created_at", "asc"], ["id", "asc"]]
        and "after" not in sample
        and sample["limit"] == 51
        and sample["unbounded"] is True,
        f"R3b: per-thread query_ordered uses SCALAR filters "
        f"{{thread_id, role:'originator', important:False}} (NOT =ANY), composite "
        f"order, explicit capped+1 lookahead (sample={sample!r})",
    )
    _check(
        [m.cursor for m in silent] == [0, 1, 2, 4, 5],
        f"R3b silent_only: important=True (cursor 3) EXCLUDED, role!=originator "
        f"(cursor 6) EXCLUDED, MERGED across agt-1/agt-2 by created_at asc "
        f"(cursors={[m.cursor for m in silent]!r})",
    )

    full, _ = p_repo.list_peer_messages_for(
        recipient_agent_id="claude_code",
        recipient_agent_instance_id="agi-r",
        after_created_at=None,
        limit=50,
        silent_only=False,
    )
    _check(
        [m.cursor for m in full] == [0, 1, 2, 3, 4, 5],
        f"R3b silent_only=False (audit): IMPORTANT cursor 3 INCLUDED; role "
        f"filter still excludes cursor 6 (cursors={[m.cursor for m in full]!r})",
    )
    _check(
        8 not in [m.cursor for m in silent] and 8 not in [m.cursor for m in full],
        f"R3b TRIPWIRE: soft-deleted (is_deleted=1) originator cursor 8 EXCLUDED "
        f"from BOTH silent + audit views (query_ordered is_deleted=0 default; raw "
        f"SQL lacked it — no-op while append-only). A future message-soft-delete "
        f"path that should surface in peer-inbox fails here + forces the decision "
        f"(silent={[m.cursor for m in silent]!r}, audit={[m.cursor for m in full]!r})",
    )

    after_page, _ = p_repo.list_peer_messages_for(
        recipient_agent_id="claude_code",
        recipient_agent_instance_id="agi-r",
        after_created_at=datetime.fromisoformat("2026-06-20T12:00:02"),
        limit=50,
    )
    _check(
        [m.cursor for m in after_page] == [4, 5],
        f"R3b after_created_at=...02: STRICT boundary excludes created_at<=02 "
        f"(cursors 0,1,2) via the hex-aware sentinel "
        f"(cursors={[m.cursor for m in after_page]!r})",
    )

    capped_page, _ = p_repo.list_peer_messages_for(
        recipient_agent_id="claude_code",
        recipient_agent_instance_id="agi-r",
        after_created_at=None,
        limit=2,
    )
    _check(
        [m.cursor for m in capped_page] == [0, 1],
        f"R3b limit=2: global top-2 by created_at = [0,1] (BOTH from agt-1) — "
        f"proves per-thread limit=capped + merge (a per-thread limit=1 would "
        f"WRONGLY drop cursor 1) (cursors={[m.cursor for m in capped_page]!r})",
    )

    empty, empty_exhausted = p_repo.list_peer_messages_for(
        recipient_agent_id="claude_code",
        recipient_agent_instance_id="agi-nobody",
        after_created_at=None,
        limit=50,
    )
    _check(
        empty == [] and empty_exhausted is True,
        "R3b no matching peer threads -> [] (R3a empty -> short-circuit, no "
        "message query)",
    )

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
