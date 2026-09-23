#!/usr/bin/env python3
"""Smoke for the R2 managed-spawn session-id guard in `mcp_bridge.__main__`.

Covers `_enforce_session_id_for_managed_registration` — the fail-loud check
added at the ACTUAL WRITE SITE for the R2 `holds=false` defect, pinned by
direct inspection on 2026-08-18: a spawned worker's LEDGER-id `peer_binding`
row carried an empty `agent_session_id` while its WATCH-id row (registered
through the separate `local_cli` watch path) was correctly populated.

Root cause, confirmed by static trace + live inspection (NOT theorized):
`macos_coding_agent_session_plugin.bridge_tracker.default_spawn` injects a
caller-chosen `AGENT_INSTANCE_ID` into the spawned MCP bridge subprocess's env
(`env = os.environ.copy(); env[ENV_AGENT_INSTANCE_ID] = agent_instance_id`) —
but forwards no `AGENT_SESSION_ID` alongside it, so
`_resolve_agent_session_id` in that subprocess reads the long-running solet
SERVER's own environment (never the calling session's) and deterministically
resolves `""`. Registering that silently writes a `peer_binding` row that
`peer_holds_role` / `peer_claim_role` (`agent_session_id_for_instance`) can
never confirm for that instance id — a check whose success path never runs is
not a check.

RED-FIRST (this file, run against `_run()`'s CURRENT and prior shape):
before this change, nothing in the write path refused to persist that empty
value for a managed (injected-instance-id) registration — the row was written
silently. The guard below intercepts exactly that moment. Both discriminators
matter and are pinned here:
  - the MANAGED case (an injected AGENT_INSTANCE_ID) with an empty session id
    now dies loud instead of registering degraded;
  - the INTERACTIVE/un-managed case (self-minted instance id, no injection)
    is UNCHANGED and MUST still be permitted empty — that degrade is
    documented, intentional (mcp_bridge/__main__.py:545-567,
    session_id_carrier_chain_smoke.py) and out of scope for this fix
    (refinement-3/4 boundary, lane-r2-holds-false ruling 2026-08-18).

Run:

    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/managed_bridge_session_id_guard_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from agent_messaging_plugin.mcp_bridge.__main__ import (  # noqa: E402
    _enforce_session_id_for_managed_registration,
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


def _raises_runtime_error(*, injected: bool, session_id: str) -> bool:
    try:
        _enforce_session_id_for_managed_registration(
            agent_instance_id_injected=injected,
            agent_session_id=session_id,
        )
    except RuntimeError:
        return True
    return False


# ─── the defect this guard closes (R2) ───────────────────────────────────


def test_managed_empty_session_id_dies_loud() -> None:
    # THE PINNED DEFECT: an injected (managed) instance id with an empty
    # session id — exactly the shape `bridge_tracker.default_spawn` produces
    # today for any caller that supplies AGENT_INSTANCE_ID without also
    # forwarding AGENT_SESSION_ID. Pre-fix this silently registered; post-fix
    # it must refuse before that write happens.
    _check(
        _raises_runtime_error(injected=True, session_id=""),
        "managed (injected instance id) + empty session id -> RuntimeError "
        "(R2 defect closed at the write site)",
    )


def test_managed_populated_session_id_passes() -> None:
    _check(
        not _raises_runtime_error(injected=True, session_id="ases-agi-real"),
        "managed (injected instance id) + populated session id -> no raise "
        "(the fixed spawner path)",
    )


# ─── the boundary this guard must NOT cross (refinement 3/4) ────────────


def test_interactive_empty_session_id_still_permitted() -> None:
    # The documented, intentional degrade for a genuinely un-managed bridge
    # (operator .mcp.json path, self-minted instance id) MUST survive
    # unchanged — this is the non-holder-shaped discriminator for THIS guard:
    # a call that looks superficially similar (empty session id) but is NOT
    # the managed shape must not be refused.
    _check(
        not _raises_runtime_error(injected=False, session_id=""),
        "interactive (self-minted instance id) + empty session id -> no "
        "raise (documented degrade, untouched)",
    )


def test_interactive_populated_session_id_passes() -> None:
    _check(
        not _raises_runtime_error(injected=False, session_id="sess-whatever"),
        "interactive + populated session id -> no raise",
    )


def main() -> int:
    print(
        "plugins/agent_messaging_plugin/tests/"
        "managed_bridge_session_id_guard_smoke.py",
    )
    test_managed_empty_session_id_dies_loud()
    test_managed_populated_session_id_passes()
    test_interactive_empty_session_id_still_permitted()
    test_interactive_populated_session_id_passes()

    print()
    print(f"passed: {_passed}")
    if _failed:
        print(f"failed: {len(_failed)}")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
