#!/usr/bin/env python3
"""Bridge peer-identity registration retry smoke — the half-alive-session defect.

`Forwarder.open_bridge()` opens the bridge and then registers the peer identity.
`_open_with_retry` loops UNBOUNDED until the solet accepts the open, but
`_register_identity` used to get exactly ONE attempt and swallow any exception
into a log line, returning None.

That asymmetry produced a silent half-alive session: the bridge is open, so
`bridge_id` exists and every process call (`process_call`, `process_result`,
`knowledge_service::search`) works normally -- but no peer-registry binding was
ever created, so the session is INVISIBLE to `peer_list`, cannot be addressed by
`peer_send`, and cannot read `peer_inbox` (the server answers "this bridge has
not registered an agent_id"). Observed live 2026-07-25 on three separate
sessions in one evening; each time it presented to the operator as "that agent
is idle / ignoring me", never as a registration error, because the only evidence
was one line on a subprocess's stderr.

RED-FIRST design: every case below fails if `_register_identity` is reverted to
a single attempt.

  R1  transient-then-success -- the exact startup race. Registration fails twice
      (solet HTTP not ready to serve /peer/register yet) then succeeds.
      Single-attempt code returns None here; the retry returns the label.
  R2  attempt budget is bounded and honoured -- a permanently failing endpoint
      makes exactly REGISTER_IDENTITY_ATTEMPTS attempts, then gives up. Guards
      both directions: no infinite startup hang, and no silent single try.
  R3  terminal failure is LOUD -- the give-up path must emit the operator-facing
      diagnostic naming the half-alive state, not just "registration failed".
  R4  the happy path still costs exactly one POST -- the retry must not turn
      normal startup into repeated registration traffic.

Hermetic: no bridge, no network, no sleeping. `_post` is replaced with a
scripted fake and the retry backoff is patched to a no-op.
"""

from __future__ import annotations

# ruff: noqa: E402
import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import agent_messaging_plugin.mcp_bridge.forwarder as fwd_mod
from agent_messaging_plugin.mcp_bridge.__main__ import (
    AGENT_ROLE_AUTOBIND_ENV,
    AGENT_ROLE_ENV,
    AGENT_SESSION_LABEL_ENV,
    _compute_session_role,
)
from agent_messaging_plugin.mcp_bridge.forwarder import (
    REGISTER_IDENTITY_ATTEMPTS,
    REGISTER_REASSERT_INTERVAL_S,
    Forwarder,
)

_BRIDGE_ID = "agc-smoke0000"
_LABEL = "Git-Controller"
# Sentinel: "the register response omits session_role_held entirely" — distinct
# from any value the server could legitimately send, including None.
_ABSENT = object()


def _make_forwarder(*, session_role: str = "") -> Forwarder:
    """A Forwarder with a bridge already open, so only registration is exercised."""
    fw = Forwarder(
        "http://127.0.0.1:1",
        "testling",
        agent_id="claude_code",
        agent_instance_id="agi-smoke",
        agent_session_id="ases-smoke",
        session_label=_LABEL,
        parent_pid=4242,
        provides_inference=True,
        session_role=session_role,
    )
    fw._bridge_id = _BRIDGE_ID  # noqa: SLF001 — bridge-open is not under test
    return fw


