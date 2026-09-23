#!/usr/bin/env python3
"""MSG-04/identity-unification: routing convergence + collision analysis.

The coordinating seat's condition on the fix (`_resolve_watch_identity` preferring the
ledger `AGENT_INSTANCE_ID` over the derived `agi-watch-{digest}` scheme):
mutation evidence must show ROUTING actually converges — a send addressed
the way a dispatcher addresses a lane (peer instance id, read off
`session_status`/`peer_list`) must reach the SAME row the worker reads
under (`peer_inbox`'s `agent_session_id` resolution). Proving a function
RETURNS a different string is not proving that; this proves two independent
`PeerRegistry` resolution paths land on one persisted row.

Also proves the collision-analysis claim from the fix's own report: a
session re-registering under a NEW instance id (the one-time scheme
transition) leaves no orphan — the existing `agent_session_id`-keyed sweep
in `PeerRegistry.register` already deletes the stale row — and that the
role-claim layer's self-refresh check (`_protects_a_live_holder`, keyed on
`agent_session_id` too, REL-07/§4.3.2) already tolerates the same swap by
design, with no code change needed there.

Standalone — not pytest. Run with::

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/watch_ledger_identity_convergence_smoke.py
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.services.store import Store, open_store  # noqa: E402

import agent_messaging_plugin.local_cli.cli as cli_mod  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.role_binding_store import ResolvedRole  # noqa: E402
from agent_messaging_plugin.role_claim import _protects_a_live_holder  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

_LEDGER_ID = "agi-82be2462c96a9bcc2e12d25db4e1fdde"
_SESSION_ID = "ases-agi-82be2462c96a9bcc2e12d25db4e1fdde"
_OLD_DERIVED_ID = "agi-watch-oldhash0000000000000000"
_ROLE = "lane-msg04-bootstrap"


def _fresh_store() -> Store:
    return open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )


def _watch_env() -> dict[str, str]:
    return {
        "AGENT_SESSION_LABEL": _ROLE,
        "AGENT_SESSION_ID": _SESSION_ID,
        "AGENT_INSTANCE_ID": _LEDGER_ID,
    }


def test_direct_send_and_the_workers_own_read_resolve_to_one_row() -> None:
    """The core proof: DIRECT (peer_agent_instance_id) and self
    (agent_session_id) resolution paths must land on the SAME persisted row.

    Mutation that turns this red: reverting `_resolve_watch_identity` to
    always derive `agi-watch-{digest(session_id)}` — proven inline below by
    constructing the identity that reversion would have produced and showing
    DIRECT resolution against the ledger id then fails outright, which is the
    exact incident this fix closes.
    """
    with patch.dict(os.environ, _watch_env(), clear=True):
        identity = cli_mod._resolve_watch_identity(None, "claude_code")
    assert identity.agent_instance_id == _LEDGER_ID, (
        "fixture sanity: the identity under test must be the ledger id, or "
        "this test is not exercising the fix"
    )

    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)
    registry.register(
        BridgeBinding(
            bridge_id="agc-worker",
            agent_id=identity.agent_id,
            agent_instance_id=identity.agent_instance_id,
            session_label=identity.role,
            parent_pid=99999,
            agent_session_id=identity.agent_session_id,
            watcher_declared=True,
        ),
    )

    # A dispatcher addressing DIRECT the way `session_status`/`peer_list`
    # told it to — the ledger instance id.
    direct = registry.resolve("claude_code", peer_agent_instance_id=_LEDGER_ID)
    # The worker reading its OWN mail — `peer_inbox`'s resolution path.
    self_read = registry.resolve_by_agent_session_id(_SESSION_ID)

    assert direct == self_read, (
        "DIRECT send and the worker's own read must resolve to ONE row, not "
        f"two (direct={direct!r}, self_read={self_read!r})"
    )
    assert direct.is_watcher, (
        "watcher_declared must restore is_watcher despite no agi-watch- "
        "prefix on the registered identity"
    )
    print(
        "  DIRECT (peer_agent_instance_id) and self (agent_session_id) "
        "resolution converge on one row; is_watcher holds via watcher_declared",
    )

    # RED-side proof: the identity the PRE-FIX scheme would have produced
    # cannot be reached the same way — that IS the incident.
    pre_fix_row = registry.resolve_by_agent_session_id(_SESSION_ID)
    assert pre_fix_row.agent_instance_id != _OLD_DERIVED_ID
    try:
        registry.resolve("claude_code", peer_agent_instance_id=_OLD_DERIVED_ID)
    except Exception as exc:  # noqa: BLE001 — asserting the failure mode itself
        assert "peer_unreachable" in str(exc), exc
    else:
        raise AssertionError(
            "a DIRECT send to the pre-fix derived id must NOT resolve — if "
            "it does, the fixture no longer distinguishes old from new",
        )
    print(
        "  RED-side check: a DIRECT send to the pre-fix derived id still "
        "fails to resolve — confirms this fixture actually discriminates",
    )


def test_reconnect_under_ledger_identity_leaves_no_orphan() -> None:
    """Collision analysis, part 1: the one-time scheme-transition reconnect
    (same session, OLD derived id -> NEW ledger id) must not leave the old
    row stranded — `PeerRegistry.register`'s label sweep already keys on
    `agent_session_id`, so it recognizes this as the SAME session and
    deletes the stale row as part of the very registration that replaces it.
    """
    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)

    # The OLD registration, under the derived scheme, as it would have been
    # made before this fix shipped.
    registry.register(
        BridgeBinding(
            bridge_id="agc-worker-old",
            agent_id="claude_code",
            agent_instance_id=_OLD_DERIVED_ID,
            session_label=_ROLE,
            parent_pid=11111,
            agent_session_id=_SESSION_ID,
            watcher_declared=True,
        ),
    )

    # The reconnect, now running the fixed code, registers under the ledger
    # id — same session_id, same label, DIFFERENT instance id.
    registry.register(
        BridgeBinding(
            bridge_id="agc-worker-new",
            agent_id="claude_code",
            agent_instance_id=_LEDGER_ID,
            session_label=_ROLE,
            parent_pid=22222,
            agent_session_id=_SESSION_ID,
            watcher_declared=True,
        ),
    )

    assert registry.resolve_by_agent_instance_id(_OLD_DERIVED_ID) is None, (
        "the stale derived-id row must be swept on reconnect under the new "
        "identity, not linger as an orphan"
    )
    survivor = registry.resolve_by_agent_instance_id(_LEDGER_ID)
    assert survivor is not None
    assert survivor.bridge_id == "agc-worker-new"

    all_rows = registry.list_agent_ids().get("claude_code", [])
    same_label_rows = [row for row in all_rows if row.session_label == _ROLE]
    assert len(same_label_rows) == 1, (
        f"exactly one row must survive the identity swap, got {len(same_label_rows)}"
    )
    print(
        "  reconnect under the ledger identity sweeps the stale derived-id "
        "row via the existing agent_session_id-keyed sweep — no orphan",
    )


def test_role_claim_self_refresh_tolerates_the_identity_swap() -> None:
    """Collision analysis, part 2: the role-claim layer must treat the SAME
    scheme-transition reconnect as a self-refresh, not a foreign live holder
    to refuse (`role_held_live`). `_protects_a_live_holder` is a pure
    function keyed on `agent_session_id` (REL-07/§4.3.2) — this exercises it
    directly with the OLD holder row and the NEW claimant identity.
    """
    old_holder = ResolvedRole(
        name=_ROLE,
        agent_id="claude_code",
        agent_instance_id=_OLD_DERIVED_ID,
        session_label=_ROLE,
        agent_session_id=_SESSION_ID,
    )
    protects = _protects_a_live_holder(
        holder=old_holder,
        agent_session_id=_SESSION_ID,  # the reclaiming session — SAME session
        agent_instance_id=_LEDGER_ID,  # — under its NEW instance id
    )
    assert protects is False, (
        "same agent_session_id must read as self-refresh regardless of the "
        "instance id change, or the identity-unification reconnect would be "
        "refused as role_held_live"
    )
    print(
        "  role-claim self-refresh (_protects_a_live_holder) already "
        "tolerates the identity swap by design — no change needed there",
    )

    # Negative control: a GENUINELY different session must still be protected.
    different_session_protects = _protects_a_live_holder(
        holder=old_holder,
        agent_session_id="ases-some-other-session",
        agent_instance_id="agi-some-other-instance",
    )
    assert different_session_protects is True, (
        "a truly different session must still be protected — this fix must "
        "not weaken that guard"
    )
    print(
        "  negative control: a genuinely different session is still "
        "protected (role_held_live guard intact)",
    )


def main() -> int:
    print("=== watch_ledger_identity_convergence_smoke ===")
    try:
        test_direct_send_and_the_workers_own_read_resolve_to_one_row()
        test_reconnect_under_ledger_identity_leaves_no_orphan()
        test_role_claim_self_refresh_tolerates_the_identity_swap()
    except AssertionError:
        print("\nWATCH_LEDGER_IDENTITY_CONVERGENCE_SMOKE: FAIL")
        traceback.print_exc()
        return 1
    except Exception:
        print("\nWATCH_LEDGER_IDENTITY_CONVERGENCE_SMOKE: ERROR")
        traceback.print_exc()
        return 2
    print("\nWATCH_LEDGER_IDENTITY_CONVERGENCE_SMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
