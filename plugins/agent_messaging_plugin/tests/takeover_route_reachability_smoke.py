#!/usr/bin/env python3
"""The takeover escape hatch, driven through the routes the refusal advertises.

WHY THIS FILE EXISTS, and it is not "more coverage of takeover".
``peer_claim_role_route_smoke`` already proves the CAPABILITY — but it proves it by calling
``role_claim.claim_role_for_session(takeover=True)`` directly, and says so in its own
docstring: *"Deliberately not routed through /peer/claim_role"*. That was a reasonable
scoping decision for THAT file. The consequence is that nothing anywhere proved the ROUTE.

Meanwhile ``role_claim.py:490`` ships this advice to every blocked claimant:

    "Re-run with an explicit takeover after confirming with the operator"

The original red measured a process and watcher surface with no ``takeover`` input. The
fixture remains registered because an error message's recovery advice is a product claim,
not merely prose: every claim leg below follows the caller-reachable route it advertises.

★ THE REUSABLE LESSON (Architect's wording): AN ERROR MESSAGE'S REMEDIATION ADVICE IS A
CLAIM ABOUT USER-REACHABLE SURFACES. It needs a fixture that follows the advertised route
VERBATIM. This is the SCOPE costume of the four-ways-a-green-lies: the assertion was true,
about the wrong surface. Same family as route-is-part-of-the-claim and
tested-copy != shipped-copy, now in error-prose form.

So EVERY leg below goes through a caller-reachable surface. If a future edit makes one of
these call ``claim_role_for_session`` directly to "simplify", it silently re-opens the exact
hole this file exists to close.

Run:
    SOLET_NAME=examplehom .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/takeover_route_reachability_smoke.py
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuses the sibling smoke's harness (live bridge + registry + real-shape state, with
# handover notices recorded at the send seam). Deliberate: a second copy of that setup is
# a second thing to drift. Its tests do not run on import — they sit behind main().
from peer_claim_role_route_smoke import _ROLE, _Harness  # noqa: E402

from agent_messaging_plugin import role_claim as role_claim_module  # noqa: E402
from agent_messaging_plugin.local_cli import cli as cli_module  # noqa: E402
from agent_messaging_plugin.local_cli.client import (  # noqa: E402
    BridgeCallError,
    BridgeClient,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

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


def _claim_via_route(h: _Harness, bridge_id: str, *, takeover: bool | None = None) -> Any:
    """POST /peer/claim_role exactly as the watcher's arm-time claim does.

    ``takeover`` is sent as a body field because that is what a caller following the
    refusal's advice would reach for. Until the plumb lands the field is simply ignored,
    which is the defect: the caller cannot tell "not supported" from "did not work".
    """
    body: dict[str, Any] = {"name": _ROLE}
    if takeover is not None:
        body["takeover"] = takeover
    return h.client.post(f"/api/v1/bridge/{bridge_id}/peer/claim_role", json=body)


def _outcome(resp: Any) -> dict[str, Any]:
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# LEG 1 — the PRECONDITION, first. Day's near-miss guard.
# ---------------------------------------------------------------------------


def test_contention_is_real_before_anything_is_claimed_about_takeover() -> None:
    """★ POSITIVE LEG FIRST — prove the setup produces a GENUINE live-holder refusal.

    Day's ``missing_session_id`` near-miss is the reason this runs before the pair below:
    two legs that fail IDENTICALLY for the WRONG reason look exactly like a working
    control. If this leg does not report ``role_held_live`` specifically, the takeover
    comparison underneath it is meaningless and must not be read as evidence.
    """
    h = _Harness()
    try:
        first = h.session(agi="agi-holder", session_id="sess-holder", label="Holder")
        h.claim(first)
        second = h.session(agi="agi-claimant", session_id="sess-claimant", label="Claimant")
        out = _outcome(_claim_via_route(h, second))
        _check(
            out.get("code") == "role_held_live",
            f"LEG1 precondition: a live holder refuses a plain claim with role_held_live "
            f"(got {out.get('code')!r})",
        )
    finally:
        h.close()


# ---------------------------------------------------------------------------
# LEG 2 + 3 — the advertised routes. RED until the plumb lands.
# ---------------------------------------------------------------------------


def test_takeover_via_the_bridge_route_succeeds() -> None:
    """The route half of Day's control pair, including the audit warning.

    Two claims identical but for ``takeover``. MUTATION (post-fix): drop the
    plumb-through at the route's call site — the two legs read identical again, which is
    today's defect exactly.
    """
    h = _Harness()
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    logger = logging.getLogger(role_claim_module.__name__)
    logger.addHandler(handler)
    try:
        first = h.session(agi="agi-holder", session_id="sess-holder", label="Holder")
        h.claim(first)
        second = h.session(agi="agi-claimant", session_id="sess-claimant", label="Claimant")
        out = _outcome(_claim_via_route(h, second, takeover=True))
        _check(
            out.get("action") == "displaced",
            f"LEG2 (b) an explicit takeover THROUGH THE ROUTE displaces the holder "
            f"(got action={out.get('action')!r} code={out.get('code')!r})",
        )
        warning = log_stream.getvalue()
        _check(
            "operator-confirmed role takeover" in warning
            and "agi-holder" in warning
            and "agi-claimant" in warning,
            f"LEG2 binding-3: takeover logs both displaced and claiming identities "
            f"(log={warning!r})",
        )
    finally:
        logger.removeHandler(handler)
        h.close()


class _ProcessPlugin(AgentMessagingPlugin):
    """Minimal decorated-process owner over the route harness's real state."""

    def __init__(self, h: _Harness) -> None:
        self.name = "agent_messaging_plugin"
        self._state = h.state
        self._bridge_manager = h.manager
        self._peer_registry = h.registry

    def _get_state_service(self) -> Any:
        return self._state

    def _handover_service(self) -> object:
        return object()