class _ScriptedPost:
    """Fails the first `failures` calls, then returns a register response."""

    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.calls = 0
        self.paths: list[str] = []

    async def __call__(self, path: str, _body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.paths.append(path)
        if self.calls <= self._failures:
            msg = f"connection refused (scripted failure {self.calls})"
            raise ConnectionError(msg)
        return {"session_label": _LABEL}


def _run_register(fw: Forwarder, post: _ScriptedPost) -> tuple[str | None, list[str]]:
    """Drive `_register_identity` with a scripted `_post` and captured logs."""
    logs: list[str] = []
    with (
        patch.object(fw, "_post", post),
        patch.object(fwd_mod, "_log", logs.append),
        patch.object(fwd_mod.asyncio, "sleep", _no_sleep),
    ):
        result = asyncio.run(fw._register_identity())  # noqa: SLF001
    return result, logs


async def _no_sleep(*_args: float) -> None:
    """Collapse the retry backoff so the smoke stays instant."""
    return None


def test_transient_failure_then_success_registers() -> None:
    # R1 — the startup race: the bridge opens before the solet is ready to
    # serve /peer/register. Two transient failures then success.
    fw = _make_forwarder()
    post = _ScriptedPost(failures=2)
    result, _ = _run_register(fw, post)
    assert result == _LABEL, f"expected label after retry, got {result!r}"
    assert post.calls == 3, f"expected 3 attempts (2 fail + 1 ok), got {post.calls}"
    assert all(p.endswith("/peer/register") for p in post.paths), post.paths


def test_attempt_budget_is_bounded_and_honoured() -> None:
    # R2 — a permanently failing endpoint must stop at the budget: not once
    # (the original defect) and not forever (would hang every session start).
    fw = _make_forwarder()
    post = _ScriptedPost(failures=REGISTER_IDENTITY_ATTEMPTS + 10)
    result, _ = _run_register(fw, post)
    assert result is None, f"expected None after exhausting attempts, got {result!r}"
    assert post.calls == REGISTER_IDENTITY_ATTEMPTS, (
        f"expected exactly {REGISTER_IDENTITY_ATTEMPTS} attempts, got {post.calls}"
    )


def test_terminal_failure_is_loud_about_the_half_alive_state() -> None:
    # R3 — the give-up log must tell an operator what actually broke. A bare
    # "registration failed" is what let this defect hide for so long.
    fw = _make_forwarder()
    post = _ScriptedPost(failures=REGISTER_IDENTITY_ATTEMPTS)
    _, logs = _run_register(fw, post)
    blob = "\n".join(logs)
    assert "PEER IDENTITY UNREGISTERED" in blob, blob
    assert "INVISIBLE" in blob, blob
    assert "peer_register" in blob, blob


def test_happy_path_costs_exactly_one_post() -> None:
    # R4 — the retry must not add traffic to the normal case.
    fw = _make_forwarder()
    post = _ScriptedPost(failures=0)
    result, logs = _run_register(fw, post)
    assert result == _LABEL
    assert post.calls == 1, f"expected a single POST, got {post.calls}"
    assert "PEER IDENTITY UNREGISTERED" not in "\n".join(logs)


def test_no_bridge_id_short_circuits_without_posting() -> None:
    # Guard the precondition: registration is meaningless before the open.
    fw = _make_forwarder()
    fw._bridge_id = None  # noqa: SLF001
    post = _ScriptedPost(failures=0)
    result, _ = _run_register(fw, post)
    assert result is None
    assert post.calls == 0, "must not POST without a bridge_id"


def test_steady_state_reassert_posts_and_is_silent_when_healthy() -> None:
    # R5 - the poll loop re-asserts the binding so a binding that goes missing
    # underneath a healthy bridge self-heals instead of staying a silent outage.
    fw = _make_forwarder()
    post = _ScriptedPost(failures=0)
    logs: list[str] = []
    with patch.object(fw, "_post", post), patch.object(fwd_mod, "_log", logs.append):
        asyncio.run(fw._reassert_identity())  # noqa: SLF001
    assert post.calls == 1, f"expected one re-assert POST, got {post.calls}"
    assert post.paths == [f"/api/v1/bridge/{_BRIDGE_ID}/peer/register"], post.paths
    assert not logs, f"steady-state re-assert must be silent when healthy: {logs}"


def test_steady_state_reassert_failure_is_logged_not_raised() -> None:
    # R6 - a failed re-assert must never propagate into the poll loop; the next
    # tick is the retry. Raising here would kill the drain that keeps the
    # session alive at all.
    fw = _make_forwarder()
    post = _ScriptedPost(failures=1)
    logs: list[str] = []
    with patch.object(fw, "_post", post), patch.object(fwd_mod, "_log", logs.append):
        asyncio.run(fw._reassert_identity())  # noqa: SLF001
    assert post.calls == 1, "re-assert must be single-attempt, not a retry storm"
    assert any("re-assert failed" in line for line in logs), logs


def test_dense_event_burst_does_not_accelerate_reassert() -> None:
    """Many immediate drains before the deadline must produce zero heartbeats."""
    now = 100.0
    fw = _make_forwarder()
    fw._monotonic_clock = lambda: now  # noqa: SLF001
    fw._poll_active = True  # noqa: SLF001
    drains = 0
    reasserts = 0

    async def _drain(_bridge_id: str, _generation: int | None = None) -> None:
        nonlocal drains
        drains += 1
        if drains == 100:
            fw._poll_active = False  # noqa: SLF001

    async def _reassert() -> None:
        nonlocal reasserts
        reasserts += 1

    with (
        patch.object(fw, "_drain_once", _drain),
        patch.object(fw, "_reassert_identity", _reassert),
    ):
        asyncio.run(fw._long_poll_loop())  # noqa: SLF001

    assert drains == 100
    assert reasserts == 0, (
        "event volume must not accelerate the elapsed-time heartbeat"
    )


def test_elapsed_deadline_reasserts_once() -> None:
    """Crossing one monotonic deadline produces exactly one reassertion."""
    now = 100.0
    fw = _make_forwarder()
    fw._monotonic_clock = lambda: now  # noqa: SLF001
    fw._poll_active = True  # noqa: SLF001
    drains = 0
    reasserts = 0

    async def _drain(_bridge_id: str, _generation: int | None = None) -> None:
        nonlocal drains, now
        drains += 1
        now += REGISTER_REASSERT_INTERVAL_S
        fw._poll_active = False  # noqa: SLF001

    async def _reassert() -> None:
        nonlocal reasserts
        reasserts += 1

    with (
        patch.object(fw, "_drain_once", _drain),
        patch.object(fw, "_reassert_identity", _reassert),
    ):
        asyncio.run(fw._long_poll_loop())  # noqa: SLF001

    assert drains == 1
    assert reasserts == 1, "one elapsed deadline must produce one heartbeat"


def test_reconnect_claims_configured_role_from_registered_identity() -> None:
    fw = _make_forwarder(session_role=_LABEL)
    posted: list[tuple[str, dict[str, Any]]] = []
    fetched: list[str] = []

    async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        posted.append((path, body))
        if path.endswith("/peer/register"):
            return {
                "agent_session_id": "ases-smoke",
                "session_label": _LABEL,
            }
        if path.endswith("/peer/claim_role"):
            # The SYNCHRONOUS shape: the outcome IS the response body.
            return {
                "action": "claimed",
                "name": _LABEL,
                "agent_instance_id": "agi-smoke",
                "agent_session_id": "ases-smoke",
            }
        raise AssertionError(f"unexpected POST path: {path}")

    async def _get(path: str) -> dict[str, Any]:
        fetched.append(path)
        raise AssertionError(f"unexpected GET path: {path}")

    async def _open() -> None:
        fw._bridge_id = _BRIDGE_ID  # noqa: SLF001

    async def _drive() -> None:
        with (
            patch.object(fw, "_post", _post),
            patch.object(fw, "_get", _get),
            patch.object(fwd_mod.asyncio, "sleep", _no_sleep),
        ):
            fw._open_with_retry = _open  # type: ignore[method-assign]
            await fw._reconnect()  # noqa: SLF001

    asyncio.run(_drive())
    assert [path for path, _ in posted] == [
        f"/api/v1/bridge/{_BRIDGE_ID}/peer/register",
        f"/api/v1/bridge/{_BRIDGE_ID}/peer/claim_role",
    ], posted
    # The synchronous route ends the /process/result poll outright. That poll was
    # a SECOND MODEL_INITIATED route the forwarder touched with no model turn, so
    # "no GETs at all" is a load-bearing assertion, not incidental tidiness.
    assert fetched == [], fetched
    # Only the role name travels: identity is read server-side from this bridge's
    # registered binding, so there is nothing here to spoof or to drift.
    assert posted[1][1] == {"name": _LABEL}, posted[1][1]


def test_failed_role_claim_is_loud_without_breaking_bridge_registration() -> None:
    fw = _make_forwarder(session_role=_LABEL)
    logs: list[str] = []

    async def _post(_path: str, _body: dict[str, Any]) -> dict[str, Any]:
        # The route answers a refusal in the body with no recognised ``action``.
        return {"code": "missing_session_id", "message": "scripted role rejection"}

    with patch.object(fw, "_post", _post), patch.object(
        fwd_mod,
        "_log",
        logs.append,
    ):
        claimed = asyncio.run(fw._claim_session_role())  # noqa: SLF001
    assert claimed is False
    assert any("ROLE UNCLAIMED" in line for line in logs), logs
    assert any(_LABEL in line for line in logs), logs


def test_explicit_session_label_defaults_to_standing_role() -> None:
    with patch.dict(
        os.environ,
        {AGENT_SESSION_LABEL_ENV: _LABEL},
        clear=True,
    ):
        assert _compute_session_role(_LABEL) == _LABEL


def test_explicit_role_can_differ_from_session_label() -> None:
    explicit_role = "Release-Coordinator"
    with patch.dict(
        os.environ,
        {
            AGENT_SESSION_LABEL_ENV: _LABEL,
            AGENT_ROLE_ENV: explicit_role,
        },
        clear=True,
    ):
        assert _compute_session_role(_LABEL) == explicit_role


def test_managed_session_can_disable_label_autobind() -> None:
    with patch.dict(
        os.environ,
        {
            AGENT_SESSION_LABEL_ENV: _LABEL,
            AGENT_ROLE_AUTOBIND_ENV: "0",
        },
        clear=True,
    ):
        assert _compute_session_role(_LABEL) == ""


def test_managed_autobind_opt_out_conflicts_with_explicit_role() -> None:
    with patch.dict(
        os.environ,
        {
            AGENT_SESSION_LABEL_ENV: _LABEL,
            AGENT_ROLE_ENV: "Release-Coordinator",
            AGENT_ROLE_AUTOBIND_ENV: "0",
        },
        clear=True,
    ):
        try:
            _compute_session_role(_LABEL)
        except RuntimeError as exc:
            assert "conflicts" in str(exc)
        else:
            raise AssertionError("managed no-autobind plus explicit role must fail loud")


def test_inferred_session_label_does_not_claim_a_role() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert _compute_session_role("claude_code on example") == ""


def _reassert_with_role_held_verdict(verdict: object) -> list[str]:
    """Drive one steady-state re-assert; return the POST paths it issued.

    ``verdict`` is spliced into the ``peer/register`` response as
    ``session_role_held``; :data:`_ABSENT` omits the field entirely (a server
    predating it).
    """
    fw = _make_forwarder(session_role=_LABEL)
    paths: list[str] = []

    async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        paths.append(path)
        if path.endswith("/peer/register"):
            assert body["session_role"] == _LABEL, (
                "register must declare the configured role, or the server "
                "cannot answer session_role_held"
            )
            payload: dict[str, Any] = {
                "agent_session_id": "ases-smoke",
                "session_label": _LABEL,
            }
            if verdict is not _ABSENT:
                payload["session_role_held"] = verdict
            return payload
        if path.endswith("/peer/claim_role"):
            return {
                "action": "claimed",
                "name": _LABEL,
                "agent_instance_id": "agi-smoke",
                "agent_session_id": "ases-smoke",
            }
        raise AssertionError(f"unexpected POST path: {path}")

    with patch.object(fw, "_post", _post):
        asyncio.run(fw._reassert_identity())  # noqa: SLF001
    return paths


def test_steady_state_reassert_skips_reclaim_when_role_still_held() -> None:
    # R7 - the defect this guards: the re-assert claimed unconditionally every
    # ~176s forever, through /process/call, which route_activity classifies
    # MODEL_INITIATED -- so the forwarder stamped last_model_activity_at with NO
    # model turn on a loop, which can mark an owed IMPORTANT wake to an IDLE
    # session consumed (the exact F1 class peer/register was kept INFRA to
    # avoid), and pushed a bridge_delivery_result at the model every tick.
    # Two defences now: the claim is CONDITIONAL (here), and when it does fire it
    # travels the INFRA peer/claim_role route (R8/R9) so it cannot stamp at all.
    # This test owns the first. When the server confirms the role is still held
    # there is nothing to recover, so NO claim may be issued.
    paths = _reassert_with_role_held_verdict("held")
    assert paths == [f"/api/v1/bridge/{_BRIDGE_ID}/peer/register"], paths


def test_steady_state_reassert_still_reclaims_when_role_not_held() -> None:
    # R8 - the other direction, and the reason the claim cannot simply be
    # deleted: peer/register's self-refresh only re-points roles the session
    # ALREADY holds, so a binding lost platform-side is restored by nothing but
    # an explicit claim. The skip must never suppress that recovery.
    paths = _reassert_with_role_held_verdict("not_held")
    assert paths == [
        f"/api/v1/bridge/{_BRIDGE_ID}/peer/register",
        f"/api/v1/bridge/{_BRIDGE_ID}/peer/claim_role",
    ], paths


def test_absent_role_held_verdict_claims_exactly_as_before() -> None:
    # R9 - version skew: a forwarder talking to a server that does not send
    # session_role_held must fall back to the unconditional pre-fix behavior,
    # never to "skip" (which would silently disable role recovery).
    paths = _reassert_with_role_held_verdict(_ABSENT)
    assert paths == [
        f"/api/v1/bridge/{_BRIDGE_ID}/peer/register",
        f"/api/v1/bridge/{_BRIDGE_ID}/peer/claim_role",
    ], paths


def main() -> int:
    tests = [
        test_transient_failure_then_success_registers,
        test_attempt_budget_is_bounded_and_honoured,
        test_terminal_failure_is_loud_about_the_half_alive_state,
        test_happy_path_costs_exactly_one_post,
        test_no_bridge_id_short_circuits_without_posting,
        test_steady_state_reassert_posts_and_is_silent_when_healthy,
        test_steady_state_reassert_failure_is_logged_not_raised,
        test_dense_event_burst_does_not_accelerate_reassert,
        test_elapsed_deadline_reasserts_once,
        test_steady_state_reassert_skips_reclaim_when_role_still_held,
        test_steady_state_reassert_still_reclaims_when_role_not_held,
        test_absent_role_held_verdict_claims_exactly_as_before,
        test_reconnect_claims_configured_role_from_registered_identity,
        test_failed_role_claim_is_loud_without_breaking_bridge_registration,
        test_explicit_session_label_defaults_to_standing_role,
        test_explicit_role_can_differ_from_session_label,
        test_managed_session_can_disable_label_autobind,
        test_managed_autobind_opt_out_conflicts_with_explicit_role,
        test_inferred_session_label_does_not_claim_a_role,
    ]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"register_identity_retry_smoke: {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
