#!/usr/bin/env python3
"""Live StateManagementInterface smoke for ``gauge_notice_record``.

This is deliberately bridge-backed rather than a direct Postgres connection:
the production state service owns the configured StateManagementInterface, so
the store calls below exercise the real SQL adapter without raw SQL, psycopg,
or a hand-built provider.  A unique subject is hard-deleted in ``finally``.

The smoke is opt-in because it writes five short-lived probe records to the
running solet.  Run it from a live bridge-bearing agent session:

    GAUGE_NOTICE_RECORD_LIVE_SMOKE=1 \\
      .venv/bin/python3 plugins/agent_messaging_plugin/tests/gauge_notice_record_live_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
)

from agent_messaging_plugin.gauge_notice_record_store import (  # noqa: E402
    record_gauge_notice,
    record_notice_best_effort,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    NOTICE_DELIVERY_APPENDED,
    NOTICE_DELIVERY_NO_STEWARD_BINDING,
)

_PROBE_SUBJECT = "agi-gauge-notice-live-smoke"


def _call(process_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run(
        ["solet-bridge", "call", process_key, json.dumps(payload)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr or completed.stdout)
    outer = json.loads(completed.stdout)
    result = outer.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"malformed bridge result: {outer!r}")
    return result


class _BridgeBackedState:
    """The running solet's StateManagementInterface, reached via its bridge."""

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        return _call(
            "service_interface::state_service::write_state",
            {"namespace": namespace, "data": data},
        )

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        result = _call(
            "service_interface::state_service::read_state",
            {
                "namespace": namespace,
                "query": {
                    "table": data["table"],
                    "filters": data.get("filters", {}),
                    "limit": data["limit"],
                },
            },
        )
        if result.get("action_status") != "completed":
            return result
        records = result["data"]["records"]
        if not isinstance(records, list):
            raise RuntimeError(f"read_state returned non-list records: {result!r}")
        for column, direction in reversed(data["order_by"]):
            records.sort(
                key=lambda row: str(row.get(column, "")),
                reverse=direction == "desc",
            )
        return {
            "action_status": "completed",
            "data": {"records": records[:data["limit"]]},
            "actions": [],
            "error": None,
        }


def _cleanup() -> None:
    result = _call(
        "service_interface::state_service::delete_records",
        {
            "namespace": AGENT_ROLE_BINDING_NAMESPACE,
            "query": {
                "table": "gauge_notice_record",
                "filters": {"agent_instance_id": _PROBE_SUBJECT},
                "soft_delete": False,
            },
        },
    )
    if result.get("action_status") != "completed":
        raise RuntimeError(f"probe cleanup failed: {result!r}")


def main() -> int:
    if os.environ.get("GAUGE_NOTICE_RECORD_LIVE_SMOKE") != "1":
        print("SKIP  set GAUGE_NOTICE_RECORD_LIVE_SMOKE=1; requires a live bridge session")
        return 0

    state = _BridgeBackedState()
    now = datetime.now(UTC)
    cases = (
        ("gauge_stale_notice", NOTICE_DELIVERY_APPENDED),
        ("gauge_stale_notice", NOTICE_DELIVERY_NO_STEWARD_BINDING),
        ("gauge_coverage_notice", NOTICE_DELIVERY_APPENDED),
        ("gauge_coverage_notice", NOTICE_DELIVERY_NO_STEWARD_BINDING),
    )
    try:
        for index, (notice_type, delivery_outcome) in enumerate(cases):
            record_gauge_notice(
                state,  # type: ignore[arg-type]  # bridge adapter implements used interface
                notice_type=notice_type,
                agent_instance_id=_PROBE_SUBJECT,
                emitted_at=(now + timedelta(seconds=index)).isoformat(),
                delivery_outcome=delivery_outcome,
                steward_instance_id=(
                    "agi-gauge-notice-live-steward"
                    if delivery_outcome == NOTICE_DELIVERY_APPENDED
                    else None
                ),
                release_id="gauge-notice-live-smoke",
                threshold_s=3600.0,
                observed_s=5400.0,
                last_report_alive_at=(now - timedelta(seconds=30)).isoformat(),
                gauge_measured_at=(now - timedelta(seconds=5400)).isoformat(),
            )
            print(f"PASS  record_gauge_notice {notice_type}/{delivery_outcome}")

        record_notice_best_effort(
            state,  # type: ignore[arg-type]
            notice_type="gauge_stale_notice",
            agent_instance_id=_PROBE_SUBJECT,
            delivery_outcome=NOTICE_DELIVERY_APPENDED,
            steward_instance_id="agi-gauge-notice-live-steward",
            clock=now + timedelta(seconds=10),
            threshold_s=3600.0,
            observed_s=5400.0,
            last_report_alive_at=now - timedelta(seconds=30),
            gauge_measured_at=now - timedelta(seconds=5400),
        )
        print("PASS  record_notice_best_effort gauge_stale_notice/appended")
    finally:
        _cleanup()
        print("PASS  probe records hard-deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
