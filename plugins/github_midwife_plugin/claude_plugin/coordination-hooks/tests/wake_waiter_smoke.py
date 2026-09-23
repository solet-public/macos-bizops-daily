#!/usr/bin/env python3
"""Behavioural proof of the wake waiter's two load-bearing security claims.

This is the plugin's only privileged hook -- the only one that executes
anything -- so it is where a reviewer's attention belongs, and it is the hook
whose SECURITY.md claims are hardest to confirm by reading alone:

1. **Exactly one bit.** "The waiter discards the child's stdout and stderr
   unread; on the wake signal it emits its own compiled-in fixed nudge." Proved
   by giving the stub CLI a payload of marker strings on both streams and
   asserting not one byte of it reaches the hook's output -- and that the nudge
   is byte-identical across stubs that emit wildly different things.

2. **It can never trap a session.** "A broken wake path degrades to 'messages
   wait for the next turn', never to a stuck session." Proved by exercising
   every non-wake exit path -- clean exit, arbitrary nonzero, death by signal,
   missing executable, non-executable file -- and asserting exit 0 each time.

It also pins one load-bearing coordination claim: **the wait is bounded.** An
unbounded waiter holds the harness status file at "shell" for its whole life,
so "idle" is never stamped and the seat idle watcher is structurally blind
(2026-08-08 overnight stall). Proved by recording the argv the hook passes to
the CLI: `--max-wait` is always present, defaulting to the compiled-in cap,
honouring a valid `AGENT_WAKE_MAX_WAIT_S` override, and falling back LOUDLY
(one fixed-format stderr note) on a malformed or non-positive override.

The arming matrix is verified with a CONTROLLED environment: a stub that records
whether it ran, so a "disarmed" case proves the CLI was never invoked rather
than merely producing no output.

Run directly; exit 0 on success, non-zero on failure.
"""

from __future__ import annotations

import sys

# Must precede the _harness import — see manifest_consistency_smoke.py for why.
sys.dont_write_bytecode = True

import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _harness import Results, preflight  # noqa: E402
from _harness import run_hook as _run_hook  # noqa: E402

HOOK = "wake_waiter.py"
WAKE_SIGNAL = 2
LABEL = "Coordinator-Day"
MARKER_ENV = "STUB_MARKER"
NOTE_PREFIX = "[coordination-hooks wake]"
_PARTIAL_OWNER_FIXTURE_ENV = "COORDINATION_OWNER_PARTIAL_FIXTURE"
_REPO = Path(__file__).resolve().parents[5]
_HOOK_ROOT = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(_REPO / "plugins/github_midwife_plugin/src"))

from github_midwife_plugin.coordination_hook_installation import (  # noqa: E402
    ReceiptSurface,
    build_receipt,
    publish_receipt,
)

# Strings the stub writes to stdout and stderr. If the hook relayed child output
# in any form, one of these would surface in its own streams.
SECRET_STDOUT = "SENSITIVE-STDOUT-b3d1f0-message-body-should-never-appear"
SECRET_STDERR = "SENSITIVE-STDERR-9a72cc-message-body-should-never-appear"


def run_hook(script: str, *, env: dict[str, str] | None = None, **kwargs: object) -> object:
    requested = dict(env or {})
    armed = bool(requested.get("AGENT_SESSION_ID"))
    if armed and not requested.pop(_PARTIAL_OWNER_FIXTURE_ENV, ""):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            receipt = build_receipt(
                solet_name="fixture-solet",
                app_home=home,
                plugin_selector="coordination-hooks@fixture-solet",
                default_hook_root=_HOOK_ROOT,
                surfaces=(ReceiptSurface("plugin_cache", _HOOK_ROOT, Path(sys.executable), _HOOK_ROOT / "hooks.json"),),
                installation_id="wake-fixture",
            )
            requested.update({
                "AGENT_INSTANCE_ID": "agi-wake-fixture",
                "AGENT_COORDINATION_RECEIPT_PATH": str(publish_receipt(receipt)),
                "SOLET_NAME": "fixture-solet",
            })
            return _run_hook(script, env=requested, **kwargs)
    return _run_hook(script, env=requested, **kwargs)


