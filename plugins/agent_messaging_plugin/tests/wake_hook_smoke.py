"""Hermetic smoke for the `solet wake` Stop-hook waker.

The wake command is the MCP-free turn-injection half of the watch/wake pair:
it blocks on the per-session spool `watch` tees deliveries into, surfaces new
lines on stderr, and exits 2 (the Claude Code hook wake code, valid for both
the asyncRewake background shape and the synchronous block-stop shape).
Everything here runs against temp files — no bridge, no network, no sleep.
"""

from __future__ import annotations

# ruff: noqa: E402
import fcntl
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.constants import ExitCodes
from click.testing import CliRunner, Result

import agent_messaging_plugin.local_cli.cli as cli_mod
import agent_messaging_plugin.local_cli.spool as spool_mod
import agent_messaging_plugin.local_cli.wake as wake_mod
import agent_messaging_plugin.local_cli.wake_role_receipts as receipt_mod

_FLEET_ENV = {
    "AGENT_SESSION_LABEL": "Worker-A",
    "AGENT_SESSION_ID": "ases-1753200000-101-11111",
}
_MANAGED_FLEET_ENV = {
    **_FLEET_ENV,
    "AGENT_INSTANCE_ID": "agi-2fa825900c217a86ccd05479b3667e2d",
}
_BARE_ENV = {
    "AGENT_SESSION_LABEL": "",
    "AGENT_SESSION_ID": "",
}
_MANAGED_INSTANCE_ID = _MANAGED_FLEET_ENV["AGENT_INSTANCE_ID"]


def _invoke_wake(
    spool: Path, *, max_wait: float = 0.05, env: dict[str, str] | None = None,
) -> Result:
    with patch.object(wake_mod, "resolve_solet_name", lambda: "testling"):
        return CliRunner().invoke(
            wake_mod.wake,
            ["--spool", str(spool), "--max-wait", str(max_wait)],
            env=dict(env if env is not None else _FLEET_ENV),
            obj={},
        )


def _tmp_spool() -> Path:
    return Path(tempfile.mkdtemp(prefix="wake-smoke-")) / "testling.agi.spool"


class _FakeBridgeClient:
    """Bridge boundary double: reconciliation still validates its response."""

    response: dict[str, object] = {}
    failure: Exception | None = None
    calls: list[tuple[str, dict[str, object]]] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _FakeBridgeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def call_and_wait(
        self, process: str, arguments: dict[str, object], **_kwargs: object,
    ) -> dict[str, object]:
        type(self).calls.append((process, arguments))
        if type(self).failure is not None:
            raise type(self).failure
        return type(self).response


def _receipt_response(
    results: list[dict[str, object]], *, instance_id: str = _MANAGED_INSTANCE_ID,
) -> dict[str, object]:
    return {
        "result": {
            "action_status": "completed",
            "data": {"agent_instance_id": instance_id, "results": results},
        },
    }


def _reconciliation_lines() -> list[str]:
    return [
        json.dumps({"watch": "event", "event": {"meta": {
            "recipient_kind": "role", "recipient_key": "R", "role_row_id": "served-live",
        }}}),
        json.dumps({"watch": "inbox", "section": "role_entries", "entry": {
            "message": {"metadata": {
                "recipient_kind": "role", "recipient_key": "R", "role_row_id": "served-catchup",
            }},
        }}),
        json.dumps({"watch": "event", "event": {"meta": {
            "recipient_kind": "role", "recipient_key": "R", "role_row_id": "unserved",
        }}}),
        json.dumps({"watch": "event", "event": {"content": "direct survives"}}),
        "not-json",
        json.dumps({"watch": "event", "event": {"meta": {"recipient_kind": "role"}}}),
        json.dumps({"watch": "event", "event": {"content": "new append"}}),
    ]


def _run_reconciliation_wake(
    spool: Path, response: dict[str, object], *, failure: Exception | None = None,
) -> Result:
    _FakeBridgeClient.calls = []
    _FakeBridgeClient.response = response
    _FakeBridgeClient.failure = failure
    with (
        patch.object(receipt_mod, "BridgeClient", _FakeBridgeClient),
        patch.object(receipt_mod, "resolve_base_url", lambda: "http://test"),
    ):
        return _invoke_wake(spool, env=_MANAGED_FLEET_ENV)


