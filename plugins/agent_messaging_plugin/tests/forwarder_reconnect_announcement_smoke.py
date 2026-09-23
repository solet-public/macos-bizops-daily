#!/usr/bin/env python3
"""Slice B smoke: forwarder emits a channel_message after _reconnect succeeds.

Verifies the surfacing edit from
``workbench/2026-06-01_local_reconnect_ux_design.md`` §5.1 / §6.3:

1. ``_register_identity`` returns the EFFECTIVE ``session_label`` from
   the peer/register response (Slice A's preserve-on-empty contract
   means this may differ from the subprocess's stale cache).
2. After ``_reconnect`` succeeds, the forwarder sends a JSON-RPC
   notification on ``notifications/claude/channel`` with content
   ``Solet reconnected -- peer_registry restored as <label>``
   when a label was restored, or
   ``Solet reconnected -- no prior label found`` when no stored
   label was recovered.
3. Failed registration (None return) suppresses the announcement —
   correctness over noise.
4. Send-side exceptions during the announcement do NOT propagate (the
   reconnect already succeeded; UX noise must not retry the network).

Stubs the HTTP layer (``Forwarder._post``) and the MCP write stream so
the smoke runs without a live solet or any MCP transport.

Standalone — not pytest. Run with::

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/forwarder_reconnect_announcement_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.mcp_bridge.forwarder import (  # noqa: E402
    CLAUDE_CHANNEL_NOTIFICATION_METHOD,
    BridgeHTTPError,
    Forwarder,
    _is_bridge_gone,
)


class _RecordingWriteStream:
    """Capture every ``send()`` payload for assertion."""

    def __init__(self, *, fail_on_send: bool = False) -> None:
        self.sent: list[Any] = []
        self._fail_on_send = fail_on_send

    async def send(self, message: Any) -> None:
        if self._fail_on_send:
            raise RuntimeError("simulated send-side failure")
        self.sent.append(message)


def _build_forwarder(
    *,
    cached_label: str,
    agent_session_id: str = "",
    write_stream: _RecordingWriteStream | None,
    register_response: dict[str, Any] | Exception | None,
) -> Forwarder:
    """Construct a Forwarder with its HTTP and stream layers stubbed.

    ``register_response`` controls what ``_post(...peer/register...)``
    returns or raises; passing ``None`` makes the stub return ``{}``.
    """
    forwarder = Forwarder(
        base_url="http://stub",
        solet_name="example",
        agent_id="claude_code",
        agent_instance_id="agi-smoke-001",
        agent_session_id=agent_session_id,
        session_label=cached_label,
        parent_pid=12345,
        provides_inference=True,
    )
    forwarder._bridge_id = "agc-stub"  # noqa: SLF001 — smoke needs to bypass open
    if write_stream is not None:
        forwarder.bind_write_stream(write_stream)  # type: ignore[arg-type]

    async def _stub_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        del body
        if "/peer/register" in path:
            if isinstance(register_response, Exception):
                raise register_response
            return register_response or {}
        return {}

    forwarder._post = _stub_post  # type: ignore[method-assign]

    async def _noop_open() -> None:
        forwarder._bridge_id = "agc-stub"

    forwarder._open_with_retry = _noop_open  # type: ignore[method-assign]
    return forwarder


def _channel_payload(message: Any) -> dict[str, Any]:
    """Extract the JSON-RPC params from a captured SessionMessage."""
    inner = message.message.root
    method = str(getattr(inner, "method", ""))
    params_raw = getattr(inner, "params", None) or {}
    if isinstance(params_raw, dict):
        params = params_raw
    else:
        params = json.loads(json.dumps(params_raw, default=str))
    return {"method": method, "params": params}


async def test_announce_restored_label() -> None:
    """Server returns a restored label → announcement names it."""
    stream = _RecordingWriteStream()
    forwarder = _build_forwarder(
        cached_label="",
        write_stream=stream,
        register_response={"session_label": "Coordinator", "status": "registered"},
    )
    await forwarder._reconnect()  # noqa: SLF001
    assert len(stream.sent) == 1, f"expected one announcement, got {len(stream.sent)}"
    captured = _channel_payload(stream.sent[0])
    assert captured["method"] == CLAUDE_CHANNEL_NOTIFICATION_METHOD, (
        f"announcement uses claude channel method, got {captured['method']!r}"
    )
    content = str(captured["params"]["content"])
    assert content == "Solet reconnected -- peer_registry restored as Coordinator", (
        f"announcement prose: got {content!r}"
    )
    # Wire-meta shape: Claude Code accepts the canonical 5-key envelope
    # (source / event_type / source_event_type / flow_id / cursor) and
    # silently rejects events with an empty flow_id. Verify both
    # constraints — the announcement renderer must actually surface.
    meta = captured["params"]["meta"]
    assert set(meta.keys()) == {
        "source", "event_type", "source_event_type", "flow_id", "cursor",
    }, f"meta keys: {sorted(meta.keys())!r}"
    assert meta["flow_id"], (
        f"flow_id must be non-empty (Claude Code drops empty-flow_id events); "
        f"got {meta['flow_id']!r}"
    )
    assert meta["source"] == "homunculus"
    assert meta["event_type"] == "post_message"
    print("  restored-label announcement carries 'restored as Coordinator'")
    print("  wire meta matches the canonical 5-key shape with non-empty flow_id")


async def test_announce_no_label_when_server_returned_empty() -> None:
    """No stored label recovered → 'no prior label found' branch."""
    stream = _RecordingWriteStream()
    forwarder = _build_forwarder(
        cached_label="",
        write_stream=stream,
        register_response={"session_label": "", "status": "registered"},
    )
    await forwarder._reconnect()  # noqa: SLF001
    assert len(stream.sent) == 1
    content = str(_channel_payload(stream.sent[0])["params"]["content"])
    assert content == "Solet reconnected -- no prior label found", (
        f"announcement prose: got {content!r}"
    )
    print("  empty-label announcement carries 'no prior label found'")


async def test_register_failure_suppresses_announcement() -> None:
    """Registration error → no channel message; reconnect itself does not raise."""
    stream = _RecordingWriteStream()
    forwarder = _build_forwarder(
        cached_label="",
        write_stream=stream,
        register_response=RuntimeError("simulated 503"),
    )
    await forwarder._reconnect()  # noqa: SLF001
    assert stream.sent == [], (
        "failed registration must suppress the announcement"
    )
    print("  registration failure suppresses announcement (no noise)")


async def test_send_failure_does_not_raise() -> None:
    """Best-effort surface: a write-stream send failure must not propagate."""
    stream = _RecordingWriteStream(fail_on_send=True)
    forwarder = _build_forwarder(
        cached_label="",
        write_stream=stream,
        register_response={"session_label": "Coordinator", "status": "registered"},
    )
    # If this raises, the test fails — the reconnect-UX surface must never
    # let a stream-send error escape the reconnect path.
    await forwarder._reconnect()  # noqa: SLF001
    print("  send-side exception swallowed (reconnect path stays clean)")


async def test_no_write_stream_does_not_raise() -> None:
    """Early-startup race: no write stream bound yet → silent no-op."""
    forwarder = _build_forwarder(
        cached_label="",
        write_stream=None,
        register_response={"session_label": "Coordinator", "status": "registered"},
    )
    await forwarder._reconnect()  # noqa: SLF001
    print("  missing write stream handled gracefully")


async def test_register_response_caches_effective_agent_session_id() -> None:
    """A server-preserved logical session id must update the forwarder cache."""
    forwarder = _build_forwarder(
        cached_label="",
        write_stream=None,
        register_response={
            "agent_session_id": "ases-preserved",
            "session_label": "Coordinator",
            "status": "registered",
        },
    )
    await forwarder._register_identity()  # noqa: SLF001
    assert forwarder._agent_session_id == "ases-preserved", (  # noqa: SLF001
        "forwarder did not cache agent_session_id returned by /peer/register"
    )

    forwarder._agent_session_id = ""  # noqa: SLF001
    await forwarder._reassert_identity()  # noqa: SLF001
    assert forwarder._agent_session_id == "ases-preserved", (  # noqa: SLF001
        "steady-state reassert did not cache preserved agent_session_id"
    )
    print("  register + steady-state reassert cache preserved agent_session_id")


async def test_current_identity_adopts_server_agent_session_id() -> None:
    """current_identity must not hide a repaired server-side session id."""
    forwarder = _build_forwarder(
        cached_label="Codex-Reviewer",
        write_stream=None,
        register_response=None,
    )

    async def _stub_get(path: str) -> dict[str, Any]:
        assert path.endswith("/current_identity"), path
        return {
            "agent_id": "codex",
            "agent_instance_id": "agi-smoke-001",
            "agent_session_id": "ases-from-server",
            "session_label": "Codex-Reviewer",
            "bridge_id": "agc-stub",
            "roles_held": ["Codex-Reviewer"],
        }

    forwarder._get = _stub_get  # type: ignore[method-assign]
    payload = await forwarder.current_identity()
    assert payload["agent_session_id"] == "ases-from-server", (
        "current_identity returned the stale local session id instead of "
        "the repaired server value"
    )
    assert forwarder._agent_session_id == "ases-from-server"  # noqa: SLF001
    print("  current_identity adopts repaired server agent_session_id")


def test_404_on_bridge_path_classified_as_stale() -> None:
    """2026-06-02 follow-on: 404 on /api/v1/bridge/agc-... ⇒ stale-bridge."""
    exc = BridgeHTTPError(
        "Solet /api/v1/bridge/agc-stale/peer/send failed (404): bridge_not_found",
        status_code=404,
        path="/api/v1/bridge/agc-stale/peer/send",
        response_code="bridge_not_found",
    )
    assert _is_bridge_gone(exc), (
        "404 on an agc- bridge-prefixed path must classify as stale"
    )
    print("  404 on /api/v1/bridge/agc-.../... classified as stale")


def test_404_on_non_bridge_path_does_not_falsely_classify() -> None:
    """Defensive path match: 404 outside the bridge-id route MUST NOT trigger reconnect."""
    exc = BridgeHTTPError(
        "Solet /api/v1/bridge/open failed (404): something else",
        status_code=404,
        path="/api/v1/bridge/open",
    )
    assert not _is_bridge_gone(exc), (
        "404 on a non-bridge-id-prefixed route must NOT classify as stale"
    )
    print("  404 on /api/v1/bridge/open NOT classified as stale (defensive)")


def test_500_on_bridge_path_does_not_classify_as_stale() -> None:
    """Status discrimination: only 404 implies stale-bridge."""
    exc = BridgeHTTPError(
        "Solet /api/v1/bridge/agc-live/peer/send failed (500): boom",
        status_code=500,
        path="/api/v1/bridge/agc-live/peer/send",
    )
    assert not _is_bridge_gone(exc), (
        "500 on bridge-prefixed path is a server error, not a stale bridge"
    )
    print("  500 on bridge-prefixed path NOT classified as stale")


def test_unstructured_404_fails_closed() -> None:
    """Unstructured errors must not reconnect a bridge on ambiguous evidence."""
    exc = BridgeHTTPError("Bridge not found or closed")
    assert not _is_bridge_gone(exc), (
        "an error without the affirmative server response code must not "
        "classify as a stale bridge"
    )
    print("  unstructured 404 evidence fails closed")


def test_async_call_with_reconnect_triggers_on_404_bridge_path() -> None:
    """End-to-end: a 404 raised by the HTTP layer ⇒ _call_with_reconnect fires reconnect."""

    async def _drive() -> dict[str, Any]:
        stream = _RecordingWriteStream()
        forwarder = _build_forwarder(
            cached_label="",
            write_stream=stream,
            register_response={"session_label": "Coordinator", "status": "registered"},
        )
        call_count = {"n": 0}

        async def _flaky_operation() -> dict[str, Any]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise BridgeHTTPError(
                    "Solet /api/v1/bridge/agc-stale/peer/send failed (404): "
                    "bridge_not_found",
                    status_code=404,
                    path="/api/v1/bridge/agc-stale/peer/send",
                    response_code="bridge_not_found",
                )
            return {"ok": True, "attempt": call_count["n"]}

        result = await forwarder._call_with_reconnect("peer_send", _flaky_operation)  # noqa: SLF001
        return {**result, "calls": call_count["n"]}

    result = asyncio.run(_drive())
    assert result["calls"] == 2, (
        f"expected one-shot reconnect-and-retry; got {result['calls']} calls"
    )
    assert result["ok"], "second attempt should succeed after reconnect"
    print("  _call_with_reconnect retries once after 404-on-bridge-path stale signal")


def test_register_payload_declares_inference_capability() -> None:
    """INF-01 client half: every register POST carries ``provides_inference``.

    The server's Trigger-1 vacancy-fill (autonomic_assignment.on_register)
    and the provider-sidecar populate both key off this field; a payload
    without it registers the session as non-provider and leaves the
    ``sys:autonomic`` slot permanently vacant (the 2026-07-10 chronic
    DEFER-loud boot signature). Pin BOTH register surfaces — the
    auto-registration on open/reconnect AND the manual ``peer_register``
    tool (a relabel must not demote the session to non-provider).
    """

    async def _drive() -> list[dict[str, Any]]:
        forwarder = _build_forwarder(
            cached_label="",
            write_stream=None,
            register_response=None,
        )
        register_bodies: list[dict[str, Any]] = []

        async def _stub_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
            if path.endswith("/peer/register"):
                register_bodies.append(body)
                return {"session_label": "Coordinator", "status": "registered"}
            return {}

        forwarder._post = _stub_post  # type: ignore[method-assign]
        await forwarder._register_identity()  # noqa: SLF001
        await forwarder.peer_register(agent_id="claude_code")
        return register_bodies

    bodies = asyncio.run(_drive())
    assert len(bodies) == 2, f"expected two register POSTs, got {len(bodies)}"
    for surface, body in zip(
        ("auto-registration", "peer_register"), bodies, strict=True,
    ):
        assert body.get("provides_inference") is True, (
            f"{surface} register payload must declare provides_inference=True "
            f"(got {body.get('provides_inference')!r}) — without it the "
            "provider sidecar never populates and sys:autonomic stays vacant"
        )
    print("  both register surfaces declare provides_inference=True")


def test_peer_send_retries_with_fresh_bridge_id() -> None:
    """High-level peer_send must rebuild its path after reconnect."""

    async def _drive() -> dict[str, Any]:
        stream = _RecordingWriteStream()
        forwarder = _build_forwarder(
            cached_label="",
            write_stream=stream,
            register_response={"session_label": "Coordinator", "status": "registered"},
        )
        forwarder._bridge_id = "agc-old"  # noqa: SLF001
        seen_paths: list[str] = []
        peer_send_count = {"n": 0}

        async def _stub_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
            del body
            seen_paths.append(path)
            if path.endswith("/peer/register"):
                return {"session_label": "Coordinator", "status": "registered"}
            if path.endswith("/peer/send"):
                peer_send_count["n"] += 1
                if peer_send_count["n"] == 1:
                    raise BridgeHTTPError(
                        "Solet /api/v1/bridge/agc-old/peer/send failed (404): "
                        "bridge_not_found",
                        status_code=404,
                        path=path,
                        response_code="bridge_not_found",
                    )
                return {"ok": True, "path": path}
            return {}

        async def _open_new_bridge() -> None:
            forwarder._bridge_id = "agc-new"  # noqa: SLF001

        forwarder._post = _stub_post  # type: ignore[method-assign]
        forwarder._open_with_retry = _open_new_bridge  # type: ignore[method-assign]
        result = await forwarder.peer_send(
            peer_id="codex",
            content=[{"type": "text", "text": "IMPORTANT test"}],
        )
        return {**result, "seen_paths": seen_paths}

    result = asyncio.run(_drive())
    seen_paths = result["seen_paths"]
    assert seen_paths[0] == "/api/v1/bridge/agc-old/peer/send", (
        f"first call should use stale id, got {seen_paths[0]!r}"
    )
    assert "/api/v1/bridge/agc-new/peer/register" in seen_paths, (
        f"reconnect should re-register on the fresh bridge, got {seen_paths!r}"
    )
    assert result["path"] == "/api/v1/bridge/agc-new/peer/send", (
        "retry must rebuild the path from the fresh bridge id; "
        f"got {result['path']!r}"
    )
    print("  peer_send retry rebuilds path with the fresh bridge_id")


def main() -> int:
    print("=== forwarder_reconnect_announcement_smoke ===")
    try:
        asyncio.run(test_announce_restored_label())
        asyncio.run(test_announce_no_label_when_server_returned_empty())
        asyncio.run(test_register_failure_suppresses_announcement())
        asyncio.run(test_send_failure_does_not_raise())
        asyncio.run(test_no_write_stream_does_not_raise())
        asyncio.run(test_register_response_caches_effective_agent_session_id())
        asyncio.run(test_current_identity_adopts_server_agent_session_id())
        test_404_on_bridge_path_classified_as_stale()
        test_404_on_non_bridge_path_does_not_falsely_classify()
        test_500_on_bridge_path_does_not_classify_as_stale()
        test_unstructured_404_fails_closed()
        test_async_call_with_reconnect_triggers_on_404_bridge_path()
        test_register_payload_declares_inference_capability()
        test_peer_send_retries_with_fresh_bridge_id()
    except AssertionError:
        print("\nFORWARDER_RECONNECT_ANNOUNCEMENT_SMOKE: FAIL")
        traceback.print_exc()
        return 1
    except Exception:
        print("\nFORWARDER_RECONNECT_ANNOUNCEMENT_SMOKE: ERROR")
        traceback.print_exc()
        return 2
    print("\nFORWARDER_RECONNECT_ANNOUNCEMENT_SMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
