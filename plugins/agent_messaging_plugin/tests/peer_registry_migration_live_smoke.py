#!/usr/bin/env python3
"""Smoke verification of the :class:`PeerRegistry` -> :class:`Store` migration.

Drives the live solet's bridge HTTP API.  Verifies:

1. ``peer_list`` response now surfaces ``created_at`` AND ``updated_at``
   alongside the deprecated-alias ``registered_at``.
2. ``register`` (via POST /peer/register) sets ``created_at == updated_at``
   on a fresh binding (no dispatch has happened yet).
3. ``peer_send`` between two bridges bumps ``updated_at`` on the SENDER
   binding without disturbing ``created_at``.
4. The recipient binding's ``updated_at`` also advances (every
   dispatch touches both endpoints).
5. ``close`` for either bridge cleans its binding out of the registry.

Requires the solet to be running with the bridge interface up; reads the
bridge port from ``~/.ananta/runtime/<name>.bridge.port`` (the same
discovery path the MCP bridge subprocess uses).

NOTE: Run this AFTER restarting the solet with the post-migration code.  The
running bridge before restart still ships the pre-migration in-memory
``threading.Lock``-based registry, which never sets ``updated_at``.

Standalone — not pytest.  Run with::

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/peer_registry_migration_live_smoke.py
"""


from __future__ import annotations

# ruff: noqa: E402
import contextlib
import os
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.core.runtime.port_manager import read_port_file

SMOKE_AGENT_ID = f"store_smoke_{uuid4().hex[:6]}"


def _read_bridge_port() -> int:
    solet = os.environ["SOLET_NAME"]
    port = read_port_file(service_name="bridge", solet_name=solet)
    if port is None:
        raise RuntimeError(
            f"bridge port file not found for solet {solet!r}; "
            "is the solet running?",
        )
    return port


_LIVE_ENV = "PEER_REGISTRY_MIGRATION_LIVE_SMOKE"


def _prereq_skip_reason() -> str | None:
    """SKIP-reason (None → run). Gate this live smoke so the offline suite
    NEVER fails-red offline and NEVER silent-writes the running solet's
    peer registry: skip unless the explicit ``PEER_REGISTRY_MIGRATION_LIVE_SMOKE=1``
    opt-in is set (the ``*_live_smoke`` convention), and skip-clean when the
    bridge is unreachable (mirrors ``cross_host_kara_ledger``'s reachability model)."""
    if os.environ.get(_LIVE_ENV) != "1":
        return f"set {_LIVE_ENV}=1 to run (LIVE-writes the running solet's peer registry)"
    solet = os.environ["SOLET_NAME"]
    port = read_port_file(service_name="bridge", solet_name=solet)
    if port is None:
        return f"bridge port file not found for solet {solet!r} (is the solet running?)"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0):
            pass
    except OSError as exc:
        return f"bridge TCP connect to 127.0.0.1:{port} failed: {exc}"
    return None


def _http(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    import json
    data = json.dumps(body or {}).encode("utf-8") if body is not None or method != "GET" else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read().decode("utf-8") or "{}"
            decoded: dict[str, Any] = json.loads(payload)
            return decoded
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} {method} {url}: {body_text}",
        ) from exc


def open_bridge(base: str) -> dict[str, Any]:
    return _http("POST", f"{base}/api/v1/bridge/open", {})


def close_bridge(base: str, bridge_id: str) -> None:
    _http("POST", f"{base}/api/v1/bridge/{bridge_id}/close", {})


def register_peer(
    base: str, bridge_id: str, *, agent_id: str, agent_instance_id: str,
    session_label: str, parent_pid: int,
) -> dict[str, Any]:
    return _http(
        "POST",
        f"{base}/api/v1/bridge/{bridge_id}/peer/register",
        {
            "agent_id": agent_id,
            "agent_instance_id": agent_instance_id,
            "session_label": session_label,
            "parent_pid": parent_pid,
        },
    )


def peer_list(base: str, bridge_id: str) -> dict[str, Any]:
    return _http("GET", f"{base}/api/v1/bridge/{bridge_id}/peer/list")


def peer_send(
    base: str, bridge_id: str, *,
    peer_id: str, peer_agent_instance_id: str, text: str,
) -> dict[str, Any]:
    return _http(
        "POST",
        f"{base}/api/v1/bridge/{bridge_id}/peer/send",
        {
            "peer_id": peer_id,
            "peer_agent_instance_id": peer_agent_instance_id,
            "content": [{"type": "text", "text": text}],
        },
    )