def test_wake_is_a_no_op_outside_fleet_sessions() -> None:
    # The Stop hook is installed at USER scope, so it fires in plain
    # unlabeled sessions too — there it must exit 0 with zero output.
    spool = _tmp_spool()
    spool.write_text('{"watch": "event"}\n', encoding="utf-8")
    result = _invoke_wake(spool, env=_BARE_ENV)
    assert result.exit_code == 0, result.output
    assert not result.output
    assert not result.stderr


def test_wake_fires_on_pending_spool_content() -> None:
    # Lines already in the spool (delivered while no waker was armed) wake
    # immediately: exit 2, content + role on stderr, offset advanced.
    spool = _tmp_spool()
    line = json.dumps({"watch": "event", "event": {"content": "ping-A"}})
    spool.write_text(line + "\n", encoding="utf-8")
    result = _invoke_wake(spool)
    assert result.exit_code == wake_mod.WAKE_EXIT_SIGNAL, result.output
    assert "ping-A" in result.stderr
    assert "Worker-A" in result.stderr
    offset = spool_mod.spool_offset_path(spool)
    assert int(offset.read_text().strip()) == len(line) + 1


def test_wake_reconciles_only_exact_served_role_rows_before_emission() -> None:
    """Actual BridgeClient reconciliation removes only exact served rows."""
    spool = _tmp_spool()
    lines = _reconciliation_lines()
    spool.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run_reconciliation_wake(spool, _receipt_response([
        {"recipient_key": "R", "role_row_id": "served-live", "served": True},
        {"recipient_key": "R", "role_row_id": "served-catchup", "served": True},
        {"recipient_key": "R", "role_row_id": "unserved", "served": False},
    ]))
    assert result.exit_code == wake_mod.WAKE_EXIT_SIGNAL, result.output
    assert "served-live" not in result.stderr and "served-catchup" not in result.stderr
    assert "unserved" in result.stderr and "direct survives" in result.stderr
    assert "not-json" in result.stderr
    assert "new append" in result.stderr
    assert _FakeBridgeClient.calls[0][0] == receipt_mod._PROCESS  # noqa: SLF001
    assert int(spool_mod.spool_offset_path(spool).read_text().strip()) == spool.stat().st_size


def test_wake_all_served_advances_offset_without_model_wake() -> None:
    spool = _tmp_spool()
    lines = _reconciliation_lines()[:2]
    spool.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run_reconciliation_wake(spool, _receipt_response([
        {"recipient_key": "R", "role_row_id": "served-live", "served": True},
        {"recipient_key": "R", "role_row_id": "served-catchup", "served": True},
    ]))
    assert result.exit_code == 0, result.output
    assert not result.stderr
    assert int(spool_mod.spool_offset_path(spool).read_text().strip()) == spool.stat().st_size


def test_wake_reconciliation_failures_preserve_every_line_and_warn() -> None:
    for label, response, failure in (
        ("missing", _receipt_response([
            {"recipient_key": "R", "role_row_id": "served-live", "served": True},
        ]), None),
        ("foreign", _receipt_response([], instance_id="agi-foreign"), None),
        ("lookup", {}, RuntimeError("transport down")),
    ):
        spool = _tmp_spool()
        lines = _reconciliation_lines()
        spool.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = _run_reconciliation_wake(spool, response, failure=failure)
        assert result.exit_code == wake_mod.WAKE_EXIT_SIGNAL, (label, result.output)
        assert "wake receipt lookup failed" in result.stderr, (label, result.stderr)
        for line in lines:
            assert line in result.stderr, (label, line, result.stderr)
        assert int(spool_mod.spool_offset_path(spool).read_text().strip()) == spool.stat().st_size