def _stub(directory: Path, name: str, *, exit_code: int | None, chatty: bool = False) -> Path:
    """Write an executable stand-in for the operator's coordination CLI.

    exit_code None means "die from a signal", which makes Python's
    subprocess.run report a NEGATIVE returncode (the signal number, negated)
    rather than a small positive status -- the path most likely to be
    mishandled.
    """
    lines = [
        "#!/usr/bin/env python3",
        "import os, signal, sys",
        f"marker = os.environ.get({MARKER_ENV!r})",
        "if marker:",
        "    open(marker, 'w', encoding='utf-8').write('ran')",
    ]
    if chatty:
        lines += [
            f"sys.stdout.write({SECRET_STDOUT!r})",
            f"sys.stderr.write({SECRET_STDERR!r})",
            "sys.stdout.flush()",
            "sys.stderr.flush()",
        ]
    if exit_code is None:
        lines.append("os.kill(os.getpid(), signal.SIGTERM)")
    else:
        lines.append(f"sys.exit({exit_code})")

    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _env(marker: Path, cli: Path | str | None, *, label: str | None = LABEL, transport: str | None = None) -> dict[str, str]:
    """Build a child env for the waiter.

    ★ §7 RE-KEY 2026-08-01: the waiter arms on AGENT_SESSION_ID (identity), not
    AGENT_SESSION_LABEL. The spool path DERIVES from the id, so the id is the
    functional precondition; the label was only ever a proxy for having a spool.
    The `label` parameter is kept and mapped onto the id so every existing
    disarm-matrix case keeps its meaning — a case that passes `label=None` is
    asserting "no identity, therefore no spool, therefore silent no-op", which
    is exactly what it asserted before, now keyed on the variable that is
    actually load-bearing.
    """
    env = {MARKER_ENV: str(marker), "AGENT_INSTANCE_ID": ""}
    if label is not None:
        env["AGENT_SESSION_ID"] = label
    if cli is not None:
        env["AGENT_WAKE_CLI"] = str(cli)
    if transport is not None:
        env["FLEET_TRANSPORT"] = transport
    return env


def check_disarm_matrix(res: Results, work: Path) -> None:
    """Disarmed means the CLI is never invoked -- not merely that output is empty."""
    cli = _stub(work, "disarm_stub", exit_code=WAKE_SIGNAL)
    cases = (
        ("no session label", _env(work / "m1", cli, label=None)),
        ("no wake CLI", _env(work / "m2", None)),
        ("declared transport mcp", _env(work / "m3", cli, transport="mcp")),
        ("declared transport http", _env(work / "m4", cli, transport="http")),
    )
    for label, env in cases:
        marker = Path(env[MARKER_ENV])
        marker.unlink(missing_ok=True)
        proc = run_hook(HOOK, env=env)
        res.check(proc.returncode == 0, f"disarmed ({label}) exits 0", f"exit {proc.returncode}")
        res.check(proc.stdout == "", f"disarmed ({label}) writes no stdout", f"got {proc.stdout[:80]!r}")
        expected_stderr = "ownership" in proc.stderr if label == "no session label" else proc.stderr == ""
        res.check(expected_stderr, f"disarmed ({label}) has the expected stderr", f"got {proc.stderr[:80]!r}")
        res.check(not marker.exists(), f"disarmed ({label}) never spawns the CLI", "the stub ran")