def test_takeover_via_the_declared_platform_process_succeeds() -> None:
    """Binding 1: the process schema AND its implementation plumb takeover.

    Mutation: replace plugin.py's ``_coerce_takeover(...)`` argument with False.
    A direct shared-helper test stays green under that defect; this process-level
    control turns red, which is why it exists alongside the bridge-route pair.
    """
    metadata = AgentMessagingPlugin.peer_claim_role._platform_process_metadata  # noqa: SLF001
    declared = metadata.parameters.get("takeover")
    h = _Harness()
    try:
        first = h.session(agi="agi-holder", session_id="sess-holder", label="Holder")
        h.claim(first)
        h.session(
            agi="agi-process-claimant",
            session_id="sess-process-claimant",
            label="Process Claimant",
        )
        result = _ProcessPlugin(h).peer_claim_role(
            {
                "name": _ROLE,
                "agent_id": "claude_code",
                "agent_instance_id": "agi-process-claimant",
                "agent_session_id": "sess-process-claimant",
                "session_label": "Process Claimant",
                "takeover": True,
            },
            {},
        )
        data = result.get("data")
        _check(
            declared is not None
            and not declared.required
            and result.get("action_status") == "completed"
            and isinstance(data, dict)
            and data.get("action") == "displaced",
            f"LEG2c binding-1: declared process takeover displaces the live holder "
            f"(declared={declared!r}, result={result})",
        )
    finally:
        h.close()


def test_takeover_with_no_live_holder_is_a_silent_no_op() -> None:
    """Binding 4: ``takeover`` against a VACANT role is a normal claim, never an error.

    Without this, callers must pre-check liveness and then race their own answer.
    """
    h = _Harness()
    try:
        only = h.session(agi="agi-solo", session_id="sess-solo", label="Solo")
        out = _outcome(_claim_via_route(h, only, takeover=True))
        _check(
            out.get("action") in ("registered", "claimed", "updated"),
            f"LEG3 binding-4: takeover with no live holder is an ordinary claim "
            f"(got action={out.get('action')!r} code={out.get('code')!r})",
        )
    finally:
        h.close()


def test_takeover_fires_the_handover_notice() -> None:
    """Binding 3: displacement stays VISIBLE on the takeover path.

    The notice machinery already runs on the ordinary displacement path; this pins that
    taking a role deliberately does not become the quiet way to steal one.
    """
    h = _Harness()
    try:
        first = h.session(agi="agi-holder", session_id="sess-holder", label="Holder")
        h.claim(first)
        second = h.session(agi="agi-claimant", session_id="sess-claimant", label="Claimant")
        before = len(h.notices)
        _claim_via_route(h, second, takeover=True)
        fired = [n for n in h.notices[before:] if n.get("agi") == "agi-holder"]
        _check(
            fired,
            f"LEG4 binding-3: the DISPLACED holder gets a handover notice "
            f"(notices since claim: {h.notices[before:]})",
        )
    finally:
        h.close()