def test_wake_survivor_write_or_flush_failure_does_not_advance_offset() -> None:
    class BrokenOutput:
        def __init__(self, failure: str) -> None:
            self.failure = failure

        def write(self, _value: str) -> None:
            if self.failure == "write":
                raise RuntimeError("survivor write failed")

        def flush(self) -> None:
            if self.failure == "flush":
                raise RuntimeError("survivor flush failed")

    response = _receipt_response([
        {"recipient_key": "R", "role_row_id": "served-live", "served": True},
        {"recipient_key": "R", "role_row_id": "served-catchup", "served": True},
        {"recipient_key": "R", "role_row_id": "unserved", "served": False},
    ])
    for failure in ("write", "flush"):
        spool = _tmp_spool()
        lines = _reconciliation_lines()
        spool.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _FakeBridgeClient.calls = []
        _FakeBridgeClient.response = response
        _FakeBridgeClient.failure = None
        with (
            patch.object(receipt_mod, "BridgeClient", _FakeBridgeClient),
            patch.object(receipt_mod, "resolve_base_url", lambda: "http://test"),
            patch(
                "click.utils._default_text_stderr",
                lambda failure=failure: BrokenOutput(failure),
            ),
        ):
            result = _invoke_wake(spool, env=_MANAGED_FLEET_ENV)
        offset = spool_mod.spool_offset_path(spool)
        assert result.exit_code != wake_mod.WAKE_EXIT_SIGNAL, (failure, result.output)
        assert not offset.exists() or int(offset.read_text().strip()) == 0, failure


def test_wake_commits_the_offset_only_after_the_packet_is_emitted() -> None:
    """D5: emit-then-commit, so a crash re-wakes instead of losing silently.

    The offset flip is this path's ``/peer/delivered``; REL-05 already ruled
    emit-before-flip on the MCP half, so both delivery halves are now
    confirm-then-commit. Pre-fix the offset was written INSIDE
    ``_block_until_delivery``, before the packet existed, so a kill in that
    window (crash, /clear, operator ^C, hook timeout) recorded the lines as
    consumed while nothing had ever been shown -- at-most-once, silently.

    Red mutation: move ``_write_offset`` back above the ``click.echo``.
    """
    spool = _tmp_spool()
    line = json.dumps({"watch": "event", "event": {"content": "ping-D5"}})
    spool.write_text(line + "\n", encoding="utf-8")
    offset_file = spool_mod.spool_offset_path(spool)

    # Crash exactly in the window: the packet write raises, so nothing was
    # shown. The offset must NOT have advanced.
    def _boom(_message: object, **_kw: object) -> None:
        raise RuntimeError("killed mid-emit")

    with patch.object(wake_mod.click, "echo", _boom):
        crashed = _invoke_wake(spool)
    assert crashed.exit_code != wake_mod.WAKE_EXIT_SIGNAL, crashed.output
    assert not offset_file.exists() or int(offset_file.read_text().strip()) == 0, (
        "the offset advanced for lines that were never emitted"
    )

    # The next wake resurfaces them -- at-least-once, and visibly.
    recovered = _invoke_wake(spool)
    assert recovered.exit_code == wake_mod.WAKE_EXIT_SIGNAL, recovered.output
    assert "ping-D5" in recovered.stderr
    assert int(offset_file.read_text().strip()) == len(line) + 1


def test_the_post_emit_crash_window_costs_exactly_one_duplicate() -> None:
    """The residual is BOUNDED, and the bound is the contract.

    A crash AFTER the packet is written but BEFORE the offset flip re-emits the
    same bytes once. "Duplicates are possible" is not a contract; "exactly one
    per crash, content-recognizable" is -- so assert the count, not the
    possibility. The singleton flock is what bounds concurrency to one waker.
    """
    spool = _tmp_spool()
    line = json.dumps({"watch": "event", "event": {"content": "ping-dup"}})
    spool.write_text(line + "\n", encoding="utf-8")
    offset_file = spool_mod.spool_offset_path(spool)

    real_write = wake_mod._write_offset

    def _die_after_emit(path: Path, value: int) -> None:  # noqa: ARG001
        raise RuntimeError("killed after emit, before commit")

    with patch.object(wake_mod, "_write_offset", _die_after_emit):
        first = _invoke_wake(spool)
    assert "ping-dup" in first.stderr, first.stderr

    assert wake_mod._write_offset is real_write
    second = _invoke_wake(spool)
    assert second.exit_code == wake_mod.WAKE_EXIT_SIGNAL, second.output
    assert second.stderr.count("ping-dup") == 1, second.stderr

    # ...and exactly one. The third wake is idle, so the duplicate does not
    # repeat: the commit landed on the second run.
    third = _invoke_wake(spool)
    assert third.exit_code == 0, third.output
    assert "ping-dup" not in third.stderr, third.stderr
    assert int(offset_file.read_text().strip()) == len(line) + 1


