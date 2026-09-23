#!/usr/bin/env python3
"""Smoke the fail-closed ownership guard for legacy-session staging."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PROJECT_SOLET_CLI", "smoke-fixture-unused-project-solet-cli")

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import legacy_session_backfill as backfill  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_PARKED,
    TABLE_MANAGED_SESSION,
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


def _completed(payload: object, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    import json

    return subprocess.CompletedProcess(
        ["psolet"],
        returncode,
        stdout=json.dumps(payload),
        stderr="command failed",
    )


def _ownership_reader() -> None:
    print("A. ownership reader is typed and fail-closed")
    complete = {
        "effective_limit": 500,
        "returned_count": 2,
        "total_count": 2,
        "truncated": False,
        "rows": [
            {"actor": "agi-owned", "state": "working"},
            {"actor": None, "state": "dispatched"},
        ],
    }
    actors = backfill._active_work_actors(lambda *_args, **_kwargs: _completed(complete))
    _check(actors == {"agi-owned"}, "complete active rows return their typed owner ids")
    incomplete = {**complete, "total_count": 501, "truncated": True}
    try:
        backfill._active_work_actors(lambda *_args, **_kwargs: _completed(incomplete))
    except backfill.OwnershipReadError:
        _check(True, "a truncated page refuses staging rather than reading no owner")
    else:
        _check(False, "a truncated page refuses staging rather than reading no owner")
    try:
        backfill._active_work_actors(lambda *_args, **_kwargs: _completed("not-json"))
    except backfill.OwnershipReadError:
        _check(True, "a malformed ownership payload refuses staging")
    else:
        _check(False, "a malformed ownership payload refuses staging")


def _active_assignment_refusal() -> None:
    print("B. active assignment overrides host-dead eligibility")
    state = RealShapeState()
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "conflict_columns": ["agent_instance_id"],
            "record": {
                "id": "mgs-owned",
                "created_at": "2026-09-16T00:00:00+00:00",
                "agent_instance_id": "agi-owned",
                "lifecycle_state": LIFECYCLE_LIVE,
            },
        },
    )
    original_actors = backfill._active_work_actors
    original_status = backfill.session_status
    try:
        backfill._active_work_actors = lambda: {"agi-owned"}
        backfill.session_status = lambda *_args: {"host_liveness": "dead"}
        result = backfill.stage_legacy_unsupervised_sessions(
            cast(Any, state),
            directed_by="smoke",
        )
    finally:
        backfill._active_work_actors = original_actors
        backfill.session_status = original_status
    _check(result["staged"] == [], "owned dead host is never parked")
    _check(
        result["skipped"]
        == [
            {
                "agent_instance_id": "agi-owned",
                "cursor": {"created_at": "2026-09-16T00:00:00+00:00", "id": "mgs-owned"},
                "reason": "active_unit_assignment",
            },
        ],
        "audit records active_unit_assignment rather than coverage classification",
    )
    rows = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION)
    _check(rows[0]["lifecycle_state"] != LIFECYCLE_PARKED, "lifecycle state remains live")


def main() -> int:
    _ownership_reader()
    _active_assignment_refusal()
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