def test_refused_reclaim_does_not_revert_the_takeover() -> None:
    """Binding 5(i): the gate now protects the NEW holder.

    After a takeover the displaced session's next re-claim must hit ``role_held_live`` and
    must NOT hand the role back — otherwise a reconnect silently undoes an
    operator-confirmed handover.
    """
    h = _Harness()
    try:
        first = h.session(agi="agi-holder", session_id="sess-holder", label="Holder")
        h.claim(first)
        second = h.session(agi="agi-claimant", session_id="sess-claimant", label="Claimant")
        _claim_via_route(h, second, takeover=True)
        back = _outcome(_claim_via_route(h, first))
        _check(
            back.get("code") == "role_held_live",
            f"LEG5 binding-5(i): the displaced holder's re-claim is REFUSED, not honoured "
            f"(got action={back.get('action')!r} code={back.get('code')!r})",
        )
    finally:
        h.close()


def test_takeover_is_one_shot_and_does_not_persist_into_reclaims() -> None:
    """Binding 2: the flag authorizes taking the role NOW, once.

    If ``takeover`` persisted into reconnect/heartbeat re-claims, a reverted contention
    would replay the theft on every reconnect. Driven through the route twice: the second,
    plain claim from the ORIGINAL holder must still be refused (proving the claimant's
    takeover did not become a standing authorization for anyone), and a repeat plain claim
    from the claimant must not re-displace anybody.
    """
    h = _Harness()
    try:
        first = h.session(agi="agi-holder", session_id="sess-holder", label="Holder")
        h.claim(first)
        second = h.session(agi="agi-claimant", session_id="sess-claimant", label="Claimant")
        _claim_via_route(h, second, takeover=True)
        before = len(h.notices)
        again = _outcome(_claim_via_route(h, second))
        displaced_again = [n for n in h.notices[before:] if n.get("kind")]
        # ⚠ NON-VACUITY GUARD. The first draft asserted only `action != "displaced"`, which
        # is TRIVIALLY TRUE while the claim is being REFUSED — so it passed today, before
        # the feature exists, for entirely the wrong reason. Requiring the follow-up claim
        # to have actually SUCCEEDED is what makes this leg track the fix instead of
        # rewarding its absence.
        succeeded = again.get("action") in ("registered", "claimed", "updated")
        _check(
            succeeded and again.get("action") != "displaced" and not displaced_again,
            f"LEG6 binding-2: a follow-up PLAIN claim by the new holder SUCCEEDS and "
            f"displaces nobody (action={again.get('action')!r} code={again.get('code')!r}, "
            f"notices={h.notices[before:]})",
        )
    finally:
        h.close()


def _watch_client_for(h: _Harness, bridge_id: str) -> BridgeClient:
    """Real local CLI client, backed by the smoke's FastAPI route in-process."""

    def route(request: httpx.Request) -> httpx.Response:
        response = h.client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={"content-type": request.headers.get("content-type", "")},
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": response.headers.get("content-type", "")},
        )

    client = BridgeClient(
        "http://testserver", transport=httpx.MockTransport(route),
    )
    client._bridge_id = bridge_id  # noqa: SLF001 -- attach to the already-open watcher bridge
    return client


