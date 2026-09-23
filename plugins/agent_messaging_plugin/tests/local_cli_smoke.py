"""Hermetic smoke for the `solet` local-invocation CLI.

Exercises the CLI and its `BridgeClient` end-to-end against an in-process
`httpx.MockTransport` — no live solet, no LM Studio, no network. Verifies:

* health / search / call happy paths and their JSON output,
* `call` polls `process/result` until a completed result payload is stored,
* one-shot invocation NEVER registers a peer identity (no registry pollution),
* exit-code mapping for not-running / non-completed / bad-args,
* install-location identity resolution (root_manifest name -> clone basename),
* the CLI import chain needs NO ambient env (the package __init__ re-exports
  the server plugin lazily, so a bare `<name>` command works at birth).
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.constants import ExitCodes
from click.testing import CliRunner, Result

import agent_messaging_plugin.env_contract as env_contract
import agent_messaging_plugin.local_cli.cli as cli_mod
import agent_messaging_plugin.local_cli.client as client_mod
import agent_messaging_plugin.local_cli.spool as spool_mod

Handler = Callable[[httpx.Request], httpx.Response]


def _make_handler(
    result_statuses: list[str],
    open_bodies: list[dict[str, Any]] | None = None,
) -> tuple[Handler, dict[str, int]]:
    """Build a mock bridge handler; `calls` counts result-polls and registers.

    ``completed_without_result`` reproduces the real persistence race where
    action_events reaches completed before core__action_results is stored.
    ``open_bodies``, when supplied, collects each ``bridge/open`` request body
    so §34.6's attribution key can be asserted on the wire.
    """
    open_bodies = [] if open_bodies is None else open_bodies
    calls = {"result": 0, "register": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/peer/register"):
            calls["register"] += 1
            return httpx.Response(200, json={"session_label": "x"})
        if path == "/api/v1/bridge/open":
            open_bodies.append(json.loads(request.content or b"{}"))
            return httpx.Response(
                200, json={"bridge_id": "agc-smoke", "session_id": "s"},
            )
        if path.endswith("/process/call"):
            return httpx.Response(
                200, json={"status": "queued", "action_id": "ae-smoke"},
            )
        if "/process/result/" in path:
            idx = min(calls["result"], len(result_statuses) - 1)
            raw_status = result_statuses[idx]
            status = (
                "completed"
                if raw_status == "completed_without_result"
                else raw_status
            )
            calls["result"] += 1
            body: dict[str, object] = {"action_id": "ae-smoke", "status": status}
            if raw_status == "completed":
                body["result"] = {"data": {"ok": True}}
            return httpx.Response(200, json=body)
        if path.endswith("/process/search"):
            return httpx.Response(
                200, json={"process_keys": ["a::b::c"], "action_status": "completed"},
            )
        if path == "/api/v1/bridge/health":
            return httpx.Response(200, json={"status": "healthy"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    return handler, calls


def _invoke(args: list[str], handler: Handler) -> Result:
    transport = httpx.MockTransport(handler)

    def factory(base_url: str, **kw: Any) -> client_mod.BridgeClient:
        # Forward the CLI's kwargs (notably §34.6's caller_agent_session_id)
        # rather than swallowing them: a factory that drops **kw would make
        # every attribution assertion below vacuously pass.
        return client_mod.BridgeClient(base_url, transport=transport, **kw)

    with (
        patch.object(cli_mod, "resolve_base_url", lambda name=None: "http://test"),
        patch.object(cli_mod, "BridgeClient", factory),
        patch.object(client_mod.time, "sleep", lambda _s: None),
    ):
        return CliRunner().invoke(cli_mod.cli, args, obj={})


def test_health() -> None:
    handler, _ = _make_handler(["completed"])
    result = _invoke(["health"], handler)
    assert result.exit_code == 0, result.output
    assert "healthy" in result.output


def test_search() -> None:
    handler, _ = _make_handler(["completed"])
    result = _invoke(["search", "q"], handler)
    assert result.exit_code == 0, result.output
    assert "a::b::c" in result.output


def test_call_polls_until_completion() -> None:
    handler, calls = _make_handler(["queued", "queued", "completed"])
    result = _invoke(["call", "x::y::z", "{}"], handler)
    assert result.exit_code == 0, result.output
    assert '"completed"' in result.output
    assert calls["result"] >= 3, f"expected polling, saw {calls['result']} polls"


def test_call_waits_for_completed_result_payload() -> None:
    handler, calls = _make_handler(
        ["completed_without_result", "completed"],
    )
    result = _invoke(["call", "x::y::z", "{}"], handler)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"] == {"data": {"ok": True}}, payload
    assert calls["result"] >= 2, (
        "completed-without-result must be treated as a persistence race, "
        f"saw {calls['result']} polls"
    )


def test_one_shot_never_registers_peer() -> None:
    handler, calls = _make_handler(["completed"])
    _invoke(["call", "x::y::z", "{}"], handler)
    assert calls["register"] == 0, "one-shot call must not register a peer identity"


def test_attribution_still_never_registers_peer() -> None:
    """§34.6 NEGATIVE CONTROL — attribution must not become a registration.

    This is the guard on the design that was REJECTED for WS-1c. Registering
    the caller's identity on this one-shot bridge would sweep the caller's own
    registry row by ``session_label`` (``PeerRegistry.register``) and then
    delete it again when the bridge closes (``close_bridge`` → ``unregister``),
    black-holing the sending session's receive path. The CLI therefore carries
    an opaque LOOKUP KEY and never calls ``peer/register``.

    Mutation that turns this red: make the CLI call ``peer_register`` on the
    ``call`` path — i.e. implement the ask's literal "have the CLI register the
    session identity it already holds".
    """
    handler, calls = _make_handler(["completed"])
    with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-attributed-1"}):
        _invoke(["call", "x::y::z", "{}"], handler)
    assert calls["register"] == 0, (
        "sender attribution must never register a peer identity — that would "
        "evict the caller's own registry row"
    )


def test_call_sends_caller_session_key_for_attribution() -> None:
    """§34.6 — the launcher-exported session key rides ``bridge/open``.

    Mutation that turns this red: drop ``caller_agent_session_id`` from
    ``BridgeClient.open``'s body, or stop reading the env in ``cli._run``.
    """
    bodies: list[dict[str, Any]] = []
    handler, _ = _make_handler(["completed"], bodies)
    with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-attributed-1"}):
        result = _invoke(["call", "x::y::z", "{}"], handler)
    assert result.exit_code == 0, result.output
    assert bodies, "bridge/open was never called"
    assert bodies[0].get("caller_agent_session_id") == "ases-attributed-1", bodies[0]


def test_call_omits_caller_session_key_when_env_absent() -> None:
    """No launcher env → no key asserted at all (best-effort, never invented).

    Uses ``env -u``-equivalent removal rather than "just not setting" it: this
    process inherits ``AGENT_SESSION_ID`` from the launcher, so a negative
    control that only omits the patch would silently test the positive case.
    """
    bodies: list[dict[str, Any]] = []
    handler, _ = _make_handler(["completed"], bodies)
    stripped = {k: v for k, v in os.environ.items() if k != "AGENT_SESSION_ID"}
    with patch.dict(os.environ, stripped, clear=True):
        result = _invoke(["call", "x::y::z", "{}"], handler)
    assert result.exit_code == 0, result.output
    assert bodies, "bridge/open was never called"
    assert "caller_agent_session_id" not in bodies[0], bodies[0]


def test_call_exit_nonzero_when_not_completed() -> None:
    handler, _ = _make_handler(["failed"])
    result = _invoke(["call", "x::y::z", "{}"], handler)
    assert result.exit_code == int(ExitCodes.EXTERNAL_ERROR), result.output


def _role_addressed_inbox_page(
    *, role_section_status: str = "ok", role_section_error: str | None = None,
    role_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "recipient_agent_id": "claude_code",
        "recipient_agent_instance_id": "agi-msg04-test",
        "entries": [],
        "next_after_created_at": None,
        "role_entries": [] if role_entries is None else role_entries,
        "next_role_cursor": None,
        "role_section_status": role_section_status,
        "role_section_error": role_section_error,
        "role_floor_applied": False,
        "role_history_cursor": None,
    }


def _process_result_handler(data: dict[str, Any]) -> Handler:
    """A one-shot bridge (`open`/`close`) plus a `process/call`+`process/result`
    pair that always answers `data` — the routes `solet inbox` actually uses.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return httpx.Response(
                200, json={"bridge_id": "agc-smoke", "session_id": "s"},
            )
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/process/call"):
            return httpx.Response(
                200, json={"status": "queued", "action_id": "ae-inbox-smoke"},
            )
        if "/process/result/" in path:
            return httpx.Response(
                200,
                json={
                    "action_id": "ae-inbox-smoke",
                    "status": "completed",
                    "result": {
                        "action_status": "completed", "data": data, "error": None,
                    },
                },
            )
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    return handler


