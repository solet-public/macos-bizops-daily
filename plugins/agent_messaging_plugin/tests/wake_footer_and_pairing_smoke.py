#!/usr/bin/env python3
"""Census batch-3 — the wake hook's footer, its identity guard, and its pairing.

Three findings from `workbench/2026-07-31_ws2d_unknown_issue_discovery_census_claude_c.md`:

  * **D6 — the footer prints a command nobody ever runs as a check.**
    `_compose_wake_packet` tells every woken session to invoke a specific
    process key. That string was reasoned about carefully at authoring time and
    never *executed*: it 500'd against the live registry for as long as the
    process was unregistered, and nothing anywhere would have noticed. This
    smoke extracts the key FROM the footer the model is actually shown and
    asserts the platform can answer it — the decorated verb exists and the
    knowledge-base process JSON that registers it exists. A future footer edit
    that renames or mistypes the key goes red here.

  * **D3 — the wake half failed silent where the delivery half fails loud.**
    `watch` dies loud on a missing `AGENT_SESSION_ID`; `wake`, given the same
    environment, returned None and exited 0 — indistinguishable from "healthy,
    nothing to do", leaving a configured session permanently un-wakeable with no
    diagnostic. Neither-present must stay silent (the Stop hook is user-scoped
    and fires in unrelated sessions); exactly-one-present must be loud.

  * **D4 — `--spool` / `--no-spool` could decouple the pair undetectably.**
    Both halves derived the same DEFAULT path, which proves agreement only when
    both are on defaults. The watcher now publishes its actual choice and the
    wake half reads it.

⚠ The negative controls use `env -u`-equivalent removal (`patch.dict(..., clear=True)`)
rather than merely not setting a variable: this process inherits the launcher's
exports, so an omitted patch would silently test the positive case.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/wake_footer_and_pairing_smoke.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from click.testing import CliRunner  # noqa: E402

import agent_messaging_plugin.local_cli.wake as wake_mod  # noqa: E402
from agent_messaging_plugin.local_cli.spool import (  # noqa: E402
    read_watch_pairing,
    watch_pairing_path,
    write_watch_pairing,
)

_PLUGIN_SRC = (
    REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"
    / "agent_messaging_plugin" / "plugin.py"
)
_KB_PROCESSES = (
    REPO_ROOT / "plugins" / "agent_messaging_plugin" / "knowledge_base" / "processes"
)
_PROCESS_KEY_RE = re.compile(r"plugin::([a-z_]+)::([a-z_]+)")

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


def _footer_packet() -> str:
    target = wake_mod.WakeTarget(
        role="Claude-C",
        solet_name="testhome",
        spool=Path("/tmp/x.spool"),
        offset_file=Path("/tmp/x.spool.offset"),
        lock_file=Path("/tmp/x.spool.lock"),
    )
    return wake_mod._compose_wake_packet(target, ['{"watch": "event"}'])


# ---------------------------------------------------------------------------
# D6 — the key the footer advertises must be one the platform can answer
# ---------------------------------------------------------------------------


def test_footer_advertises_a_real_process_key() -> None:
    packet = _footer_packet()
    keys = _PROCESS_KEY_RE.findall(packet)
    _check(keys, f"D6: the wake footer advertises a process key at all ({keys})")
    source = _PLUGIN_SRC.read_text(encoding="utf-8")
    for plugin_name, verb in keys:
        _check(
            plugin_name == "agent_messaging_plugin",
            f"D6: {verb} is advertised under this plugin's own namespace",
        )
        _check(
            f'name="{verb}"' in source,
            f"D6: a decorated verb named {verb!r} exists in plugin.py — the "
            "footer's key is answerable, not just well-formed",
        )
        _check(
            (_KB_PROCESSES / f"{verb}.json").is_file(),
            f"D6: knowledge_base/processes/{verb}.json exists — the process is "
            "REGISTERED, which is what the live 500 proved it was not",
        )


def test_footer_argument_names_are_declared_by_the_verb() -> None:
    """The footer supplies arguments; the verb must actually accept them.

    A key that resolves but rejects the arguments the footer prints fails just
    as hard as an unregistered one — it was `missing_argument` instead of 500.
    """
    # Scope to the advertised COMMAND, not the whole packet: the surfaced body
    # is spooled delivery JSON and its keys are not verb arguments. (This test
    # first went red on "watch" from a spool line — a reminder that the packet
    # is a mixed document and only one slice of it is the contract.)
    packet = _footer_packet()
    _, _, command = packet.partition("Durable copies:")
    _check(command, "D6: the footer still carries a durable-copies command")
    source = _PLUGIN_SRC.read_text(encoding="utf-8")
    args = re.findall(r'"([a-z_]+)":', command)
    _check(args, f"D6: the advertised command passes arguments ({args})")
    for arg in args:
        _check(
            f'"{arg}": ParameterMetadata' in source,
            f"D6: the footer's {arg!r} argument is a declared parameter",
        )


# ---------------------------------------------------------------------------
# D3 — silent for a non-fleet session, LOUD for a half-configured one
# ---------------------------------------------------------------------------


def _run_wake(env: dict[str, str]) -> tuple[int, str]:
    with patch.dict(os.environ, env, clear=True):
        result = CliRunner().invoke(wake_mod.wake, [], obj={})
    return result.exit_code, (result.output or "") + str(result.exception or "")


def test_wake_silent_when_no_launcher_identity() -> None:
    """Neither variable present = an ordinary session; the user-scope hook must no-op."""
    code, _ = _run_wake({})
    _check(code == 0, f"D3: no launcher identity → silent exit 0 (got {code})")


def test_wake_loud_when_identity_is_half_configured() -> None:
    """Exactly one present = a fleet session that can never be woken. Say so.

    Mutation that turns this red: restore the original
    `if not role or not session_id: return None`, collapsing both cases into
    the silent branch.
    """
    for env, missing in (
        ({"AGENT_SESSION_LABEL": "Claude-C"}, "AGENT_SESSION_ID"),
        ({"AGENT_SESSION_ID": "ases-1"}, "AGENT_SESSION_LABEL"),
    ):
        code, out = _run_wake(env)
        _check(
            code != 0 and code != wake_mod.WAKE_EXIT_SIGNAL,
            f"D3: half-configured ({missing} missing) → non-zero, non-wake "
            f"exit (got {code})",
        )
        _check(
            missing in out,
            f"D3: the diagnostic names the missing variable {missing}",
        )


# ---------------------------------------------------------------------------
# D4 — the watcher publishes its spool choice; the wake half pairs with it
# ---------------------------------------------------------------------------


def test_pairing_roundtrip_and_absence() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        pairing = Path("pair.json")
        _check(
            read_watch_pairing(pairing) == (False, None),
            "D4: no sidecar → (False, None) so the caller keeps the derived "
            "default (pre-D4 behaviour when no watcher ever armed)",
        )
        write_watch_pairing(pairing, Path("/tmp/elsewhere.spool"))
        _check(
            read_watch_pairing(pairing) == (True, Path("/tmp/elsewhere.spool")),
            "D4: a relocated spool round-trips, so `watch --spool` can no "
            "longer silently decouple the pair",
        )
        write_watch_pairing(pairing, None)
        _check(
            read_watch_pairing(pairing) == (True, None),
            "D4: --no-spool round-trips as an EXPLICIT null, distinguishable "
            "from 'no watcher has armed'",
        )
        pairing.write_text("{not json", encoding="utf-8")
        _check(
            read_watch_pairing(pairing) == (False, None),
            "D4: a corrupt sidecar degrades to the default rather than raising "
            "inside a Stop hook",
        )


def test_pairing_path_is_derived_from_session_identity_alone() -> None:
    """Both halves must find the sidecar without a handshake."""
    a = watch_pairing_path("testhome", "agi-watch-abc")
    b = watch_pairing_path("testhome", "agi-watch-abc")
    c = watch_pairing_path("testhome", "agi-watch-xyz")
    _check(a == b, "D4: the sidecar path is deterministic per session")
    _check(a != c, "D4: distinct sessions get distinct sidecars")


def test_wake_reports_rather_than_blocks_when_tee_is_disabled() -> None:
    """`watch --no-spool` = no wake can ever arrive. Report it; don't block 23.9h.

    Mutation that turns this red: have `_paired_spool` fall through to the
    derived default on a (True, None) pairing — the wake hook would then wait
    out its whole --max-wait on a file nothing writes, reporting nothing.
    """
    runner = CliRunner()
    with runner.isolated_filesystem() as tmp:
        name, instance = "testhome", "agi-watch-nospool"
        with patch.object(
            wake_mod, "watch_pairing_path",
            lambda _n, _i: Path(tmp) / "pair.json",
        ):
            write_watch_pairing(Path(tmp) / "pair.json", None)
            raised = 0
            try:
                wake_mod._paired_spool(name, instance)
            except SystemExit as exc:
                raised = int(exc.code or 0)
        _check(
            raised != 0 and raised != wake_mod.WAKE_EXIT_SIGNAL,
            f"D4: a --no-spool pairing exits non-zero, non-wake (got {raised})",
        )


def main() -> None:
    print("census batch-3 — wake footer (D6), identity guard (D3), pairing (D4)")
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            print(f"\n{name}")
            obj()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