def test_wake_expires_idle_without_waking() -> None:
    # Fully-consumed spool + no new deliveries -> --max-wait expiry, exit 0.
    spool = _tmp_spool()
    line = '{"watch": "event"}\n'
    spool.write_text(line, encoding="utf-8")
    spool_mod.spool_offset_path(spool).write_text(f"{len(line)}\n")
    with patch.object(wake_mod, "WAKE_POLL_INTERVAL_S", 0.01):
        result = _invoke_wake(spool)
    assert result.exit_code == 0, result.output
    assert not result.stderr


def test_wake_ignores_trailing_partial_line() -> None:
    # A torn append (no trailing newline) must not wake with half a JSON
    # line; it waits for the line to complete.
    spool = _tmp_spool()
    spool.write_text('{"watch": "eve', encoding="utf-8")
    with patch.object(wake_mod, "WAKE_POLL_INTERVAL_S", 0.01):
        result = _invoke_wake(spool)
    assert result.exit_code == 0, result.output


def test_wake_singleton_yields_to_armed_waker() -> None:
    # Every turn's Stop spawns a waker; the flock collapses them to one so a
    # single delivery never produces N duplicate wakes.
    spool = _tmp_spool()
    spool.write_text('{"watch": "event"}\n', encoding="utf-8")
    lock = spool_mod.spool_lock_path(spool)
    with lock.open("w", encoding="utf-8") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _invoke_wake(spool)
    assert result.exit_code == 0, result.output
    assert not result.stderr


def test_wake_truncates_fully_consumed_oversized_spool() -> None:
    spool = _tmp_spool()
    content = ('{"watch": "event"}\n' * 200)
    spool.write_text(content, encoding="utf-8")
    spool_mod.spool_offset_path(spool).write_text(f"{len(content)}\n")
    with (
        patch.object(wake_mod, "WAKE_SPOOL_TRUNCATE_BYTES", 100),
        patch.object(wake_mod, "WAKE_POLL_INTERVAL_S", 0.01),
    ):
        result = _invoke_wake(spool)
    assert result.exit_code == 0, result.output
    assert spool.stat().st_size == 0
    assert int(spool_mod.spool_offset_path(spool).read_text().strip()) == 0


def test_wake_resurfaces_when_spool_shrank_below_offset() -> None:
    # Spool recreated shorter than the recorded offset (manual cleanup):
    # resurface from the start rather than silently skipping deliveries.
    spool = _tmp_spool()
    spool.write_text('{"watch": "event", "event": {"content": "re"}}\n')
    spool_mod.spool_offset_path(spool).write_text("9999\n")
    result = _invoke_wake(spool)
    assert result.exit_code == wake_mod.WAKE_EXIT_SIGNAL, result.output
    assert '"re"' in result.stderr


def test_wake_caps_surfaced_lines() -> None:
    spool = _tmp_spool()
    lines = [json.dumps({"watch": "event", "n": i}) for i in range(50)]
    spool.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with patch.object(wake_mod, "WAKE_MAX_SURFACED_LINES", 3):
        result = _invoke_wake(spool)
    assert result.exit_code == wake_mod.WAKE_EXIT_SIGNAL, result.output
    assert "(+47 more line(s)" in result.stderr