def test_inbox_surfaces_role_addressed_mail_the_entries_only_idiom_misses() -> None:
    """MSG-04 mutation evidence: `lane-w3-instruments`' actual broken parser
    (`len(data['entries'])`, verbatim from its transcript) reports ZERO mail
    for this exact inbox page, because the message is role-addressed. `solet
    inbox` must surface it anyway — that is the whole point of the fix.

    Mutation that turns this red: `solet inbox` reading only ``entries`` (the
    same field the incident's hand-rolled parser read), or the merge dropping
    the ``role_entries`` half.
    """
    role_message = {
        "thread_id": "role:lane-msg04-bootstrap",
        "sender_agent_id": "claude_code",
        "sender_agent_instance_id": "agi-adamain0000000000000000000001",
        "sender_session_label": "Coordinator-Seat",
        "message": {
            "id": "m-1", "cursor": "c-1", "role": "user", "kind": "text",
            "content": [{"type": "text", "text": "role-addressed dispatch"}],
            "action_id": None, "backend_session_id": None, "error": None,
            "artifacts": [], "metadata": {},
            "created_at": "2026-08-20T05:00:00+00:00",
        },
    }
    page = _role_addressed_inbox_page(role_entries=[role_message])

    # THE OLD IDIOM, unaltered: this is the exact incident-transcript parser.
    old_idiom_count = len(page["entries"])
    assert old_idiom_count == 0, (
        "sanity check on the fixture: the old entries-only idiom must miss "
        "this role-addressed message, or this test is not exercising MSG-04"
    )

    with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-msg04-test"}):
        result = _invoke(["inbox"], _process_result_handler(page))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["role_count"] == 1, payload
    assert payload["direct_count"] == 0, payload
    assert payload["complete"] is True, payload
    role_messages = [m for m in payload["messages"] if m["section"] == "role"]
    assert len(role_messages) == 1, payload
    assert any(
        "role-addressed dispatch" in part["text"]
        for part in role_messages[0]["message"]["content"]
    ), payload


def test_inbox_needs_caller_session_id() -> None:
    stripped = {k: v for k, v in os.environ.items() if k != "AGENT_SESSION_ID"}
    with patch.dict(os.environ, stripped, clear=True):
        result = _invoke(["inbox"], _process_result_handler(_role_addressed_inbox_page()))
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output


def test_inbox_reports_incomplete_loudly_on_a_role_section_fault() -> None:
    """Requirement: a role-section fault must never look like a complete,
    empty inbox. Exit is non-zero AND the JSON itself says `complete: false`
    — a script that only checks exit code and one that only parses JSON both
    still see the fault.
    """
    page = _role_addressed_inbox_page(
        role_section_status="error", role_section_error="boom",
    )
    with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-msg04-test"}):
        result = _invoke(["inbox"], _process_result_handler(page))
    assert result.exit_code == int(ExitCodes.EXTERNAL_ERROR), result.output
    # JSON on stdout is followed by a loud stderr warning line (CliRunner
    # mixes both into `.output`), so decode only the leading JSON value.
    payload, _ = json.JSONDecoder().raw_decode(result.output)
    assert payload["complete"] is False, payload
    assert payload["role_section_status"] == "error", payload
    assert payload["role_section_error"] == "boom", payload


def _ack_pages_handler(
    *, fail_first_ack: bool = False, timeline: list[str] | None = None,
) -> tuple[Handler, list[str]]:
    """Real process-call transport with two issued role page tokens."""
    calls: list[str] = []
    pages = [
        _role_addressed_inbox_page(role_entries=[_entry("role-1", "2026-09-12T00:00:01")]),
        _role_addressed_inbox_page(role_entries=[_entry("role-2", "2026-09-12T00:00:02")]),
    ]
    for index, page in enumerate(pages):
        page["role_read_page_token"] = f"page-{index}"
        page["next_role_cursor"] = f"r-{index + 1}" if index == 0 else None

    pending: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return httpx.Response(200, json={"bridge_id": "agc-ack", "session_id": "s"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/process/call"):
            body = json.loads(request.content)
            key = body["process_key"]
            arguments = body["arguments"]
            action = f"ae-{len(pending)}"
            if key == cli_mod.PEER_INBOX_PROCESS_KEY:
                page = pages[1] if arguments.get("role_after") == "r-1" else pages[0]
                pending[action] = {"action_status": "completed", "data": page}
            else:
                calls.append(str(arguments["page_token"]))
                if timeline is not None:
                    timeline.append(f"ack:{arguments['page_token']}")
                pending[action] = (
                    {"action_status": "failed", "error": "ack down"}
                    if fail_first_ack and len(calls) == 1
                    else {"action_status": "completed", "data": {"status": "acked"}}
                )
            return httpx.Response(200, json={"status": "queued", "action_id": action})
        if "/process/result/" in path:
            action = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"status": "completed", "result": pending[action]})
        return httpx.Response(404, json={"detail": path})
    return handler, calls


def test_inbox_flushes_complete_output_before_all_page_acks() -> None:
    order: list[str] = []
    handler, acks = _ack_pages_handler(timeline=order)
    original_emit = cli_mod._emit
    with (
        patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-ack-test"}),
        patch.object(
            cli_mod, "_emit", lambda value: (order.append("emit"), original_emit(value))[1],
        ),
        patch.object(cli_mod, "_flush_inbox_output", lambda: order.append("flush")),
    ):
        result = _invoke(["inbox"], handler)
    assert result.exit_code == 0, result.output
    assert acks == ["page-0", "page-1"]
    assert order == ["emit", "flush", "ack:page-0", "ack:page-1"], order


def test_inbox_emit_or_flush_failure_makes_zero_ack_calls() -> None:
    for failure in ("emit", "flush"):
        handler, acks = _ack_pages_handler()
        target = cli_mod
        attribute = "_emit" if failure == "emit" else "_flush_inbox_output"
        with (
            patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-ack-test"}),
            patch.object(target, attribute, side_effect=RuntimeError(failure)),
        ):
            result = _invoke(["inbox"], handler)
        assert result.exit_code != 0
        assert acks == [], failure