def find_instance(
    listing: dict[str, Any], agent_id: str, instance_id: str,
) -> dict[str, Any] | None:
    for entry in listing.get("instances", {}).get(agent_id, []):
        if entry.get("agent_instance_id") == instance_id:
            matched: dict[str, Any] = entry
            return matched
    return None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    skip_reason = _prereq_skip_reason()
    if skip_reason is not None:
        print(f"[SKIP] {skip_reason}")
        return 0
    port = _read_bridge_port()
    base = f"http://127.0.0.1:{port}"
    print(f"--- bridge discovered on {base}")

    bridge_a: dict[str, Any] | None = None
    bridge_b: dict[str, Any] | None = None
    try:
        agent_id_a = f"{SMOKE_AGENT_ID}_a"
        agent_id_b = f"{SMOKE_AGENT_ID}_b"
        instance_a = f"agi-{uuid4().hex}"
        instance_b = f"agi-{uuid4().hex}"

        # 1. Open two bridges + register a peer on each.
        bridge_a = open_bridge(base)
        bridge_b = open_bridge(base)
        bridge_a_id = bridge_a["bridge_id"]
        bridge_b_id = bridge_b["bridge_id"]
        print(f"  opened bridges: {bridge_a_id}, {bridge_b_id}")

        register_peer(
            base, bridge_a_id, agent_id=agent_id_a,
            agent_instance_id=instance_a, session_label="smoke A",
            parent_pid=11111,
        )
        register_peer(
            base, bridge_b_id, agent_id=agent_id_b,
            agent_instance_id=instance_b, session_label="smoke B",
            parent_pid=22222,
        )
        print(f"  registered peers: {agent_id_a}/{instance_a}, {agent_id_b}/{instance_b}")

        # 2. Peer_list surfaces created_at + updated_at + deprecated alias.
        listing = peer_list(base, bridge_a_id)
        entry_a = find_instance(listing, agent_id_a, instance_a)
        entry_b = find_instance(listing, agent_id_b, instance_b)
        assert entry_a is not None, f"expected {agent_id_a}/{instance_a} in listing"
        assert entry_b is not None, f"expected {agent_id_b}/{instance_b} in listing"
        for entry, label in ((entry_a, "A"), (entry_b, "B")):
            assert "created_at" in entry, f"{label}: created_at missing"
            assert "updated_at" in entry, f"{label}: updated_at missing"
            assert "registered_at" in entry, (
                f"{label}: deprecated alias registered_at missing"
            )
            assert entry["registered_at"] == entry["created_at"], (
                f"{label}: registered_at should alias created_at"
            )
        print("  peer_list shape: created_at + updated_at + registered_at alias present")

        # 3. Fresh registration: created_at == updated_at (no dispatch yet).
        assert entry_a["created_at"] == entry_a["updated_at"], (
            f"A: fresh binding should have created_at == updated_at, "
            f"got created={entry_a['created_at']} updated={entry_a['updated_at']}"
        )
        assert entry_b["created_at"] == entry_b["updated_at"], (
            "B: fresh binding should have created_at == updated_at"
        )
        print("  fresh registrations: created_at == updated_at")

        created_a = entry_a["created_at"]
        created_b = entry_b["created_at"]
        prior_updated_a = entry_a["updated_at"]
        prior_updated_b = entry_b["updated_at"]

        # 4. Sleep then peer_send to drive an updated_at bump on both ends.
        time.sleep(1.2)
        peer_send(
            base, bridge_a_id, peer_id=agent_id_b,
            peer_agent_instance_id=instance_b,
            text="store-smoke ping",
        )
        print("  peer_send A -> B issued")

        listing_post = peer_list(base, bridge_a_id)
        entry_a_post = find_instance(listing_post, agent_id_a, instance_a)
        entry_b_post = find_instance(listing_post, agent_id_b, instance_b)
        assert entry_a_post is not None
        assert entry_b_post is not None

        # 5. Sender's updated_at advanced; created_at didn't.
        assert entry_a_post["created_at"] == created_a, (
            f"A: created_at must not move (was {created_a}, now {entry_a_post['created_at']})"
        )
        assert entry_a_post["updated_at"] > prior_updated_a, (
            f"A (sender): updated_at must advance through peer_send "
            f"(was {prior_updated_a}, now {entry_a_post['updated_at']})"
        )
        # 6. Recipient also touched (every dispatch hits both endpoints).
        assert entry_b_post["created_at"] == created_b, "B: created_at must not move"
        assert entry_b_post["updated_at"] > prior_updated_b, (
            f"B (recipient): updated_at must advance through peer_send "
            f"(was {prior_updated_b}, now {entry_b_post['updated_at']})"
        )
        print("  sender + recipient updated_at advanced; created_at frozen")

        # 7. Close bridge A -> binding A evicted.
        close_bridge(base, bridge_a_id)
        bridge_a = None
        listing_after_close = peer_list(base, bridge_b_id)
        assert find_instance(listing_after_close, agent_id_a, instance_a) is None, (
            "A: binding should be evicted after close"
        )
        assert find_instance(listing_after_close, agent_id_b, instance_b) is not None, (
            "B: binding should still be live after A closes"
        )
        print("  close(A) evicted A's binding; B untouched")

        # 8. Close bridge B -> all smoke bindings gone.
        close_bridge(base, bridge_b_id)
        bridge_b = None
        print("  cleanup complete")

        print()
        print("PEER_REGISTRY MIGRATION SMOKE: PASS")
        return 0

    except AssertionError as exc:
        print("PEER_REGISTRY MIGRATION SMOKE: FAIL")
        traceback.print_exc()
        print(f"\nAssertion failure: {exc}")
        return 1
    except Exception:
        print("PEER_REGISTRY MIGRATION SMOKE: ERROR")
        traceback.print_exc()
        return 2
    finally:
        # Best-effort: close any bridge we left dangling so a failed run
        # doesn't leave stale bindings behind for the next attempt.
        for handle in (bridge_a, bridge_b):
            if handle is None:
                continue
            with contextlib.suppress(Exception):  # cleanup is best-effort
                close_bridge(base, handle["bridge_id"])


if __name__ == "__main__":
    sys.exit(main())
