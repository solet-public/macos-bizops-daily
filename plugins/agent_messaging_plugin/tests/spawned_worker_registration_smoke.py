#!/usr/bin/env python3
"""Offline smoke for spawned CLAUDE-worker peer registration.

RCA of record: workbench/2026-08-13_registration_loss_rca_lane_r_report.md.
Two spawned tmux workers ran multi-hour productive lives on 2026-08-13/14 and
never appeared in ``peer_list`` — unreachable by ``peer_send``, contributing
zero liveness, reaped mid-programme. Root cause was an asymmetry: the managed
Codex adapters arm a presence watcher and resolve an absolute CLI, the Claude
adapters did neither.

Named mutations this suite must catch:

* a Claude adapter exporting the BARE command name ``solet-bridge`` as
  ``AGENT_WAKE_CLI`` again (dies FileNotFoundError, silently, under the
  minimal PATH a tmux pane or a materialized release actually runs with);
* the tmux ``-e`` allowlist dropping ``PATH`` again (52edfb559's lesson,
  learned on the Codex path only);
* either Claude adapter shipping NO presence watcher on the ``watch``
  transport — the registration loss itself;
* a Claude sidecar copying Codex's ``--no-spool`` verbatim, which would
  re-deafen the very Stop-hook wake the sidecar exists to enable;
* a sidecar CLAIMING a role at launch instead of registering presence only
  (``--no-claim`` is what keeps role ownership model-initiated);
* arming the sidecar on the ``mcp`` transport, where the bridge registers;
* the ``/rename`` skill and its seed template drifting out of lock-step, or
  either one telling the model to claim against ``$AGENT_INSTANCE_ID``
  (the launcher's id) rather than the watcher's derived ``agi-watch-*`` id.

No tmux server, bridge, database, or model turn is used.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    HeadlessHostDriver,
    _arm_watcher,
)
from agent_messaging_plugin.local_cli.spool import (  # noqa: E402
    watch_instance_digest,
)
from agent_messaging_plugin.models import (  # noqa: E402
    WATCH_AGENT_INSTANCE_PREFIX,
)
from agent_messaging_plugin.solet_cli import (  # noqa: E402
    expose_worker_cli,
    resolve_solet_bin,
    watch_sidecar_argv,
)
from agent_messaging_plugin.tmux_adapter import (  # noqa: E402
    _env_pairs,
    _pane_command,
)

_RENAME_SKILL = REPO_ROOT / ".claude" / "commands" / "rename.md"
_RENAME_TEMPLATE = (
    REPO_ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
    / "hydration_templates" / "rename_skill_SKILL.md.template"
)

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


def _executable(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    return str(target)


def _env_map(pairs: list[str]) -> dict[str, str]:
    """``["-e", "K=V", ...]`` -> ``{"K": "V"}``, asserting the -e pairing."""
    out: dict[str, str] = {}
    for flag, assignment in zip(pairs[::2], pairs[1::2], strict=True):
        assert flag == "-e", f"expected -e flag, got {flag!r}"
        key, _, value = assignment.partition("=")
        out[key] = value
    return out


def _tmux_env(solet_bin: str, *, transport: str = "watch") -> dict[str, str]:
    return _env_map(_env_pairs(
        agent_instance_id="agi-test", agent_session_id="ases-agi-test",
        label="lane-x", solet_name="testsolet", solet_bin=solet_bin,
        allowed_tools=(), transport=transport,
        context_gauge_reporter_path=(
            Path(solet_bin).parent / "rotation_due_watch.py"
        ),
        # The spawn's RESOLVED lane root (2026-09-07, iss_2236d297) -- these
        # CLI/PATH assertions are indifferent to which root it is, so this
        # passes the same directory the fixture already built.
        cwd=Path(solet_bin).parent,
    ))


def test_tmux_env_exports_absolute_cli_and_path() -> None:
    print("\ntmux adapter exports an absolute CLI and a PATH that finds it")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        solet_bin = _executable(bin_dir, "solet-bridge")
        env = _tmux_env(solet_bin)
    _check(
        env.get("AGENT_WAKE_CLI") == solet_bin,
        "AGENT_WAKE_CLI is the ABSOLUTE binary, not the bare name 'solet-bridge'",
    )
    _check(
        env.get("AGENT_WAKE_CLI") != "solet-bridge",
        "AGENT_WAKE_CLI is not the bare command name (the silent-death shape)",
    )
    _check("PATH" in env, "PATH crosses the tmux -e allowlist boundary")
    _check(
        env.get("PATH", "").split(os.pathsep)[0] == str(bin_dir),
        "the CLI's directory is PREPENDED to PATH, so a bare `solet-bridge` resolves",
    )
    _check(
        env.get("AGENT_WAKE_CLI") != env.get("SOLET_NAME"),
        "AGENT_WAKE_CLI is the binary, never the solet INSTANCE name",
    )


def test_tmux_pane_command_arms_registration_sidecar_with_spool() -> None:
    print("\ntmux pane command arms the presence sidecar (watch transport)")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp)
        solet_bin = _executable(bin_dir, "solet-bridge")
        pane = _pane_command(
            ["claude", "--print"], label="lane-x",
            solet_bin=solet_bin, transport="watch",
        )
        mcp_pane = _pane_command(
            ["claude", "--print"], label="lane-x",
            solet_bin=solet_bin, transport="mcp",
        )
        no_cli_pane = _pane_command(
            ["claude", "--print"], label="lane-x",
            solet_bin="", transport="watch",
        )
    _check(
        f"{shlex.quote(solet_bin)} watch" in pane or f"{solet_bin} watch" in pane,
        "a `solet-bridge watch` sidecar is armed at all — THE registration step",
    )
    _check("--no-claim" in pane, "the sidecar registers presence WITHOUT claiming a role")
    _check(
        "--no-spool" not in pane,
        "the Claude sidecar keeps its spool (wake_waiter.py consumes it) — "
        "copying Codex's --no-spool would re-deafen the wake",
    )
    _check("--exit-with-parent $$" in pane, "the sidecar dies with the pane's shell")
    # find(), not index(): a mutation that removes the sidecar must RED this
    # check, not raise ValueError out of the suite (a crash reports no verdict
    # for the remaining legs at all).
    _check(
        0 <= pane.find(" watch ") < pane.find("exec "),
        "the sidecar is backgrounded BEFORE exec replaces the shell",
    )
    _check(
        pane.rstrip().endswith(shlex.join(["claude", "--print"])),
        "the pane still execs the worker command last",
    )
    _check("watch" not in mcp_pane, "no sidecar on the mcp transport (the bridge registers)")
    _check(
        "watch" not in no_cli_pane and no_cli_pane.endswith(
            f"exec {shlex.join(['claude', '--print'])}",
        ),
        "an unresolvable CLI degrades to the pre-fix pane, never a broken one",
    )


def test_headless_env_and_watcher() -> None:
    print("\nheadless adapter: absolute CLI, PATH, and a tracked sidecar")
    calls: list[dict[str, Any]] = []

    class _FakeProc:
        pid = 4242
        stdin = None
        stdout = None
        stderr = None

        def poll(self) -> int | None:
            return None

    def _popen(argv: list[str], **kwargs: Any) -> Any:
        calls.append({"argv": argv, "env": dict(kwargs.get("env") or {})})
        return _FakeProc()

    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        solet_bin = _executable(bin_dir, "solet-bridge")
        driver = HeadlessHostDriver(
            claude_bin="/bin/true", solet_name="testsolet", solet_bin=solet_bin,
            transport="watch", cwd=Path(tmp),
            python_executable=str(bin_dir / "python3"), popen_fn=_popen,
        )
        env = driver._spawn_env(  # noqa: SLF001 -- offline shape assertion
            agent_instance_id="agi-test", agent_session_id="ases-agi-test",
            label="lane-x", allowed_tools=(), transport="watch",
            context_gauge_reporter_path=(
                Path(tmp) / ".claude" / "hooks" / "rotation_due_watch.py"
            ),
        )
        watcher = _arm_watcher(
            _popen, solet_bin, Path(tmp), 999, env, "watch",
        )
        none_on_mcp = _arm_watcher(
            _popen, solet_bin, Path(tmp), 999, env, "mcp",
        )

    _check(env.get("AGENT_WAKE_CLI") == solet_bin, "headless AGENT_WAKE_CLI is absolute")
    _check(
        env.get("AGENT_CONTEXT_GAUGE_REPORTER_PATH", "").endswith(
            ".claude/hooks/rotation_due_watch.py"
        ),
        "headless exports its designated gauge reporter path",
    )
    _check(
        env.get("PATH", "").split(os.pathsep)[0] == str(bin_dir),
        "headless PATH is prepended with the CLI's directory",
    )
    _check(watcher is not None, "headless arms a presence watcher on the watch transport")
    _check(none_on_mcp is None, "headless arms no watcher on the mcp transport")
    watch_calls = [c for c in calls if "watch" in c["argv"]]
    _check(len(watch_calls) == 1, "exactly one watcher spawned")
    # Every downstream leg reads watch_calls[0]; a mutation that arms NO
    # watcher must RED each of them rather than raising IndexError and
    # leaving the rest of the suite unreported.
    argv: list[str] = watch_calls[0]["argv"] if watch_calls else []
    _check(argv[:1] == [solet_bin], "the watcher runs the ABSOLUTE binary")
    _check("--no-claim" in argv, "headless watcher registers presence WITHOUT a role claim")
    _check(
        bool(argv) and "--no-spool" not in argv,
        "headless watcher keeps its spool for wake_waiter.py",
    )
    _check(
        "--exit-with-parent" in argv
        and argv[argv.index("--exit-with-parent") + 1] == "999",
        "the watcher is bound to the worker's pid, not its own",
    )


def test_resolver_falls_back_to_the_active_venv() -> None:
    print("\nCLI resolution survives a minimal PATH")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        solet_bin = _executable(bin_dir, "solet-bridge")
        fake_python = str(bin_dir / "python3")
        original = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = "/nonexistent-for-this-smoke"
            resolved = resolve_solet_bin(None, python_executable=fake_python)
        finally:
            os.environ["PATH"] = original
    _check(
        resolved == solet_bin,
        "resolves the venv-sibling CLI when PATH cannot (the release/pane case)",
    )
    _check(
        resolve_solet_bin(solet_bin, python_executable=fake_python) == solet_bin,
        "owned explicit path wins",
    )
    _check(
        resolve_solet_bin(None, python_executable="/nope/python3") == ""
        or Path(resolve_solet_bin(None, python_executable="/nope/python3")).is_absolute(),
        "unresolvable returns empty rather than a bogus relative path",
    )
    degraded: dict[str, str] = {"PATH": "/usr/bin"}
    expose_worker_cli(degraded, "")
    _check(
        degraded["AGENT_WAKE_CLI"] == "solet-bridge" and degraded["PATH"] == "/usr/bin",
        "an unresolved CLI degrades to the bare name and leaves PATH untouched",
    )
    absent = {"PATH": "/usr/bin:/bin"}
    expose_worker_cli(absent, "/a/solet-bridge")
    _check(
        absent["PATH"] == f"/a{os.pathsep}/usr/bin{os.pathsep}/bin",
        "an absent CLI directory is prepended to PATH",
    )
    idempotent = {"PATH": f"/a{os.pathsep}/usr/bin{os.pathsep}/bin"}
    expose_worker_cli(idempotent, "/a/solet-bridge")
    expose_worker_cli(idempotent, "/a/solet-bridge")
    _check(
        idempotent["PATH"] == f"/a{os.pathsep}/usr/bin{os.pathsep}/bin",
        "an already-leading CLI directory remains leading and unique across re-exposure",
    )
    later_and_duplicate = {"PATH": f"/usr/bin{os.pathsep}/a{os.pathsep}/bin{os.pathsep}/a"}
    expose_worker_cli(later_and_duplicate, "/a/solet-bridge")
    _check(
        later_and_duplicate["PATH"] == f"/a{os.pathsep}/usr/bin{os.pathsep}/bin",
        "later and duplicate CLI-directory entries are removed before one leading prepend",
    )


def test_sidecar_argv_contract() -> None:
    print("\nwatch_sidecar_argv encodes the presence-not-ownership contract")
    # Neither actual Codex host driver (codex_tmux.py, codex_app_server.py)
    # calls this shared helper -- they build their watch argv inline (see
    # codex_managed_session_smoke.py's spool-parity legs for the real,
    # CDX-06-updated Codex call-site behavior). This leg only pins the
    # helper's OWN mechanics: it must still honor whatever `spool` value a
    # future caller passes, in both directions -- not a claim about which
    # value any current caller uses.
    claude = watch_sidecar_argv("/x/solet-bridge", agent_id="claude_code", spool=True)
    codex = watch_sidecar_argv("/x/solet-bridge", agent_id="codex", spool=False)
    _check("--no-claim" in claude and "--no-claim" in codex, "--no-claim on BOTH runtimes")
    _check("--no-spool" not in claude, "spool=True never emits --no-spool")
    _check("--no-spool" in codex, "spool=False still emits --no-spool (helper mechanics only)")
    _check("--role" not in claude, "the sidecar never passes --role: presence, not ownership")
    _check(
        claude[claude.index("--agent-id") + 1] == "claude_code",
        "claude sidecar registers under the claude_code agent id",
    )


def test_presence_and_liveness_regress_independently() -> None:
    """RCA of record (Lane R, 2026-08-14): PRESENCE (the watch sidecar
    ``_pane_command`` arms) and LIVENESS (the ``AGENT_WAKE_CLI``/``PATH``
    export ``_env_pairs``/``expose_worker_cli`` build, which
    ``heartbeat_report_alive.py``'s bare ``subprocess.run(["solet-bridge", ...])``
    depends on to resolve) are two separate mechanisms built by two
    functions that never call each other or share mutable state. A future
    change could restore one without the other and still look green if
    nothing asserts them side by side. This pins that they redden
    independently: proof-by-mutation recorded in
    ``workbench/2026-08-13_presence_liveness_independence_smoke_report_lane_s.md``
    (each mutation applied by hand to src, one at a time, smoke run,
    reverted — never left in the committed tree).
    """
    print("\npresence (sidecar arm) and liveness (PATH/CLI export) regress independently")
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        solet_bin = _executable(bin_dir, "solet-bridge")
        pane = _pane_command(
            ["claude", "--print"], label="lane-x",
            solet_bin=solet_bin, transport="watch",
        )
        env = _tmux_env(solet_bin)
        # Resolved while the stub `solet-bridge` still exists on disk -- shutil.which
        # stats the candidate, so this must run inside the tempdir's lifetime.
        liveness_ok = shutil.which("solet-bridge", path=env.get("PATH", "")) == solet_bin
    presence_ok = "watch" in pane and "--no-claim" in pane
    # The literal dependency heartbeat_report_alive.py's bare
    # `subprocess.run(["solet-bridge", ...])` has on PATH -- not a proxy for it.
    _check(
        presence_ok,
        "LEG 1 baseline: presence assertion is green (sidecar armed in the pane) -- "
        "named mutation: neuter the `if transport == 'watch' and solet_bin:` block "
        "in tmux_adapter.py::_pane_command; must redden ONLY this assertion",
    )
    _check(
        liveness_ok,
        "LEG 2 baseline: liveness assertion is green (PATH resolves the bare "
        "`solet-bridge` heartbeat_report_alive.py shells out to) -- named mutation: "
        "skip the PATH-prepend in solet_cli.py::expose_worker_cli; must redden "
        "ONLY this assertion",
    )
    _check(
        presence_ok and liveness_ok,
        "LEG 3: both assertions are green from the SAME baseline build, so a "
        "mutation applied to only one of _pane_command/expose_worker_cli has "
        "exactly one of these two checks available to redden -- see the "
        "report for the two separate single-mutation runs",
    )


def test_rename_skill_and_template_stay_in_lockstep() -> None:
    print("\n/rename skill + seed template teach the sidecar-aware claim")
    template = _RENAME_TEMPLATE.read_text(encoding="utf-8")
    if not _RENAME_SKILL.is_file():
        # Born clone: .claude/ is origin-only and never ships, so the lockstep
        # narrows to its shipped half — the template the clone actually carries.
        print("  NOTE: origin rename skill absent (.claude/ never ships) — "
              "checking the shipped template half only")
        surfaces: tuple[tuple[str, str], ...] = (("template", template),)
        skill = None
    else:
        skill = _RENAME_SKILL.read_text(encoding="utf-8")
        surfaces = (("skill", skill), ("template", template))
    for name, body in surfaces:
        _check(
            "peer_claim_role" in body and "agi-watch-" in body,
            f"{name}: claims via peer_claim_role against the derived agi-watch id",
        )
        _check(
            "$WATCH_ID" in body,
            f"{name}: uses the computed watcher id, not $AGENT_INSTANCE_ID, for the claim",
        )
        _check(
            "--no-claim" in body,
            f"{name}: explains that the spawned sidecar holds presence without a role",
        )
        _check(
            "watch --role" in body,
            f"{name}: PRESERVES the arm-with-role branch for operator-launched sessions",
        )
        _check(
            "1a" in body and "1b" in body,
            f"{name}: branches rather than replacing the existing path",
        )
    if skill is not None:
        _check(
            ("$ARGUMENTS" in skill) and ("$ROLE" in template),
            "each render keeps its own role variable (the documented divergence)",
        )
    else:
        _check(
            "$ROLE" in template,
            "template keeps its role variable (skill half absent in this tree)",
        )


def test_watch_id_recipe_matches_the_code() -> None:
    print("\nthe skill's shell recipe reproduces the code's watcher identity")
    session_id = "ases-agi-574487c6c5c922c8533b70e39e981031"
    from_code = f"{WATCH_AGENT_INSTANCE_PREFIX}{watch_instance_digest(session_id)}"
    from_python = "agi-watch-" + hashlib.sha256(
        session_id.encode("utf-8"),
    ).hexdigest()[:24]
    _check(from_code == from_python, "digest recipe matches watch_instance_digest")
    shell = subprocess.run(
        ["sh", "-c",
         'printf %s "$1" | shasum -a 256 | cut -c1-24', "_", session_id],
        capture_output=True, text=True, check=False,
    )
    _check(
        shell.returncode == 0
        and f"agi-watch-{shell.stdout.strip()}" == from_code,
        "the SHELL one-liner the skill hands the model matches the code exactly",
    )


def main() -> int:
    tests = [
        test_tmux_env_exports_absolute_cli_and_path,
        test_tmux_pane_command_arms_registration_sidecar_with_spool,
        test_headless_env_and_watcher,
        test_resolver_falls_back_to_the_active_venv,
        test_sidecar_argv_contract,
        test_presence_and_liveness_regress_independently,
        test_rename_skill_and_template_stay_in_lockstep,
        test_watch_id_recipe_matches_the_code,
    ]
    for test in tests:
        test()
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