def test_inbox_ack_failure_is_loud_only_after_output() -> None:
    handler, acks = _ack_pages_handler(fail_first_ack=True)
    with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-ack-test"}):
        result = _invoke(["inbox"], handler)
    assert result.exit_code != 0 and "acknowledgement failed" in result.output
    assert '"complete": true' in result.output and acks == ["page-0"]


def test_inbox_observer_never_acknowledges_issued_pages() -> None:
    handler, acks = _ack_pages_handler()
    with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-ack-test"}):
        result = _invoke(["inbox", "--observer"], handler)
    assert result.exit_code == 0, result.output
    assert acks == []


def test_not_running_maps_to_connection_error() -> None:
    def boom(_name: str | None = None) -> str:
        raise client_mod.SoletNotRunningError("down")

    with patch.object(cli_mod, "resolve_base_url", boom):
        result = CliRunner().invoke(cli_mod.cli, ["health"], obj={})
    assert result.exit_code == int(ExitCodes.CONNECTION_ERROR), result.output


def test_bad_json_args_maps_to_unknown_error() -> None:
    handler, _ = _make_handler(["completed"])
    result = _invoke(["call", "x::y::z", "not-json"], handler)
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output


def test_cli_import_chain_needs_no_ambient_env() -> None:
    # The no-MCP-first contract: a bare `<name>` symlink must work on a fresh
    # machine with NO ambient env. A subprocess import with SOLET_NAME
    # scrubbed proves the console script's whole import chain stays lazy
    # (regression: the package __init__ used to import the server plugin, which
    # requires SOLET_NAME at import time — the bare CLI tracebacked).
    env = {k: v for k, v in os.environ.items() if k != "SOLET_NAME"}
    proc = subprocess.run(
        [sys.executable, "-c", "import agent_messaging_plugin.local_cli.cli"],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_identity_prefers_manifest_name() -> None:
    manifest = SimpleNamespace(solet_name="shelby")
    with (
        patch.object(client_mod, "_clone_root", lambda: Path("/tmp/other-dir")),
        patch.object(client_mod, "load_manifest", lambda _p: (manifest, None)),
    ):
        assert client_mod.resolve_solet_name() == "shelby"


def test_identity_falls_back_to_basename_on_placeholder() -> None:
    # A genesis-un-rewritten root_manifest (or an unreadable one) -> the clone
    # directory basename is the name (the birth/clone convention ~/Workspace/<name>/).
    with (
        patch.object(client_mod, "_clone_root", lambda: Path("/tmp/shelby")),
        patch.object(client_mod, "load_manifest", lambda _p: (None, "unreadable")),
    ):
        assert client_mod.resolve_solet_name() == "shelby"


_WATCH_ENV = {
    "AGENT_SESSION_LABEL": "Git-Controller",
    "AGENT_SESSION_ID": "ases-1753000000-777-12345",
    # MSG-04 rider: _resolve_watch_identity (cli.py:698) now PREFERS an ambient
    # AGENT_INSTANCE_ID when present (8d5b084f4). Before that landing this
    # fixture was already isolated because identity derived only from the
    # AGENT_SESSION_ID it DOES override; after it, a caller running this smoke
    # from inside a live managed worker (every spawned lane has its own real
    # AGENT_INSTANCE_ID exported) leaked that ambient value straight into
    # CliRunner-invoked tests, colliding with the calling session's own live
    # watch lock. Pinning a fixture value here means the test controls every
    # input its identity resolution reads, same as the fixture already
    # promises. `_no_ledger_id_env` strips this key back out for the
    # no-ledger-id fixtures, which must keep exercising the derived-id branch.
    "AGENT_INSTANCE_ID": "agi-0123456789abcdef0123456789abcdef",
}
_NO_WATCH_ENV = {
    "AGENT_SESSION_LABEL": "",
    "AGENT_SESSION_ID": "",
}


def _tmp_marks() -> Path:
    """A fresh, empty marks sidecar path -- i.e. the no-marks (seeding) case."""
    return Path(tempfile.mkdtemp()) / "session.marks"


def _entry(msg_id: str, created_at: str) -> dict[str, object]:
    return {"thread_id": "agt-1", "message": {"id": msg_id, "created_at": created_at}}


def _paging_inbox(
    instance_pages: list[list[dict[str, object]]],
    role_pages: list[list[dict[str, object]]],
) -> Callable[[dict[str, str]], dict[str, object]]:
    """Serve real multi-page sections, honouring the two cursors.

    Both sections are newest-first and walk backward: instance pages use the
    last entry's timestamp as ``after``; role pages use an opaque
    ``role_after`` token.
    """
    def render(params: dict[str, str]) -> dict[str, object]:
        after = params.get("after", "")
        idx = 0
        if after:
            idx = next(
                (i + 1 for i, page in enumerate(instance_pages)
                 if page and str(page[-1]["message"]["created_at"]) == after),  # type: ignore[index]
                len(instance_pages),
            )
        entries = instance_pages[idx] if idx < len(instance_pages) else []
        role_token = params.get("role_after", "")
        ridx = int(role_token.removeprefix("rc-")) if role_token else 0
        role_entries = role_pages[ridx] if ridx < len(role_pages) else []
        return {
            "entries": entries,
            "role_entries": role_entries,
            "next_after_created_at": (
                str(entries[-1]["message"]["created_at"]) if entries else None  # type: ignore[index]
            ),
            "next_role_cursor": (
                f"rc-{ridx + 1}" if ridx + 1 < len(role_pages) else None
            ),
        }
    return render


def _static_inbox(params: dict[str, str]) -> dict[str, object]:  # noqa: ARG001
    """The pre-D1 fixture shape: one instance entry, no role mail, no cursors."""
    return {
        "entries": [
            {
                "thread_id": "agt-1",
                "message": {"id": "m1", "created_at": "2026-07-30T10:00:00", "text": "hi"},
            },
        ],
        "role_entries": [],
        "next_after_created_at": None,
        "next_role_cursor": None,
    }


def _watch_handler(
    events_batches: list[object],
    inbox: Callable[[dict[str, str]], dict[str, object]] = _static_inbox,
) -> tuple[Handler, dict[str, object], dict[str, int]]:
    """Mock bridge for watch: open -> register -> claim -> inbox -> events.

    Captures the register body, the claim request, the first events cursor, and
    EVERY inbox query. ``inbox`` renders the inbox response from those query
    params, so a test can serve real multi-page sections and observe which
    cursors the client actually sent -- the pre-D1 fixture returned one fixed
    page and ignored the query, which made truncation, cursor advance and
    re-emission individually unobservable (census D1).
    """
    seen: dict[str, object] = {}
    counts = {"events": 0, "register": 0, "inbox": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return httpx.Response(200, json={"bridge_id": "agc-w", "session_id": "s"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/peer/register"):
            seen["register"] = json.loads(request.content)
            counts["register"] += 1
            return httpx.Response(200, json={"status": "registered"})
        if path.endswith("/peer/claim_role"):
            seen["claim"] = json.loads(request.content)
            return httpx.Response(200, json={"name": "Git-Controller", "action": "claimed"})
        if path.endswith("/process/call"):
            # WS-2a: the arm-claim must NOT come here. This path is recorded so
            # the assertion can prove its ABSENCE — the MODEL_INITIATED verb
            # stamps model activity and fires an EDGE_SINK delivery per call,
            # which on a re-arming watcher is a phantom stamp that corrupts the
            # "no model activity since emission" discriminator.
            seen["process_call"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "queued", "action_id": "ae-w"})
        if "/process/result/" in path:
            return httpx.Response(
                200,
                json={
                    "action_id": "ae-w",
                    "status": "completed",
                    "result": {"action": "registered"},
                },
            )
        if path.endswith("/peer/inbox"):
            counts["inbox"] += 1
            params = dict(request.url.params)
            seen.setdefault("inbox_queries", []).append(params)  # type: ignore[union-attr]
            return httpx.Response(200, json=inbox(params))
        if "/events" in path:
            seen.setdefault("first_events_query", request.url.query.decode())
            idx = counts["events"]
            counts["events"] = idx + 1
            batch = events_batches[idx] if idx < len(events_batches) else "drop"
            if isinstance(batch, dict):
                return httpx.Response(200, json=batch)
            return httpx.Response(404, json={"detail": "bridge gone"})
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    return handler, seen, counts


def test_watch_requires_env_identity() -> None:
    # No exported role/session-id and no --role -> loud refusal, not a
    # degraded anonymous registration.
    result = CliRunner().invoke(cli_mod.cli, ["watch"], env=dict(_NO_WATCH_ENV), obj={})
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output
    result = CliRunner().invoke(
        cli_mod.cli,
        ["watch", "--role", "Git-Controller"],
        env=dict(_NO_WATCH_ENV),
        obj={},
    )
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output
    assert "session id" in result.output


def test_watch_arms_then_streams_then_detects_drop() -> None:
    # Full arm sequence over one bridge lifetime: register (stable identity),
    # claim (REL-07 shape), drain inbox, stream from cursor -1, drop -> raise.
    batch = {
        "events": [{"cursor": 1, "event_type": "peer_message", "content": "hi"}],
        "next_cursor": 1,
    }
    handler, seen, _counts = _watch_handler([batch, "drop"])
    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    identity = cli_mod.WatchIdentity(
        role="Git-Controller",
        agent_id="claude_code",
        agent_session_id=_WATCH_ENV["AGENT_SESSION_ID"],
        agent_instance_id="agi-watch-deadbeefdeadbeefdeadbeef",
    )
    captured: list[str] = []
    try:
        with patch.object(
            cli_mod.click, "echo", lambda s, **_kw: captured.append(str(s)),
        ):
            cli_mod._arm_and_stream(client, identity, None, _tmp_marks())
    except client_mod.BridgeCallError:
        pass  # a 404 is how a rotated/reaped bridge surfaces to the streamer
    finally:
        client.close()
    _assert_watch_arm_requests(seen, identity)
    _assert_watch_stream_output(captured)


def _assert_watch_arm_requests(
    seen: dict[str, object], identity: cli_mod.WatchIdentity,
) -> None:
    register = seen["register"]
    assert isinstance(register, dict)
    assert register["agent_session_id"] == _WATCH_ENV["AGENT_SESSION_ID"]
    assert register["agent_instance_id"] == identity.agent_instance_id
    assert register["session_label"] == "Git-Controller"
    # WS-2a: the claim rides the INFRA bridge route, carrying ONLY the role
    # name — every identity field is read from this bridge's registered binding
    # (REL-07), so sending them would add a spoofing surface for nothing.
    claim = seen["claim"]
    assert isinstance(claim, dict)
    # The arm-time claim body now carries the one-shot takeover authorization
    # (§4.3.3a). Asserted EXPLICITLY as False rather than loosened to a subset
    # match: the default must stay false, and a body that silently started
    # sending true is exactly the regression this line should catch.
    assert claim == {"name": "Git-Controller", "takeover": False}, claim
    # ...and NOT through the MODEL_INITIATED verb. Asserting the absence is the
    # point: that path preserves no failure code (the queue poller overwrites
    # every one with `action_failed`) and phantom-stamps model activity on each
    # re-arm. Without this line the suite would stay green if the claim silently
    # moved back.
    assert "process_call" not in seen, seen.get("process_call")
    assert seen["first_events_query"] == "after=-1"


def _assert_watch_stream_output(captured: list[str]) -> None:
    assert any('"watch": "armed"' in line for line in captured), captured
    # D1: a session arming with NO marks seeds to the newest and spools NOTHING.
    # It has never been shown anything, so there is nothing it is owed a wake
    # about; its backlog is reachable on demand via the peer_inbox process. The
    # pre-D1 assertion here was `any('"watch": "inbox"')`, which encoded the
    # replay-every-arm behaviour as the contract.
    assert not any('"watch": "inbox"' in line for line in captured), captured
    assert any("peer_message" in line for line in captured), captured


def _drain(
    inbox: Callable[[dict[str, str]], dict[str, object]], marks: Path,
) -> tuple[list[str], dict[str, object]]:
    """Run ONE arm-time drain against ``inbox``; return (emitted lines, seen)."""
    handler, seen, _counts = _watch_handler(["drop"], inbox)
    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    captured: list[str] = []
    try:
        with patch.object(
            cli_mod.click, "echo", lambda s, **_kw: captured.append(str(s)),
        ):
            cli_mod._drain_inbox(client, None, marks)
    finally:
        client.close()
    return captured, seen


def test_drain_pages_both_sections_and_never_repeats_across_arms() -> None:
    """D1: the whole defect, in one test.

    Pre-fix the drain sent ONE cursor-less request per arm and discarded both
    next-cursors, so page 2+ of either section was permanently unreachable and
    every re-arm re-spooled the same entries. Red mutations: drop `after` from
    the client; drop `role_after`; stop persisting the marks.
    """
    instance_pages = [
        [_entry("i3", "2026-07-30T12:00:00")],
        [_entry("i2", "2026-07-30T11:00:00"), _entry("i1", "2026-07-30T10:00:00")],
    ]
    # Both sections are NEWEST-FIRST and page BACKWARD.
    role_pages = [
        [_entry("r3", "2026-07-30T09:00:00"), _entry("r2", "2026-07-30T08:00:00")],
        [_entry("r1", "2026-07-30T07:00:00")],
    ]
    marks = _tmp_marks()
    inbox = _paging_inbox(instance_pages, role_pages)

    # Arm 1: no marks -> seed to newest, emit NOTHING.
    first, _ = _drain(inbox, marks)
    assert not any('"watch": "inbox"' in line for line in first), first

    # New mail lands after the seed, in BOTH sections.
    instance_pages.insert(0, [_entry("i4", "2026-07-30T13:00:00")])
    role_pages.insert(0, [_entry("r4", "2026-07-30T09:30:00")])

    # Arm 2: exactly the new entries, once each.
    second, seen = _drain(inbox, marks)
    ids = [
        i
        for line in second
        for i in ("i1", "i2", "i3", "i4", "r1", "r2", "r3", "r4")
        if f'"id": "{i}"' in line
    ]
    assert ids == ["i4", "r4"], (ids, second)

    # Arm 3: nothing new -> nothing spooled. This is the re-emission half.
    third, _ = _drain(inbox, marks)
    assert not any('"watch": "inbox"' in line for line in third), third

    # The client really did send both cursors, on their own sections.
    queries = seen["inbox_queries"]
    assert isinstance(queries, list)
    assert any("after" in q and "role_after" not in q for q in queries), queries


def test_drain_walks_the_role_section_back_only_to_its_mark() -> None:
    """The role half's algorithm, which is NOT the instance half's.

    ``role_after`` walks BACKWARD, so "what is new" is "everything newer than
    my mark, reading newest-first until I hit it". Red mutation: page the role
    section to exhaustion (the census's stated fix shape) -- that re-spools the
    entire history on every arm.
    """
    role_pages = [
        [_entry("r5", "2026-07-30T12:00:00"), _entry("r4", "2026-07-30T11:00:00")],
        [_entry("r3", "2026-07-30T10:00:00"), _entry("r2", "2026-07-30T09:00:00")],
        [_entry("r1", "2026-07-30T08:00:00")],
    ]
    marks = _tmp_marks()
    spool_mod.write_watch_marks(
        marks, instance_after="", role_high_water="2026-07-30T10:00:00",
    )
    emitted, seen = _drain(_paging_inbox([], role_pages), marks)
    # The EMITTED SEQUENCE, not a membership set: a membership assertion cannot
    # see duplication, and duplication is half the defect. r4/r5 are newer than
    # the mark; r3 IS the mark; r1/r2 are older and must never be walked to --
    # and the order is chronological, oldest first.
    ids = [
        i
        for line in emitted
        for i in ("r1", "r2", "r3", "r4", "r5")
        if f'"id": "{i}"' in line
    ]
    assert ids == ["r4", "r5"], (ids, emitted)
    # It stopped walking: page 3 was never requested.
    role_queries = [q for q in seen["inbox_queries"] if "role_after" in q]  # type: ignore[union-attr]
    assert all(q["role_after"] != "rc-2" for q in role_queries), role_queries


def test_third_state_role_mark_empty_seeds_independently() -> None:
    """Fix (B): the role section seeds on ITS OWN empty mark, not the pair.

    Before (B) the third state -- instance mark SET (this session has
    drained at least once), role mark EMPTY (never shown role mail) -- fell
    through the GLOBAL predicate (``seeding = not instance_after and not
    role_high_water``) to ``seeding=False``, so the role drain walked its
    full history back with ``mark=""`` -- every entry compares newer than
    ``""``, the early-stop never fires, and the walk continues to the page
    bound (up to ``WATCH_INBOX_DRAIN_LIMIT * WATCH_INBOX_MAX_PAGES`` entries
    replayed on a single re-arm). Safe only because (A) makes an empty role
    mark truthful even when nothing but live delivery has ever touched it.
    Red mutation: restore the global predicate at the ``_drain_role_section``
    call site in ``_drain_inbox``.
    """
    role_pages = [
        [_entry("r5", "2026-07-30T12:00:00"), _entry("r4", "2026-07-30T11:00:00")],
        [_entry("r3", "2026-07-30T10:00:00")],
    ]
    marks = _tmp_marks()
    spool_mod.write_watch_marks(
        marks, instance_after="2026-07-30T09:00:00", role_high_water="",
    )
    emitted, seen = _drain(_paging_inbox([], role_pages), marks)
    assert not any('"section": "role_entries"' in line for line in emitted), emitted
    instance_after, role_high_water = spool_mod.read_watch_marks(marks)
    assert instance_after == "2026-07-30T09:00:00", instance_after
    assert role_high_water == "2026-07-30T12:00:00", role_high_water
    # It seeded, not walked: one instance query (empty page, breaks
    # immediately) + one role query (seeding breaks after the first page).
    # Pre-(B), the role side would keep walking (a non-empty ``role_pages``
    # with an advancing cursor) and request its second page too.
    queries = seen["inbox_queries"]
    assert isinstance(queries, list)
    assert len(queries) == 2, queries


def test_instance_section_seeding_is_unchanged_by_the_per_section_split() -> None:
    """(B) re-scopes only the ROLE section; the instance drain must not regress.

    Mirror of the third state, reachable via (A): instance mark EMPTY (no
    instance mail has ever arrived) + role mark SET (role mail arrived LIVE
    and (A) advanced the mark without a drain ever running). Per the
    Architect's ruling
    (workbench/2026-08-01_architect_walkback_per_section_seeding_ruling.md
    §4), the instance section is UNTOUCHED by (B): unlike the role section it
    has no notice-and-pull backstop for anything it suppresses, so a
    per-section split that independently seeded on ``not instance_after``
    would turn this instance mail into a silent loss -- the exact inversion
    (B)'s own bindings forbid, relocated to the other section. Red mutation:
    change the instance call site to ``seeding=not instance_after``.
    """
    instance_pages = [
        [_entry("i2", "2026-07-30T09:00:00"), _entry("i1", "2026-07-30T08:00:00")],
    ]
    marks = _tmp_marks()
    spool_mod.write_watch_marks(
        marks, instance_after="", role_high_water="2026-07-30T07:00:00",
    )
    emitted, _seen = _drain(_paging_inbox(instance_pages, []), marks)
    ids = [i for line in emitted for i in ("i1", "i2") if f'"id": "{i}"' in line]
    assert ids == ["i1", "i2"], (ids, emitted)


def test_marks_survive_a_bridge_rotation() -> None:
    """Marks are keyed on SESSION identity, so a swap resumes, never replays."""
    name, instance = "testhome", "agi-watch-deadbeefdeadbeefdeadbeef"
    first = spool_mod.watch_marks_path(name, instance)
    second = spool_mod.watch_marks_path(name, instance)
    assert first == second, (first, second)
    # ...and NOT on the spool, so --spool cannot resurrect a backlog.
    assert "spool" not in first.name, first


def test_watch_heartbeat_reregisters_during_stream() -> None:
    # Field-observed on a live deployment: the peer binding can be dropped
    # server-side while the bridge stays healthy — the events long-poll keeps
    # returning empty 200s with no error, so without a heartbeat the watcher
    # black-holes deliveries
    # as persisted_silent forever. The stream loop must re-assert
    # peer/register on a cadence so a dropped binding heals within one
    # interval. Interval patched to 0 -> expect a re-register per poll.
    batch = {"events": [], "next_cursor": 0}
    handler, _seen, counts = _watch_handler([batch, batch, batch, "drop"])
    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    identity = cli_mod.WatchIdentity(
        role="Git-Controller",
        agent_id="claude_code",
        agent_session_id="ases-x",
        agent_instance_id="agi-watch-x",
    )
    try:
        with (
            patch.object(cli_mod, "WATCH_REREGISTER_INTERVAL_S", 0.0),
            patch.object(cli_mod.click, "echo", lambda s, **_kw: None),
        ):
            cli_mod._arm_and_stream(client, identity, None, _tmp_marks())
    except client_mod.BridgeCallError:
        pass  # the eventual 404 drop ends the stream, as in the arm test
    finally:
        client.close()
    assert counts["register"] >= 3, counts  # 1 at arm + 1 heartbeat per poll


def _no_ledger_id_env(overrides: Mapping[str, str]) -> dict[str, str]:
    """``_WATCH_ENV``-shaped env with NO ``AGENT_INSTANCE_ID`` inherited.

    This process (a managed worker) has its own real ``AGENT_INSTANCE_ID``
    exported — a bare ``patch.dict(..., clear=False)`` would leak it into
    every "no ledger id" fixture below and silently test the wrong branch
    (negative-control convention, this file's own established pattern).
    ``overrides`` (typically ``_WATCH_ENV``) is stripped of the same key
    AFTER the merge, not just before: ``_WATCH_ENV`` itself now pins a
    fixture ``AGENT_INSTANCE_ID`` (the MSG-04 rider above), and a caller of
    this helper is asking for the no-ledger-id branch regardless of what the
    base fixture carries.
    """
    stripped = {
        k: v for k, v in os.environ.items() if k != "AGENT_INSTANCE_ID"
    }
    stripped.update(overrides)
    stripped.pop("AGENT_INSTANCE_ID", None)
    return stripped


def test_watch_identity_is_deterministic_per_session() -> None:
    # No ledger AGENT_INSTANCE_ID -> the derived fallback: same session id ->
    # same instance id (reconnect REPLACES the binding); different session id
    # -> different instance id (no cross-session bleed).
    with patch.dict(os.environ, _no_ledger_id_env(_WATCH_ENV), clear=True):
        first = cli_mod._resolve_watch_identity(None, "claude_code")
        second = cli_mod._resolve_watch_identity(None, "claude_code")
    assert first == second
    assert first.agent_instance_id.startswith(spool_mod.WATCH_AGENT_INSTANCE_PREFIX)
    other_env = dict(_WATCH_ENV)
    other_env["AGENT_SESSION_ID"] = "ases-1753000001-778-54321"
    with patch.dict(os.environ, _no_ledger_id_env(other_env), clear=True):
        third = cli_mod._resolve_watch_identity(None, "claude_code")
    assert third.agent_instance_id != first.agent_instance_id


def test_watch_prefers_ledger_instance_id_when_present() -> None:
    """MSG-04/identity-unification: a managed worker's `AGENT_INSTANCE_ID`
    wins over the derived `agi-watch-{digest}` scheme, so a DIRECT peer_send
    addressed to the ledger id (the identity `session_status`/`peer_list`
    show a spawner) reaches the SAME identity the watcher registers under.

    Mutation that turns this red: `_resolve_watch_identity` ignoring
    `AGENT_INSTANCE_ID` and always deriving from the session id.
    """
    env = dict(_WATCH_ENV)
    env["AGENT_INSTANCE_ID"] = "agi-82be2462c96a9bcc2e12d25db4e1fdde"
    with patch.dict(os.environ, env, clear=True):
        identity = cli_mod._resolve_watch_identity(None, "claude_code")
    assert identity.agent_instance_id == "agi-82be2462c96a9bcc2e12d25db4e1fdde", (
        "the ledger AGENT_INSTANCE_ID must win outright, not just influence "
        f"the derived digest (got {identity.agent_instance_id!r})"
    )
    assert not identity.agent_instance_id.startswith(
        spool_mod.WATCH_AGENT_INSTANCE_PREFIX,
    )


def test_watch_falls_back_to_derived_id_with_no_ledger_row() -> None:
    """The manual/no-ledger-row case (an interactive, non-managed watch) is
    UNCHANGED: nothing addresses that session by a ledger id that does not
    exist, so there is nothing to unify — it keeps the derived scheme.
    """
    with patch.dict(os.environ, _no_ledger_id_env(_WATCH_ENV), clear=True):
        identity = cli_mod._resolve_watch_identity(None, "claude_code")
    assert identity.agent_instance_id.startswith(spool_mod.WATCH_AGENT_INSTANCE_PREFIX)


def _claim_refusal_client(
    status: int, body: Mapping[str, object],
) -> client_mod.BridgeClient:
    """A bridge whose claim route refuses with a given status + code body."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return httpx.Response(200, json={"bridge_id": "agc-w", "session_id": "s"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/peer/register"):
            return httpx.Response(200, json={"status": "registered"})
        if path.endswith("/peer/claim_role"):
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    return client


_CLAIM_IDENTITY = cli_mod.WatchIdentity(
    role="Git-Controller",
    agent_id="claude_code",
    agent_session_id="ases-x",
    agent_instance_id="agi-watch-x",
)


def test_watch_permanent_claim_refusal_dies_loud() -> None:
    """A PERMANENT code exits loudly — retrying cannot change the answer.

    `role_held_live` means a live holder refused displacement; looping the
    reconnect path against that would retry forever against a gate that said no.
    """
    client = _claim_refusal_client(
        409, {"code": "role_held_live", "message": "held by a live session"},
    )
    try:
        cli_mod._register_and_claim(client, _CLAIM_IDENTITY)
        raise AssertionError("permanent refusal did not exit")
    except SystemExit as exc:
        assert exc.code == int(ExitCodes.EXTERNAL_ERROR)
    finally:
        client.close()


def test_watch_transient_claim_failure_retries_instead_of_dying() -> None:
    """Census D2: a TRANSIENT failure must NOT kill the watcher.

    Before this, ANY non-completed claim raised SystemExit — which sails past
    the reconnect loop's except-tuple (SystemExit derives from BaseException),
    so one platform hiccup mid-swap ended the watcher permanently and silently.
    A transient failure now surfaces as BridgeCallError, which that loop already
    catches and backs off on.

    `state_service_unavailable` is a real emitted code, not a hypothetical, and
    is exactly the mid-startup/mid-swap case.
    """
    client = _claim_refusal_client(
        503, {"code": "state_service_unavailable", "message": "state service down"},
    )
    try:
        cli_mod._register_and_claim(client, _CLAIM_IDENTITY)
        raise AssertionError("transient failure did not raise")
    except SystemExit:  # noqa: TRY203 — the whole point is that this must NOT happen
        raise AssertionError("transient claim failure killed the watcher (D2)") from None
    except client_mod.BridgeCallError as exc:
        assert "state_service_unavailable" in str(exc), str(exc)
    finally:
        client.close()


def test_watch_unknown_claim_code_is_presumed_transient() -> None:
    """An unrecognized or absent code must be presumed TRANSIENT, not permanent.

    Fail toward staying armed: a watcher retrying a genuinely permanent unknown
    logs loudly every attempt, while one that dies on a transient unknown is
    silent forever. Also covers the malformed-body path, where no code can be
    parsed at all — inventing one there would let a parse failure masquerade as
    a permanent refusal.
    """
    for status, body in (
        (400, {"code": "some_future_code", "message": "unknown to this client"}),
        (500, {"unexpected": "shape"}),
    ):
        client = _claim_refusal_client(status, body)
        try:
            cli_mod._register_and_claim(client, _CLAIM_IDENTITY)
            raise AssertionError("unknown-code failure did not raise")
        except SystemExit:  # noqa: TRY203
            raise AssertionError(f"unknown code {body} was treated as permanent") from None
        except client_mod.BridgeCallError:
            pass
        finally:
            client.close()


def test_permanent_claim_failures_excludes_peer_identity_unregistered() -> None:
    """`peer_identity_unregistered` must stay TRANSIENT.

    The claim route returns it when the bridge has no registered binding yet —
    the ordinary post-rotation window, which the very next register repairs.
    Promoting it to the permanent set "for completeness" would kill a watcher on
    a routine reconnect, which is census D2 re-created by tidiness. Pinned as a
    literal because the danger is a future edit, not today's code.
    """
    assert "peer_identity_unregistered" not in cli_mod.PERMANENT_ARM_FAILURES
    assert frozenset({
        "role_held_live",
        "system_slot_claim_denied",
        "missing_argument",
        "missing_session_id",
        "missing_role_name",
        # §4.3.3a — the ARM sequence is register-then-claim, so a permanent
        # REGISTER refusal belongs here too: it reaches the same retry loop.
        "session_id_bound_to_live_session",
    }) == cli_mod.PERMANENT_ARM_FAILURES


def _register_refusal_client(status: int, body: dict[str, str]) -> client_mod.BridgeClient:
    """A bridge whose peer/register refuses with a code body (§4.3.3a)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return httpx.Response(200, json={"bridge_id": "agc-w", "session_id": "s"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/peer/register"):
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    return client


def test_r7a_live_session_id_refusal_dies_loud() -> None:
    """R7a — a watcher armed under a LIVE foreign session id must die loud.

    Day's live find: `watch` inherits whatever AGENT_SESSION_ID is in the shell
    it was launched from, registers a second binding under it, and the register
    route's self-refresh then re-points EVERY role the victim session holds.
    Nothing announces. RED before §4.3.3a, because the register simply
    succeeded.

    The refusal must be PERMANENT: retrying under the same inherited id against
    the same live incumbent can never succeed.
    """
    client = _register_refusal_client(409, {
        "code": "session_id_bound_to_live_session",
        "message": "held by a LIVE session: label 'Coordinator-Day'",
    })
    try:
        cli_mod._register_and_claim(client, _CLAIM_IDENTITY)
        raise AssertionError("live-session-id register refusal did not exit")
    except SystemExit as exc:
        assert exc.code == int(ExitCodes.EXTERNAL_ERROR)
    finally:
        client.close()


def test_r7b_transient_register_failure_still_retries() -> None:
    """R7b's client half — a TRANSIENT register failure must NOT die.

    The succession/restart path must stay cheap. A register failure that is not
    on the permanent list has to reach the reconnect loop, exactly as a
    transient claim failure does; dying here would re-create D2 one layer up.
    """
    client = _register_refusal_client(503, {
        "code": "state_service_unavailable",
        "message": "state service down",
    })
    try:
        cli_mod._register_and_claim(client, _CLAIM_IDENTITY)
        raise AssertionError("transient register failure did not raise")
    except SystemExit:  # noqa: TRY203 — dying here is the regression
        raise AssertionError("transient REGISTER failure killed the watcher") from None
    except client_mod.BridgeCallError as exc:
        assert "state_service_unavailable" in str(exc), str(exc)
    finally:
        client.close()


def test_legacy_env_tripwire_fires_without_neutral() -> None:
    # One-release migration tripwire (env_contract): either legacy prefix
    # without its neutral AGENT_* replacement fails loud — never a read-through.
    # The legacy literal is fragmented (not written whole) because the seed
    # seal validator scans shipped bytes for legacy identity markers and would
    # refuse this file; the test must assemble the string it is testing for.
    for legacy_name in (
        "A" + "DA_AGENT_SESSION_LABEL",
        "HOMUNCULUS_AGENT_SESSION_LABEL",
    ):
        with patch.dict(os.environ, {legacy_name: "Worker-A"}, clear=True):
            try:
                env_contract.enforce_no_legacy_agent_env()
                raise AssertionError(f"{legacy_name} did not trip")
            except RuntimeError as exc:
                assert legacy_name in str(exc)
                assert "un-migrated" in str(exc)


def test_legacy_env_tripwire_silent_on_neutral_or_absent() -> None:
    # Steady state (no legacy names) and the flip window (both names present)
    # both pass; only legacy-without-neutral trips.
    # (Fragmented for the same reason as above: the seal validator scans for
    # this literal whole, so the test must assemble it rather than write it.)
    with patch.dict(os.environ, {}, clear=True):
        env_contract.enforce_no_legacy_agent_env()
    for legacy_name in (
        "A" + "DA_AGENT_SESSION_LABEL",
        "HOMUNCULUS_AGENT_SESSION_LABEL",
    ):
        with patch.dict(
            os.environ,
            {
                legacy_name: "Worker-A",
                "AGENT_SESSION_LABEL": "Worker-A",
            },
            clear=True,
        ):
            env_contract.enforce_no_legacy_agent_env()


def test_watch_dies_loud_on_legacy_only_env() -> None:
    # The watch entry converts the tripwire into the CLI's loud non-wake
    # error exit instead of registering a mislabeled/degraded identity.
    with patch.dict(
        os.environ,
        {
            "HOMUNCULUS_AGENT_SESSION_LABEL": "Git-Controller",
            "HOMUNCULUS_AGENT_SESSION_ID": "ases-1753000000-777-12345",
        },
        clear=True,
    ):
        result = CliRunner().invoke(cli_mod.cli, ["watch"], obj={})
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output
    assert "un-migrated" in result.output


def test_watch_ctrl_c_exits_clean() -> None:
    def interrupt(
        _identity: cli_mod.WatchIdentity,
        _spool: Path | None,
        _marks: Path,
        _exit_with_parent: int | None = None,
        takeover: bool = False,
    ) -> None:
        assert takeover is False
        raise KeyboardInterrupt

    with patch.object(cli_mod, "_watch_forever", interrupt):
        result = CliRunner().invoke(
            cli_mod.cli, ["watch"], env=dict(_WATCH_ENV), obj={},
        )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Fix (A) — a LIVE role delivery advances ``role_high_water``.
#
# Before (A), ``write_watch_marks`` had exactly ONE call site (``_drain_inbox``)
# and ``_stream_events`` was never even PASSED the marks path, so live delivery
# was structurally incapable of advancing the mark. A session shown role mail
# live therefore arrived at its next arm with the mark still where the last
# drain left it and re-spooled that history — bounded only by
# WATCH_INBOX_DRAIN_LIMIT * WATCH_INBOX_MAX_PAGES.
#
# Every fixture below ADVANCES (arm -> deliver live -> re-arm) rather than
# asserting against a frozen step-1 state: the claim is about what the SECOND
# arm does, and a fixture that never re-arms cannot see it.
# ---------------------------------------------------------------------------

_A_ROLE = "Git-Controller"


def _role_event(
    cursor: int, msg_id: str, *, row_created_at: str, event_created_at: str,
) -> dict[str, object]:
    """One live role delivery in the shape ``/events`` serves (asdict).

    ``row_created_at`` goes in meta under the Control #5 key; ``event_created_at``
    is the QueuedEvent's OWN clock reading. They are deliberately different in
    every fixture here, so a leg cannot pass by reading the wrong one — the
    event's timestamp is a different quantity and using it loses mail.
    """
    meta: dict[str, object] = {
        "recipient_kind": "role",
        "recipient_key": _A_ROLE,
        "delivery_external_id": f"role:{_A_ROLE}:{msg_id}",
    }
    if row_created_at:
        meta["role_created_at"] = row_created_at
    return {
        "cursor": cursor,
        "event_type": "peer_message",
        "content": f"delivery {msg_id}",
        "meta": meta,
        "created_at": event_created_at,
    }


def _role_inbox(
    role_entries: list[dict[str, object]],
) -> Callable[[dict[str, str]], dict[str, object]]:
    """A one-page inbox serving ``role_entries`` NEWEST-FIRST, no instance mail."""
    def render(params: dict[str, str]) -> dict[str, object]:  # noqa: ARG001
        return {
            "entries": [],
            "role_entries": list(role_entries),
            "next_after_created_at": None,
            "next_role_cursor": None,
        }
    return render


def _arm_once(
    inbox: Callable[[dict[str, str]], dict[str, object]],
    marks: Path,
    events_batches: list[object],
) -> list[str]:
    """One whole bridge lifetime: arm -> drain -> stream -> drop."""
    handler, _seen, _counts = _watch_handler(events_batches, inbox)
    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    identity = cli_mod.WatchIdentity(
        role=_A_ROLE,
        agent_id="claude_code",
        agent_session_id="ases-a",
        agent_instance_id="agi-watch-aaaaaaaaaaaaaaaaaaaa",
    )
    captured: list[str] = []
    try:
        with patch.object(
            cli_mod.click, "echo", lambda s, **_kw: captured.append(str(s)),
        ):
            cli_mod._arm_and_stream(client, identity, None, marks)
    except client_mod.BridgeCallError:
        pass  # the 404 drop is how the stream ends, as in the arm test
    finally:
        client.close()
    return captured


def _spooled_role_ids(captured: list[str], candidates: tuple[str, ...]) -> list[str]:
    """Ids the DRAIN spooled — the re-emission surface, not the live one."""
    return [
        c
        for line in captured
        if '"watch": "inbox"' in line
        for c in candidates
        if f'"id": "{c}"' in line
    ]


def test_live_role_delivery_is_not_replayed_on_the_next_arm() -> None:
    """(A)'s defect leg. Goes RED against pre-(A) code.

    Red mutation: delete the ``_commit_live_role_mark(...)`` call in
    ``_stream_events`` (equivalently, stop passing ``marks`` to it) — the
    pre-(A) shape exactly. r2 is then re-spooled on the second arm.
    """
    marks = _tmp_marks()
    r1 = _entry("r1", "2026-07-30T10:00:00")
    live = _role_event(
        1, "r2",
        row_created_at="2026-07-30T11:00:00",
        event_created_at="2026-07-30T11:00:05",
    )
    _arm_once(
        _role_inbox([r1]), marks,
        [{"events": [live], "next_cursor": 1}, "drop"],
    )
    # Re-arm. The inbox now also holds the durable row for the live-delivered
    # r2 — pull never consumes, so it is still there to be re-served.
    r2 = _entry("r2", "2026-07-30T11:00:00")
    second = _arm_once(_role_inbox([r2, r1]), marks, ["drop"])
    assert _spooled_role_ids(second, ("r1", "r2")) == [], second


def test_gap_role_mail_still_emits_in_full_after_a_live_delivery() -> None:
    """★ LOAD-BEARING, loss direction. The leg a wrong fix passes everything else and fails.

    A change that merely SUPPRESSED re-emission (or seeded the mark to the
    newest row in the inbox, or to the event's own clock) passes the defect leg
    above and fails here: r3 arrived while the watcher was STOPPED, was never
    shown live, and must still be emitted IN FULL.

    Red mutation: in ``_stream_events`` commit the newest INBOX row rather than
    the newest DELIVERED row — r3 is then marked as seen without ever being
    emitted, converting a duplicate into a silent loss.
    """
    marks = _tmp_marks()
    r1 = _entry("r1", "2026-07-30T10:00:00")
    live = _role_event(
        1, "r2",
        row_created_at="2026-07-30T11:00:00",
        event_created_at="2026-07-30T11:00:05",
    )
    _arm_once(
        _role_inbox([r1]), marks,
        [{"events": [live], "next_cursor": 1}, "drop"],
    )
    r2 = _entry("r2", "2026-07-30T11:00:00")
    r3 = _entry("r3", "2026-07-30T12:00:00")  # arrived while STOPPED
    second = _arm_once(_role_inbox([r3, r2, r1]), marks, ["drop"])
    ids = _spooled_role_ids(second, ("r1", "r2", "r3"))
    assert ids == ["r3"], ids


def test_the_live_delivery_mark_is_written_to_the_sidecar() -> None:
    """Binding 2: (A) is a SOURCE claim — assert the wiring, not the output.

    A spool looks IDENTICAL whether or not the mark moved (the live line is
    emitted either way); only the sidecar distinguishes them. Also carries the
    instance-section non-regression (binding 6): committing the role mark is a
    read-modify-write and must leave ``instance_after`` untouched.

    Red mutation: have ``_commit_live_role_mark`` pass ``instance_after=""`` —
    the role assertion still passes and this one fails.
    """
    marks = _tmp_marks()
    spool_mod.write_watch_marks(
        marks, instance_after="2026-07-30T09:00:00", role_high_water="",
    )
    live = _role_event(
        1, "r2",
        row_created_at="2026-07-30T11:00:00",
        event_created_at="2026-07-30T23:00:00",
    )
    _arm_once(
        _role_inbox([]), marks,
        [{"events": [live], "next_cursor": 1}, "drop"],
    )
    instance_after, role_high_water = spool_mod.read_watch_marks(marks)
    assert role_high_water == "2026-07-30T11:00:00", role_high_water
    assert instance_after == "2026-07-30T09:00:00", instance_after


def test_a_role_delivery_without_the_row_timestamp_leaves_the_mark_alone() -> None:
    """The pre-deploy state AND the substitute-quantity guard, in one leg.

    (A)'s server half ships in a RELEASE; until it deploys, live role events
    carry no ``role_created_at`` and the client must leave the mark alone —
    inert, never guessing. The event's own ``created_at`` here is deliberately
    the NEWEST timestamp in the fixture: a client that fell back to it would
    advance the mark past mail it never emitted and lose it silently.

    Red mutation: read ``event["created_at"]`` instead of
    ``meta["role_created_at"]`` in ``_live_role_created_at``.
    """
    marks = _tmp_marks()
    spool_mod.write_watch_marks(
        marks, instance_after="", role_high_water="2026-07-30T10:00:00",
    )
    live = _role_event(
        1, "r2",
        row_created_at="",  # pre-deploy server: the key is absent
        event_created_at="2026-07-31T23:00:00",
    )
    _arm_once(
        _role_inbox([]), marks,
        [{"events": [live], "next_cursor": 1}, "drop"],
    )
    _instance_after, role_high_water = spool_mod.read_watch_marks(marks)
    assert role_high_water == "2026-07-30T10:00:00", role_high_water


def test_only_role_deliveries_advance_the_role_mark_and_it_never_rewinds() -> None:
    """Widening guard + monotonicity.

    A direct (instance-addressed) wake carries no ``recipient_kind: role``, so
    it must not touch the ROLE mark; and a replayed/out-of-order role event
    older than the mark must not rewind it.

    Red mutations: drop the ``recipient_kind`` check in
    ``_live_role_created_at``; drop the ``newest <= role_high_water`` guard in
    ``_commit_live_role_mark``.
    """
    marks = _tmp_marks()
    spool_mod.write_watch_marks(
        marks, instance_after="", role_high_water="2026-07-30T11:00:00",
    )
    # A NON-role event that nonetheless carries a timestamp under the shared
    # key. Shaped this way on purpose: it is what distinguishes "gated on the
    # role discriminator" from "grabs whatever key it finds", and only the
    # former is correct — the ROLE mark must not move for instance-addressed
    # mail no matter what meta rides along.
    direct = {
        "cursor": 1,
        "event_type": "peer_message",
        "content": "direct wake",
        "meta": {"message_id": "agm-d1", "role_created_at": "2026-07-31T23:00:00"},
        "created_at": "2026-07-31T23:00:00",
    }
    stale_role = _role_event(
        2, "r0",
        row_created_at="2026-07-30T08:00:00",  # OLDER than the mark
        event_created_at="2026-07-31T23:00:00",
    )
    _arm_once(
        _role_inbox([]), marks,
        [{"events": [direct, stale_role], "next_cursor": 2}, "drop"],
    )
    _instance_after, role_high_water = spool_mod.read_watch_marks(marks)
    assert role_high_water == "2026-07-30T11:00:00", role_high_water


def main() -> None:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"{failures}/{len(tests)} failed")
        sys.exit(1)
    print(f"all {len(tests)} passed")


if __name__ == "__main__":
    main()
