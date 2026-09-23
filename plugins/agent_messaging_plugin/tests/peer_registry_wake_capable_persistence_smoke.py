#!/usr/bin/env python3
"""Codex ``wake_capable`` flag-loss smoke (fleet-wake-integrity Task 1, 2026-08-08).

Proves the write-side defect found by reading ``PeerRegistry.register()`` and
``_binding_from_row()`` directly: ``get_peer_binding_schema()``'s persisted
columns never included ``wake_capable``, ``register()``'s ``insert()`` dict
never wrote it, and ``_binding_from_row()`` never read it back -- so every
binding round-tripped through the real store silently fell back to
``BridgeBinding``'s dataclass default (``True``), regardless of what the
registering bridge declared. A cold-send to an idle stock-codex office
(which correctly declares ``wake_capable=False`` at registration --
``mcp_bridge/__main__.py``) therefore resolved a binding that read back
``wake_capable=True`` instead of the declared ``False`` — at the time this
smoke was written, that silently no-opped the dispatch-side spool tee
(``peer_dispatch.py``'s since-retired ``_tee_spool_if_wake_incapable``,
removed codex-0147-dead-spool-retirement 2026-08-13). The persistence defect
this smoke guards — a declared value silently dropped on the round trip —
is independent of that consumer and remains live: ``wake_capable`` is kept
as compatibility metadata on the schema/dataclass today.

This smoke names its failing mutation directly: swap ``schema.py``'s
``wake_capable`` column, ``register()``'s inclusion of it in the insert
dict, or ``_binding_from_row()``'s read of it back out, for a no-op, and
``test_wake_capable_false_persists_through_register_and_resolve`` reds.

Standalone -- not pytest. Run with::

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/peer_registry_wake_capable_persistence_smoke.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)


def _fresh_store() -> Store:
    return open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )


def _binding(
    *,
    bridge_id: str,
    agent_id: str,
    agent_instance_id: str,
    wake_capable: bool,
    agent_session_id: str = "",
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        session_label="",
        parent_pid=12345,
        agent_session_id=agent_session_id,
        wake_capable=wake_capable,
    )


def test_wake_capable_false_persists_through_register_and_resolve() -> None:
    """The exact defect: a stock-codex-shaped registration declares False."""
    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)
    instance = "agi-codex-office-001"

    registry.register(
        _binding(
            bridge_id="agc-codex-1",
            agent_id="codex",
            agent_instance_id=instance,
            wake_capable=False,
        ),
    )

    resolved = registry.resolve("codex", instance)
    assert resolved.wake_capable is False, (
        "registered wake_capable=False but resolve() read back "
        f"{resolved.wake_capable!r} -- the flag was dropped between "
        "register()'s insert and _binding_from_row()'s read"
    )
    print("  codex registration: wake_capable=False survived register()+resolve()")


def test_wake_capable_true_persists_through_register_and_resolve() -> None:
    """Symmetric positive case -- Claude Code's native-wake declaration."""
    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)
    instance = "agi-claude-code-001"

    registry.register(
        _binding(
            bridge_id="agc-cc-1",
            agent_id="claude_code",
            agent_instance_id=instance,
            wake_capable=True,
        ),
    )

    resolved = registry.resolve("claude_code", instance)
    assert resolved.wake_capable is True, (
        f"registered wake_capable=True but resolve() read back {resolved.wake_capable!r}"
    )
    print("  claude_code registration: wake_capable=True survived register()+resolve()")


def test_wake_capable_survives_restart() -> None:
    """Same persistence-boundary contract as session_label: a rebuilt
    ``PeerRegistry`` over the SAME store must still read the stored value,
    not silently reset to the dataclass default on every process restart."""
    store = _fresh_store()
    instance = "agi-codex-restart-001"

    registry_before = PeerRegistry(bindings_store=store)
    registry_before.register(
        _binding(
            bridge_id="agc-pre-restart",
            agent_id="codex",
            agent_instance_id=instance,
            wake_capable=False,
        ),
    )
    del registry_before

    registry_after = PeerRegistry(bindings_store=store)
    resolved = registry_after.resolve("codex", instance)
    assert resolved.wake_capable is False, (
        f"wake_capable=False did not survive a registry rebuild over the same "
        f"store, got {resolved.wake_capable!r}"
    )
    print("  post-restart: stored wake_capable=False survived")


def test_wake_capable_reasserted_on_reregister_not_preserved_on_default() -> None:
    """Unlike ``session_label``, ``wake_capable`` has no preserve-on-empty
    semantics (a bool has no "empty" state) -- every register() call must
    re-declare it and the new value must win outright, per the design
    comment in ``forwarder.py``: "nothing here survives implicitly"."""
    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)
    instance = "agi-codex-flap-001"

    registry.register(
        _binding(
            bridge_id="agc-1",
            agent_id="codex",
            agent_instance_id=instance,
            wake_capable=False,
        ),
    )
    assert registry.resolve("codex", instance).wake_capable is False

    # Same logical session reconnects and now declares True (e.g. the patched
    # build's native wake path came up). The new declaration must win.
    registry.register(
        _binding(
            bridge_id="agc-2",
            agent_id="codex",
            agent_instance_id=instance,
            wake_capable=True,
        ),
    )
    resolved = registry.resolve("codex", instance)
    assert resolved.wake_capable is True, (
        f"re-register with wake_capable=True must overwrite the prior False, "
        f"got {resolved.wake_capable!r}"
    )
    print("  re-register: wake_capable is re-asserted, not sticky")


def main() -> int:
    print("=== peer_registry_wake_capable_persistence_smoke ===")
    try:
        test_wake_capable_false_persists_through_register_and_resolve()
        test_wake_capable_true_persists_through_register_and_resolve()
        test_wake_capable_survives_restart()
        test_wake_capable_reasserted_on_reregister_not_preserved_on_default()
    except AssertionError:
        print("\nPEER_REGISTRY_WAKE_CAPABLE_PERSISTENCE_SMOKE: FAIL")
        traceback.print_exc()
        return 1
    except Exception:
        print("\nPEER_REGISTRY_WAKE_CAPABLE_PERSISTENCE_SMOKE: ERROR")
        traceback.print_exc()
        return 2
    print("\nPEER_REGISTRY_WAKE_CAPABLE_PERSISTENCE_SMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