def test_wake_corrupt_offset_dies_loud_not_as_wake() -> None:
    # A corrupt offset sidecar must be a plain visible error — never exit 2,
    # which would impersonate a delivery wake.
    spool = _tmp_spool()
    spool.write_text('{"watch": "event"}\n', encoding="utf-8")
    spool_mod.spool_offset_path(spool).write_text("not-a-number\n")
    result = _invoke_wake(spool)
    assert result.exit_code not in (0, wake_mod.WAKE_EXIT_SIGNAL), result.output


def test_watch_spools_deliveries_but_not_armed_line() -> None:
    # The watch side of the contract: event + inbox lines are teed to the
    # spool for the waker; the armed line is not (re-arming is not a
    # delivery and must not wake anyone).
    spool = _tmp_spool()
    with patch.object(cli_mod.click, "echo", lambda _s, **_kw: None):
        cli_mod._emit_line({"watch": "armed", "role": "Worker-A"})
        cli_mod._emit_line({"watch": "inbox", "entry": {"text": "hi"}}, spool)
        cli_mod._emit_line({"watch": "event", "event": {"content": "yo"}}, spool)
    lines = spool.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"inbox"' in lines[0]
    assert '"event"' in lines[1]


def test_watch_and_wake_derive_the_same_spool_path() -> None:
    # The pairing contract: watch (writer) and wake (reader) must meet at the
    # SAME derived path with no flags — both use the unmanaged fallback.
    with tempfile.TemporaryDirectory() as temporary_dir:
        runtime_dir = Path(temporary_dir)
        with (
            patch.dict("os.environ", _FLEET_ENV, clear=True),
            patch.object(spool_mod, "get_runtime_dir", lambda _name: runtime_dir),
            patch.object(wake_mod, "resolve_solet_name", lambda: "testling"),
        ):
            watch_identity = cli_mod._resolve_watch_identity(None, "codex")
            watch_spool = spool_mod.default_spool_path(
                "testling",
                watch_identity.agent_instance_id,
            )
            wake_target = wake_mod._resolve_target(None)
    assert wake_target is not None
    assert wake_target.spool == watch_spool
    assert watch_identity.agent_instance_id.startswith("agi-watch-")


def test_managed_session_halves_pair_on_the_ledger_id_spool() -> None:
    """Managed watch and wake halves must use the launcher's ledger identity.

    Fails before the shared resolver: watch writes the ledger-id spool while
    wake derives an ``agi-watch-<digest>`` spool and parks there until timeout.
    """
    with tempfile.TemporaryDirectory() as temporary_dir:
        runtime_dir = Path(temporary_dir)
        with (
            patch.dict("os.environ", _MANAGED_FLEET_ENV, clear=True),
            patch.object(spool_mod, "get_runtime_dir", lambda _name: runtime_dir),
            patch.object(wake_mod, "resolve_solet_name", lambda: "testling"),
        ):
            watch_identity = cli_mod._resolve_watch_identity(None, "codex")
            watch_spool = spool_mod.default_spool_path(
                "testling",
                watch_identity.agent_instance_id,
            )
            spool_mod.write_watch_pairing(
                spool_mod.watch_pairing_path(
                    "testling",
                    watch_identity.agent_instance_id,
                ),
                watch_spool,
            )
            watch_spool.write_text('{"watch": "event"}\n', encoding="utf-8")
            wake_target = wake_mod._resolve_target(None)
            assert watch_spool.read_text(encoding="utf-8") == '{"watch": "event"}\n'
    assert wake_target is not None
    assert wake_target.spool == watch_spool


def test_wake_identity_error_is_not_a_wake_exit() -> None:
    # SoletIdentityError maps to UNKNOWN_ERROR (1) by design:
    # ExitCodes.CONNECTION_ERROR is 2, which the hook contract reads as a
    # wake — an identity failure must never impersonate one.
    def boom() -> str:
        raise cli_mod.SoletIdentityError("unreadable root_manifest")

    with patch.object(wake_mod, "resolve_solet_name", boom):
        result = CliRunner().invoke(
            wake_mod.wake, ["--max-wait", "0.05"], env=dict(_FLEET_ENV), obj={},
        )
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output
    assert result.exit_code != wake_mod.WAKE_EXIT_SIGNAL


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
