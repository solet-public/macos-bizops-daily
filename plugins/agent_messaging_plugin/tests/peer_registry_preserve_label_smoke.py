#!/usr/bin/env python3
"""Slice A smoke: PeerRegistry preserves ``session_label`` across a solet restart.

Verifies the read-before-delete contract from
``workbench/2026-06-01_local_reconnect_ux_design.md`` §4.2:

1. An incoming ``register()`` with a non-empty ``session_label`` writes
   it (operator-explicit case — `/rename Coordinator`).
2. A subsequent ``register()`` with the SAME ``agent_instance_id`` but
   an EMPTY ``session_label`` (auto-reconnect's stale-cache case)
   preserves the stored label rather than wiping it.
3. A subsequent ``register()`` with a non-empty incoming label
   OVERWRITES the stored one (operator re-`/rename`).
4. The "restart" simulation: the same persistence-boundary ``Store``
   is reused across two ``PeerRegistry`` instances. With the
   production ``backend="postgres"`` swap, that boundary is a
   Postgres table; the test exercises the contract through the in-memory
   backend that the platform's ``Store`` abstraction shares with the
   Postgres backend at the API level (same ``insert`` / ``read_one`` /
   ``delete`` shape).

Standalone — not pytest. Run with::

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/peer_registry_preserve_label_smoke.py
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
    agent_instance_id: str,
    session_label: str,
    agent_session_id: str = "",
) -> BridgeBinding:
    return BridgeBinding(
        bridge_id=bridge_id,
        agent_id="claude_code",
        agent_instance_id=agent_instance_id,
        session_label=session_label,
        parent_pid=12345,
        agent_session_id=agent_session_id,
    )


def test_preserve_label_on_empty_reregister() -> None:
    """The canonical Coordinator-label-loss case: rename → restart → reconnect."""
    store = _fresh_store()
    instance = "agi-coordinator-001"

    # 1. Initial registration carries the operator's /rename label.
    registry_before_restart = PeerRegistry(bindings_store=store)
    registry_before_restart.register(
        _binding(
            bridge_id="agc-pre-restart",
            agent_instance_id=instance,
            session_label="Coordinator",
        ),
    )

    listing_before = registry_before_restart.list_agent_ids()["claude_code"]
    assert len(listing_before) == 1, "pre-restart: one binding registered"
    assert listing_before[0].session_label == "Coordinator", (
        "pre-restart: label persisted as written"
    )
    print("  pre-restart: Coordinator label persisted")

    # 2. Simulate a solet restart: drop the old registry, rebuild over the SAME
    # store (the persistence boundary that postgres would survive across).
    del registry_before_restart
    registry_after_restart = PeerRegistry(bindings_store=store)

    listing_post_restart = registry_after_restart.list_agent_ids()["claude_code"]
    assert len(listing_post_restart) == 1, "post-restart: row survived"
    assert listing_post_restart[0].session_label == "Coordinator", (
        "post-restart: stored label survived"
    )
    print("  post-restart: stored label survived")

    # 3. Auto-reconnect: subprocess's stale cache sends an empty label.
    # The new bridge_id wins; the empty label must NOT wipe the stored one.
    registry_after_restart.register(
        _binding(
            bridge_id="agc-post-restart",
            agent_instance_id=instance,
            session_label="",
        ),
    )

    listing_reconnect = registry_after_restart.list_agent_ids()["claude_code"]
    assert len(listing_reconnect) == 1, "reconnect: still one binding"
    reconnect_binding = listing_reconnect[0]
    assert reconnect_binding.bridge_id == "agc-post-restart", (
        "reconnect: bridge_id flipped to the new connection"
    )
    assert reconnect_binding.session_label == "Coordinator", (
        f"reconnect: stored label preserved, "
        f"got {reconnect_binding.session_label!r}"
    )
    print("  auto-reconnect: empty-label register preserved 'Coordinator'")


def test_explicit_rename_overwrites_stored() -> None:
    """Non-empty incoming label overwrites — operator's explicit /rename wins."""
    store = _fresh_store()
    instance = "agi-watchdog-001"

    registry = PeerRegistry(bindings_store=store)
    registry.register(
        _binding(
            bridge_id="agc-1",
            agent_instance_id=instance,
            session_label="Watchdog-old",
        ),
    )

    # Operator runs /rename Watchdog-new
    registry.register(
        _binding(
            bridge_id="agc-1",
            agent_instance_id=instance,
            session_label="Watchdog-new",
        ),
    )

    listing = registry.list_agent_ids()["claude_code"]
    assert len(listing) == 1
    assert listing[0].session_label == "Watchdog-new", (
        f"explicit /rename overwrites stored label, "
        f"got {listing[0].session_label!r}"
    )
    print("  explicit /rename overwrites stored label")