def test_displaced_watcher_exits_loud_on_its_next_plain_reclaim() -> None:
    """Binding 5(ii): pin the WATCHER PROCESS fate, not only the route result.

    The real BridgeClient preserves ``role_held_live`` and the real watcher arm
    classifies it as permanent. ``_die`` writes the refusal to stderr before
    raising SystemExit; SystemExit is outside the reconnect loop's catch tuple,
    so the displaced process exits instead of silently re-stealing the role.
    """
    h = _Harness()
    watcher: BridgeClient | None = None
    try:
        first = h.session(agi="agi-holder", session_id="sess-holder", label="Holder")
        h.claim(first)
        second = h.session(
            agi="agi-claimant", session_id="sess-claimant", label="Claimant",
        )
        _claim_via_route(h, second, takeover=True)
        watcher = _watch_client_for(h, first)
        identity = cli_module.WatchIdentity(
            role=_ROLE,
            agent_id="claude_code",
            agent_session_id="sess-holder",
            agent_instance_id="agi-holder",
        )
        stderr = io.StringIO()
        exit_code: object = None
        with contextlib.redirect_stderr(stderr):
            try:
                cli_module._register_and_claim(watcher, identity)  # noqa: SLF001
            except SystemExit as exc:
                exit_code = exc.code
        loud = stderr.getvalue()
        still_held = _outcome(_claim_via_route(h, second))
        _check(
            exit_code is not None
            and "role_held_live" in loud
            and "was refused" in loud
            and still_held.get("action") == "updated",
            f"LEG7 binding-5(ii): displaced watcher exits LOUD and new holder remains "
            f"(exit={exit_code!r}, stderr={loud!r}, holder={still_held})",
        )
    finally:
        if watcher is not None:
            watcher._bridge_id = None  # noqa: SLF001 -- do not close the harness bridge
            watcher.close()
        h.close()


def test_reconnect_exception_cannot_replay_the_takeover_flag() -> None:
    """Binding 2 at the actual reconnect loop: first arm true, next arm false.

    The first stream raises exactly as a blue-green bridge drop does. Clearing
    takeover after ``_arm_and_stream`` would never execute on this path and the
    second arm would steal again; consuming before the attempt makes it false.
    """
    seen: list[bool] = []

    class _Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return

    def arm(*_args: object, takeover: bool = False, **_kwargs: object) -> None:
        seen.append(takeover)
        if len(seen) == 1:
            raise BridgeCallError("simulated bridge rotation")
        raise SystemExit(0)

    identity = cli_module.WatchIdentity(
        role=_ROLE,
        agent_id="claude_code",
        agent_session_id="sess-claimant",
        agent_instance_id="agi-claimant",
    )
    with (
        patch.object(cli_module, "resolve_base_url", return_value="http://test"),
        patch.object(cli_module, "BridgeClient", _Client),
        patch.object(cli_module, "_arm_and_stream", arm),
        patch.object(cli_module.time, "sleep", return_value=None),
        contextlib.suppress(SystemExit),
    ):
        cli_module._watch_forever(  # noqa: SLF001
            identity, None, Path("/tmp/a2-unused-marks"), takeover=True,
        )
    _check(
        seen == [True, False],
        f"LEG8 binding-2: reconnect consumes takeover exactly once (seen={seen})",
    )


def test_watch_command_forwards_the_operator_takeover_flag() -> None:
    """Binding 2 starts at the shipped Click command, not ``_watch_forever``.

    The reconnect-loop test above can stay green while the top-level ``watch``
    command drops Click's parsed flag and calls ``_watch_forever`` with its
    default ``False``. Drive the advertised command and pin that first handoff.
    """
    seen: list[bool] = []

    def run_watch(
        _identity: cli_module.WatchIdentity,
        _spool: Path | None,
        _marks: Path,
        _exit_with_parent: int | None = None,
        takeover: bool = False,
    ) -> None:
        seen.append(takeover)
        raise KeyboardInterrupt

    with (
        patch.object(cli_module, "resolve_solet_name", return_value="examplehom"),
        patch.object(cli_module, "_acquire_watch_singleton", return_value=None),
        patch.object(cli_module, "_install_sigterm_unwind", return_value=None),
        patch.object(cli_module, "write_watch_pairing", return_value=None),
        patch.object(cli_module, "_watch_forever", run_watch),
    ):
        result = CliRunner().invoke(
            cli_module.cli,
            ["watch", "--no-spool", "--takeover"],
            env={
                "AGENT_SESSION_ID": "ases-command-takeover",
                "AGENT_SESSION_LABEL": _ROLE,
            },
            obj={},
        )
    _check(
        result.exit_code == 0 and seen == [True],
        f"LEG9 binding-2: shipped watch command forwards takeover=True exactly once "
        f"(exit={result.exit_code}, seen={seen}, output={result.output!r})",
    )


def main() -> None:
    print("takeover escape hatch — driven through the ADVERTISED routes only\n")
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            print(f"{name}")
            obj()
            print()
    print(f"{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