def check_arm_matrix(res: Results, work: Path) -> None:
    cli = _stub(work, "arm_stub", exit_code=0)
    cases = (
        ("transport unset", None),
        ("transport watch", "watch"),
        # RULED 2026-07-31 (Architect): an empty FLEET_TRANSPORT is EQUIVALENT
        # TO UNSET — empty is not a declaration — so it ARMS. This is the
        # contract, no longer a documented divergence: a hydration bug that
        # exports "" must never silently kill wakes on a watch deployment.
        # The hook already behaves this way (`transport && transport !== "watch"`
        # is falsy on ""); this case pins it so a "tidying" change to that
        # condition surfaces as a red rather than a silent loss of wakes.
        # NOTE this is a different question from the rename skill's own
        # ${FLEET_TRANSPORT:-watch} fallback (which transport to USE) vs.
        # this hook's own question (was a transport DECLARED at all) — the
        # two consumers apply their own independent defaults and may
        # legitimately differ in value even though, since the fleet-watch-
        # transport-migration charter (2026-08-06), both currently resolve
        # empty to "watch".
        ("transport empty string", ""),
    )
    for label, transport in cases:
        marker = work / f"arm_{label.replace(' ', '_')}"
        marker.unlink(missing_ok=True)
        proc = run_hook(HOOK, env=_env(marker, cli, transport=transport))
        res.check(marker.exists(), f"armed ({label}) spawns the CLI", "the stub never ran")
        res.check(proc.returncode == 0, f"armed ({label}) passes through exit 0", f"exit {proc.returncode}")


def _argv_stub(directory: Path, name: str) -> Path:
    """A stand-in CLI that records the argv it was invoked with (sans argv[0])."""
    lines = [
        "#!/usr/bin/env python3",
        "import json, os, sys",
        f"marker = os.environ.get({MARKER_ENV!r})",
        "if marker:",
        "    open(marker, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))",
        "sys.exit(0)",
    ]
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def check_bounded_wait_argv(res: Results, work: Path) -> None:
    """The wait must be bounded: `--max-wait` always reaches the CLI.

    The default is pinned by value on purpose -- it is a coordination constant
    that must stay under the prompt-cache TTL (see the hook's docstring), so a
    change to it should surface here as a deliberate red, not slip through.
    """
    cli = _argv_stub(work, "argv_stub")
    default = "2400"
    cases: tuple[tuple[str, str | None, str, bool], ...] = (
        ("no override", None, default, False),
        ("valid override", "90", "90", False),
        ("malformed override", "soon", default, True),
        ("zero override", "0", default, True),
        ("negative override", "-5", default, True),
    )
    for label, override, expected, expect_note in cases:
        marker = work / f"argv_{label.replace(' ', '_')}"
        marker.unlink(missing_ok=True)
        env = _env(marker, cli)
        if override is not None:
            env["AGENT_WAKE_MAX_WAIT_S"] = override
        proc = run_hook(HOOK, env=env)
        argv = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
        res.check(
            argv == ["wake", "--max-wait", expected],
            f"bounded wait ({label}): CLI argv is ['wake', '--max-wait', {expected!r}]",
            f"got {argv!r}",
        )
        res.check(proc.returncode == 0, f"bounded wait ({label}) exits 0", f"exit {proc.returncode}")
        if expect_note:
            res.check(
                proc.stderr.startswith(NOTE_PREFIX) and proc.stderr.strip().count("\n") == 0,
                f"bounded wait ({label}) falls back LOUDLY with a one-line note",
                f"got {proc.stderr[:120]!r}",
            )
        else:
            res.check(proc.stderr == "", f"bounded wait ({label}) is silent on stderr", f"got {proc.stderr[:80]!r}")


def check_one_bit_claim(res: Results, work: Path) -> None:
    """The child's output must not reach the session by any route."""
    cli = _stub(work, "chatty_wake_stub", exit_code=WAKE_SIGNAL, chatty=True)
    marker = work / "chatty_marker"
    proc = run_hook(HOOK, env=_env(marker, cli))

    res.check(marker.exists(), "one-bit case actually spawned the CLI", "the stub never ran")
    res.check(proc.returncode == WAKE_SIGNAL, "wake signal is propagated as exit 2", f"exit {proc.returncode}")
    res.check(proc.stdout == "", "wake path writes nothing to stdout", f"got {proc.stdout[:80]!r}")

    combined = proc.stdout + proc.stderr
    for secret in (SECRET_STDOUT, SECRET_STDERR):
        res.check(secret not in combined, "child output never reaches the hook's streams", f"leaked {secret!r}")
    # Substring checks alone could miss a mangled relay, so also bound the size:
    # the nudge is a single fixed sentence, not a transcript.
    res.check(
        len(proc.stderr) < 400,
        "wake nudge is a short fixed message",
        f"stderr was {len(proc.stderr)} bytes",
    )
    res.check(
        proc.stderr.strip().count("\n") == 0,
        "wake nudge is a single line",
        f"got {proc.stderr!r}",
    )


