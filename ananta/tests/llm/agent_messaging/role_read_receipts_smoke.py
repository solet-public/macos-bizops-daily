#!/usr/bin/env python3
"""Actual service fault invariants for immutable role-read receipts."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
fixture = runpy.run_path(
    str(ROOT / "ananta" / "tests" / "llm" / "agent_messaging" / "role_inbox_smoke.py"),
    run_name="role_read_receipts_fixture",
)

from ananta.llm.agent_messaging.models import PeerInboxRequest  # noqa: E402
from ananta.llm.agent_messaging.role_cursor import (  # noqa: E402
    RoleCursorScope,
    encode_role_cursor,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE,
    TABLE_ROLE_READ_PAGE_ITEM,
    TABLE_ROLE_READ_RECEIPT,
    TABLE_ROLE_READ_WATERMARK,
)
from ananta.llm.agent_messaging.service import AgentRequestInvalidError  # noqa: E402

FakeState = fixture["_FakeState"]
make_service = fixture["_make_service"]
seed_binding = fixture["_seed_binding"]
seed_role_msg = fixture["_seed_role_msg"]
INSTANCE = fixture["_INSTANCE"]


class FaultState(FakeState):
    fault_item_write = False
    fault_item_read = False
    fault_summary_write = False

    def upsert_state(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        if self.fault_item_write and data.get("table") == TABLE_ROLE_READ_PAGE_ITEM:
            raise RuntimeError("simulated page item write fault")
        if self.fault_summary_write and data.get("table") == TABLE_ROLE_READ_WATERMARK:
            raise RuntimeError("simulated summary write fault")
        return super().upsert_state(namespace, data)

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        if self.fault_item_read and data.get("table") == TABLE_ROLE_READ_PAGE_ITEM:
            raise RuntimeError("simulated page item read fault")
        return super().query_ordered(namespace, data)


def _request(role_after: str | None = None) -> PeerInboxRequest:
    return PeerInboxRequest(
        "codex",
        INSTANCE,
        "sess-agi-holder",
        limit=10,
        role_after=role_after,
    )


def _seed(state: Any, count: int = 2) -> None:
    seed_binding(state, "R1")
    for index in range(count):
        seed_role_msg(state, row_id=f"receipt-{index}", role="R1", created_at=f"2026-09-12T00:00:0{index}+00:00")


def _rows(state: Any, table: str) -> list[dict[str, object]]:
    return state._table(NAMESPACE, table)


def _ack(service: Any, token: str | None) -> None:
    service.acknowledge_role_read_page(
        agent_session_id="sess-agi-holder",
        agent_instance_id=INSTANCE,
        token=token or "",
    )


def test_partial_issue_and_ack_faults_leave_no_receipts() -> None:
    issue_state = FaultState()
    _seed(issue_state)
    issue_state.fault_item_write = True
    issue = make_service(issue_state, include_direct_inbox=True).peer_inbox(_request())
    assert len(issue.role_entries) == 2
    assert issue.role_read_page_token is None
    assert issue.role_read_page_status == "error"
    assert not _rows(issue_state, TABLE_ROLE_READ_RECEIPT)

    ack_state = FaultState()
    _seed(ack_state)
    service = make_service(ack_state, include_direct_inbox=True)
    page = service.peer_inbox(_request())
    ack_state.fault_item_read = True
    try:
        _ack(service, page.role_read_page_token)
    except RuntimeError:
        pass
    else:
        raise AssertionError("partial item query ACK succeeded")
    assert not _rows(ack_state, TABLE_ROLE_READ_RECEIPT)


def test_summary_failure_keeps_exact_receipt_and_retry_is_immutable() -> None:
    state = FaultState()
    _seed(state, 3)
    service = make_service(state, include_direct_inbox=True)
    page = service.peer_inbox(_request())
    state.fault_summary_write = True
    try:
        _ack(service, page.role_read_page_token)
    except RuntimeError:
        pass
    else:
        raise AssertionError("summary write failure was hidden")

    first_receipts = [dict(row) for row in _rows(state, TABLE_ROLE_READ_RECEIPT)]
    assert len(first_receipts) == 1
    state.fault_summary_write = False
    _ack(service, page.role_read_page_token)
    retried_receipts = [dict(row) for row in _rows(state, TABLE_ROLE_READ_RECEIPT)]
    assert len(retried_receipts) == 3
    assert first_receipts[0] in retried_receipts
    _ack(service, page.role_read_page_token)
    assert _rows(state, TABLE_ROLE_READ_RECEIPT) == retried_receipts


def test_late_lower_key_and_unsigned_cursor_do_not_suppress_work() -> None:
    state = FaultState()
    _seed(state, 3)
    service = make_service(state, include_direct_inbox=True)
    page = service.peer_inbox(_request())
    _ack(service, page.role_read_page_token)
    seed_role_msg(
        state,
        row_id="late-lower",
        role="R1",
        created_at="2026-09-11T23:00:00+00:00",
    )
    late = service.peer_inbox(_request())
    assert [entry.message.id for entry in late.role_entries] == ["msg-late-lower"]

    state = FakeState()
    _seed(state, 3)
    service = make_service(state, include_direct_inbox=True)
    unsigned_cursor = encode_role_cursor(
        RoleCursorScope(
            include_important=True,
            held_roles=("R1",),
            agent_instance_id=INSTANCE,
        ),
        created_at_iso="2026-09-12T00:00:01+00:00",
        row_id="receipt-1",
    )
    page = service.peer_inbox(_request(unsigned_cursor))
    assert [entry.message.id for entry in page.role_entries] == ["msg-receipt-0"]
    _ack(service, page.role_read_page_token)
    assert [row["role_row_id"] for row in _rows(state, TABLE_ROLE_READ_RECEIPT)] == [
        "receipt-0"
    ]
    assert {
        entry.message.id for entry in service.peer_inbox(_request()).role_entries
    } == {"msg-receipt-1", "msg-receipt-2"}


def test_watermark_cas_and_forged_page_token_do_not_regress_or_write() -> None:
    state = FakeState()
    _seed(state, 1)
    service = make_service(state, include_direct_inbox=True)
    service._advance_role_read_watermark(  # noqa: SLF001
        recipient_key="R1",
        agent_instance_id=INSTANCE,
        created_at="2026-09-12T00:00:02+00:00",
        row_id="high",
        message_id="m-high",
    )
    service._advance_role_read_watermark(  # noqa: SLF001
        recipient_key="R1",
        agent_instance_id=INSTANCE,
        created_at="2026-09-12T00:00:01+00:00",
        row_id="low",
        message_id="m-low",
    )
    assert _rows(state, TABLE_ROLE_READ_WATERMARK)[0]["read_id"] == "high"
    try:
        _ack(service, "forged")
    except AgentRequestInvalidError:
        pass
    else:
        raise AssertionError("forged token accepted")
    assert not _rows(state, TABLE_ROLE_READ_RECEIPT)


def main() -> int:
    test_partial_issue_and_ack_faults_leave_no_receipts()
    test_summary_failure_keeps_exact_receipt_and_retry_is_immutable()
    test_late_lower_key_and_unsigned_cursor_do_not_suppress_work()
    test_watermark_cas_and_forged_page_token_do_not_regress_or_write()
    print("role_read_receipts_smoke: actual service fault invariants pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