def test_preserve_agent_session_id_on_empty_reregister() -> None:
    """An empty reconnect must not erase the logical session key."""
    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)
    instance = "agi-session-key-001"

    registry.register(
        _binding(
            bridge_id="agc-1",
            agent_instance_id=instance,
            session_label="Coordinator",
            agent_session_id="ases-stable-001",
        ),
    )

    registry.register(
        _binding(
            bridge_id="agc-2",
            agent_instance_id=instance,
            session_label="",
            agent_session_id="",
        ),
    )
    listing = registry.list_agent_ids()["claude_code"]
    assert len(listing) == 1
    assert listing[0].bridge_id == "agc-2"
    assert listing[0].session_label == "Coordinator"
    assert listing[0].agent_session_id == "ases-stable-001", (
        "empty re-register erased the stored agent_session_id"
    )
    print("  empty re-register preserved agent_session_id")


def test_explicit_agent_session_id_overwrites_stored() -> None:
    """A non-empty incoming logical session id remains authoritative."""
    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)
    instance = "agi-session-key-002"

    registry.register(
        _binding(
            bridge_id="agc-1",
            agent_instance_id=instance,
            session_label="Coordinator",
            agent_session_id="ases-old",
        ),
    )
    registry.register(
        _binding(
            bridge_id="agc-2",
            agent_instance_id=instance,
            session_label="Coordinator",
            agent_session_id="ases-new",
        ),
    )

    listing = registry.list_agent_ids()["claude_code"]
    assert len(listing) == 1
    assert listing[0].agent_session_id == "ases-new", (
        "explicit incoming agent_session_id must overwrite the stored value"
    )
    print("  explicit agent_session_id overwrites stored value")


def test_new_session_claiming_same_label_evicts_prior_holder() -> None:
    """Single-active-session-per-name invariant (operator directive 2026-06-09).

    A different ``agent_instance_id`` claiming an already-held session_label
    via ``/rename`` must evict the previous holder so ``peer_list`` never
    shows two rows answering to the same display name.
    """
    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)

    # Session A claims the label.
    registry.register(
        _binding(
            bridge_id="agc-session-A",
            agent_instance_id="agi-session-A",
            session_label="Coordinator-Day",
        ),
    )
    listing_a = registry.list_agent_ids()["claude_code"]
    assert len(listing_a) == 1
    assert listing_a[0].agent_instance_id == "agi-session-A"
    print("  session A registered as Coordinator-Day")

    # Session B claims the SAME label (fresh agent_instance_id + bridge_id).
    # Without the session_label sweep, both rows would coexist.
    registry.register(
        _binding(
            bridge_id="agc-session-B",
            agent_instance_id="agi-session-B",
            session_label="Coordinator-Day",
        ),
    )
    listing_b = registry.list_agent_ids()["claude_code"]
    assert len(listing_b) == 1, (
        f"single-active-session-per-name violated: "
        f"{len(listing_b)} rows hold the same label"
    )
    assert listing_b[0].agent_instance_id == "agi-session-B", (
        "session B should be the sole holder of Coordinator-Day"
    )
    print("  session B claim evicted session A; Coordinator-Day single-occupant")

    # Idempotency: session B re-registering with the same label keeps one row.
    registry.register(
        _binding(
            bridge_id="agc-session-B-reconnect",
            agent_instance_id="agi-session-B",
            session_label="Coordinator-Day",
        ),
    )
    listing_b_reconnect = registry.list_agent_ids()["claude_code"]
    assert len(listing_b_reconnect) == 1
    assert listing_b_reconnect[0].bridge_id == "agc-session-B-reconnect"
    print("  session B reconnect: still single occupant")


def test_first_registration_with_empty_label_stores_empty() -> None:
    """No stored label + empty incoming → empty stays empty (baseline behavior)."""
    store = _fresh_store()
    registry = PeerRegistry(bindings_store=store)
    registry.register(
        _binding(
            bridge_id="agc-fresh",
            agent_instance_id="agi-fresh-001",
            session_label="",
        ),
    )

    listing = registry.list_agent_ids()["claude_code"]
    assert len(listing) == 1
    assert listing[0].session_label == "", (
        "first-ever registration with no label remains label-less "
        "(no /rename has happened yet)"
    )
    print("  first-time empty-label registration stays empty")


def main() -> int:
    print("=== peer_registry_preserve_label_smoke ===")
    try:
        test_preserve_label_on_empty_reregister()
        test_explicit_rename_overwrites_stored()
        test_preserve_agent_session_id_on_empty_reregister()
        test_explicit_agent_session_id_overwrites_stored()
        test_new_session_claiming_same_label_evicts_prior_holder()
        test_first_registration_with_empty_label_stores_empty()
    except AssertionError:
        print("\nPEER_REGISTRY_PRESERVE_LABEL_SMOKE: FAIL")
        traceback.print_exc()
        return 1
    except Exception:
        print("\nPEER_REGISTRY_PRESERVE_LABEL_SMOKE: ERROR")
        traceback.print_exc()
        return 2
    print("\nPEER_REGISTRY_PRESERVE_LABEL_SMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