def check_nudge_is_compiled_in(res: Results, work: Path) -> None:
    """Differential proof: two very different children yield the identical nudge."""
    quiet = _stub(work, "quiet_wake_stub", exit_code=WAKE_SIGNAL)
    chatty = _stub(work, "loud_wake_stub", exit_code=WAKE_SIGNAL, chatty=True)
    first = run_hook(HOOK, env=_env(work / "n1", quiet))
    second = run_hook(HOOK, env=_env(work / "n2", chatty))
    res.check(
        first.stderr == second.stderr and first.stderr != "",
        "the nudge is a compiled-in literal, independent of the child",
        f"{first.stderr[:80]!r} != {second.stderr[:80]!r}",
    )
    res.check(
        first.returncode == second.returncode == WAKE_SIGNAL,
        "both wake cases exit 2",
        f"{first.returncode} / {second.returncode}",
    )


def check_never_traps_the_session(res: Results, work: Path) -> None:
    """Every non-wake outcome must degrade to exit 0."""
    missing = work / "does_not_exist_at_all"
    not_executable = work / "not_executable"
    not_executable.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    not_executable.chmod(0o644)

    cases: tuple[tuple[str, Path, bool], ...] = (
        ("clean exit 0", _stub(work, "exit0_stub", exit_code=0), False),
        ("arbitrary nonzero", _stub(work, "exit7_stub", exit_code=7), True),
        ("killed by signal", _stub(work, "signal_stub", exit_code=None), True),
        ("executable missing", missing, True),
        ("file not executable", not_executable, True),
    )
    for label, cli, expect_note in cases:
        proc = run_hook(HOOK, env=_env(work / f"t_{label.replace(' ', '_')}", cli))
        res.check(proc.returncode == 0, f"non-wake outcome exits 0 ({label})", f"exit {proc.returncode}")
        res.check(proc.stdout == "", f"non-wake outcome writes no stdout ({label})", f"got {proc.stdout[:80]!r}")
        if expect_note:
            res.check(
                proc.stderr.startswith(NOTE_PREFIX),
                f"failure note is fixed-format ({label})",
                f"got {proc.stderr[:80]!r}",
            )
            res.check(
                proc.stderr.strip().count("\n") == 0,
                f"failure note is one line ({label})",
                f"got {proc.stderr!r}",
            )
        else:
            res.check(proc.stderr == "", f"clean exit is silent ({label})", f"got {proc.stderr[:80]!r}")

    seven = run_hook(HOOK, env=_env(work / "t_seven_status", _stub(work, "exit7b_stub", exit_code=7)))
    res.check("7" in seven.stderr, "the disclosed variable part is the numeric status", f"got {seven.stderr!r}")


def check_hook_writes_no_files(res: Results, work: Path) -> None:
    """SECURITY.md: 'No hook writes a file as an action of its own.'"""
    probe = work / "write_probe"
    probe.mkdir()
    cli = _stub(probe, "probe_stub", exit_code=WAKE_SIGNAL)
    marker = probe / "probe_marker"
    before = {path.name for path in probe.iterdir()}
    run_hook(HOOK, env=_env(marker, cli))
    after = {path.name for path in probe.iterdir()}
    created = after - before - {marker.name}
    res.check(not created, "the hook creates no files of its own", f"appeared: {sorted(created)}")


def main() -> int:
    preflight()
    res = Results("coordination-hooks — wake waiter")
    with tempfile.TemporaryDirectory(prefix="coordination-hooks-wake-") as raw:
        work = Path(raw)
        check_disarm_matrix(res, work)
        check_arm_matrix(res, work)
        check_bounded_wait_argv(res, work)
        check_one_bit_claim(res, work)
        check_nudge_is_compiled_in(res, work)
        check_never_traps_the_session(res, work)
        check_hook_writes_no_files(res, work)
    return res.finish()


if __name__ == "__main__":
    sys.exit(main())
