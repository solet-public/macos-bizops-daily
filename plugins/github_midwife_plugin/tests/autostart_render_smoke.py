"""Slice F smoke — the simple genesis LaunchAgent plist renderer.

Renders into a tmpfs `plist_dir` + a tmpfs `home_dir` (so
`~/.ananta/runtime/<name>` resolves inside the fixture, never the real
`~`); `launchctl` is a fully mocked `run` callable -- no real
`/bin/launchctl` invoked. Pins every build-spec §6.2 load-bearing plist
field, the install/uninstall/status idempotency contract, and the
`ananta.core.runtime.get_runtime_dir` divergence this module
deliberately avoids.

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/autostart_render_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.autostart import (
    _PATH_ENV,
    AutostartError,
    LaunchctlObservationError,
    SimpleAutostartRenderer,
)

# A value no real PATH can plausibly hold, used to MANUFACTURE a
# distinguishable ambient environment for the anti-capture leg below rather
# than hoping the real one happens to be distinguishable.
_PATH_CAPTURE_SENTINEL = "/sentinel-capture-canary"

_CHECKS_RUN: list[str] = []
_LAUNCHCTL_TIMEOUT_S_FOR_SMOKE = 10.0


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _fake_completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _FakeLaunchctl:
    """Tracks whether the label is 'loaded' so `list`/`load`/`unload` behave consistently."""

    def __init__(self) -> None:
        self.loaded = False
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        verb = cmd[1] if len(cmd) > 1 else ""
        if verb == "list":
            if self.loaded:
                return _fake_completed(0)
            # Real launchctl's documented not-found signal (not just any
            # nonzero exit) -- the mock must match launchctl's actual
            # contract, not the code's prior (wrong) assumption that any
            # nonzero exit meant "not loaded".
            label = cmd[2] if len(cmd) > 2 else ""
            return _fake_completed(113, stderr=f'Could not find service "{label}" in domain for ...')
        if verb == "load":
            self.loaded = True
            return _fake_completed(0)
        if verb == "unload":
            self.loaded = False
            return _fake_completed(0)
        return _fake_completed(1, stderr=f"unexpected launchctl verb {verb!r}")


def _make_renderer(root: Path, fake_launchctl: _FakeLaunchctl) -> SimpleAutostartRenderer:
    return SimpleAutostartRenderer(
        solet_name="testhum",
        clone_root=root / "clone",
        plist_dir=root / "LaunchAgents",
        home_dir=root / "home",
        run=fake_launchctl,
    )


def _check_label_and_paths(root: Path) -> None:
    fake_launchctl = _FakeLaunchctl()
    renderer = _make_renderer(root, fake_launchctl)
    _check("label matches local.solet.<name>", renderer.label == "local.solet.testhum", renderer.label)
    _check(
        "runtime_dir is home_dir/.ananta/runtime/<name> -- NOT the shared ananta.core.runtime.get_runtime_dir() path",
        renderer.runtime_dir == root / "home" / ".ananta" / "runtime" / "testhum",
        str(renderer.runtime_dir),
    )
    _check(
        "plist_path is <plist_dir>/local.solet.<name>.plist",
        renderer.plist_path == root / "LaunchAgents" / "local.solet.testhum.plist",
        str(renderer.plist_path),
    )


def _check_rendered_plist_fields(root: Path) -> None:
    fake_launchctl = _FakeLaunchctl()
    renderer = _make_renderer(root, fake_launchctl)
    xml = renderer._render_plist().decode("utf-8")  # noqa: SLF001

    clone = root / "clone"
    expected_interpreter = str(clone / ".venv" / "bin" / "python3")
    expected_profile = str(clone / "profile")
    expected_working_dir = str(renderer.runtime_dir)

    _check("plist sets Label", "<string>local.solet.testhum</string>" in xml, xml)
    _check(
        "plist EnvironmentVariables sets SOLET_NAME (boot fast-fails without it)",
        "<key>SOLET_NAME</key>\n    <string>testhum</string>" in xml,
        xml,
    )
    _check(
        "plist WorkingDirectory is the per-solet runtime dir, not the clone root",
        f"<key>WorkingDirectory</key>\n  <string>{expected_working_dir}</string>" in xml
        and str(clone) not in xml.split("WorkingDirectory")[1].split("</string>")[0],
        xml,
    )
    _check(
        "ProgramArguments uses the clone's own venv python3 (absolute path)",
        f"<string>{expected_interpreter}</string>" in xml,
        xml,
    )
    _check(
        "ProgramArguments launches ananta.cli directly (no supervisor module)",
        "<string>-m</string>\n    <string>ananta.cli</string>" in xml,
        xml,
    )
    _check(
        "ProgramArguments --app-home is the clone's absolute profile dir, not a bare 'profile' string",
        f"<string>--app-home</string>\n    <string>{expected_profile}</string>" in xml,
        xml,
    )
    # §39.2 (adopter-reported, field-verified): a launchd process with no PATH
    # key gets the bare /usr/bin:/bin:/usr/sbin:/sbin and cannot see
    # /opt/homebrew/bin/tmux even when tmux is installed.
    # FAILING MUTATION: drop the PATH line from SimpleAutostartRenderer.
    # _render_plist -> this leg reds (it is the only assertion on PATH presence).
    _check(
        "plist EnvironmentVariables sets PATH (daemon cannot find Homebrew tmux without it)",
        "<key>PATH</key>\n    <string>" in xml,
        xml,
    )
    # FAILING MUTATION: reorder the literal to put /usr/bin ahead of
    # /opt/homebrew/bin, or drop either Homebrew prefix -> this leg reds.
    # Asserted as the EXACT value against the MODULE's own constant (not a
    # hand-copied string, which can silently drift from the renderer): a
    # per-component substring test would stay green while the daemon still
    # resolved the system binary first.
    _check(
        "plist PATH is the exact deterministic literal, both Homebrew prefixes ahead of the system defaults",
        f"<key>PATH</key>\n    <string>{_PATH_ENV}</string>" in xml,
        xml,
    )
    _check(
        _PATH_ENV.startswith("/opt/homebrew/bin:") and ":/usr/bin:" in _PATH_ENV,
        "the module literal itself puts a Homebrew prefix ahead of the system defaults",
        _PATH_ENV,
    )
    _check("plist sets RunAtLoad true", "<key>RunAtLoad</key>\n  <true/>" in xml, xml)
    _check(
        "KeepAlive is the SIMPLE crash-restart form (SuccessfulExit=false), not an unconditional <true/>",
        "<key>KeepAlive</key>\n  <dict>\n    <key>SuccessfulExit</key>\n    <false/>\n  </dict>" in xml,
        xml,
    )


def _check_path_is_not_an_ambient_capture(root: Path) -> None:
    """The anti-capture negative control for §39.2's PATH, rebuilt 2026-08-10
    to be ENVIRONMENT-INDEPENDENT.

    The original leg asserted ``os.environ["PATH"] not in xml`` — it relied on
    the ambient PATH happening to be distinguishable from the rendered literal.
    That held on a developer machine and FALSE-POSITIVED against a correct
    render in the born-clone publish gate's declared-minimum environment, where
    ambient PATH is ``/usr/bin:/bin`` — a literal SUBSTRING of the correct
    output ``/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:
    /usr/sbin:/sbin``. The smoke went red on code that was right, and it
    blocked a seed mint.

    The fix is to MANUFACTURE the distinguishable value instead of hoping for
    one: render with PATH set to a sentinel no real environment holds, and
    assert the sentinel is absent. A renderer that captured the ambient PATH
    would emit the sentinel — on every machine, including the constrained one —
    so the control is now valid under ANY ambient PATH, including one that
    equals or is contained in the literal.

    FAILING MUTATION: change ``SimpleAutostartRenderer._render_plist`` to emit
    ``os.environ["PATH"]`` instead of ``_PATH_ENV`` -> the sentinel appears in
    the render and this leg reds, deterministically, everywhere.
    """
    fake_launchctl = _FakeLaunchctl()
    renderer = _make_renderer(root, fake_launchctl)
    previous = os.environ.get("PATH")
    os.environ["PATH"] = _PATH_CAPTURE_SENTINEL
    try:
        xml = renderer._render_plist().decode("utf-8")  # noqa: SLF001
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous
    _check(
        _PATH_CAPTURE_SENTINEL not in xml,
        "plist PATH is a fixed literal, NOT a capture of the ambient $PATH "
        "(rendered under a sentinel PATH; a capturing renderer would leak it)",
        xml,
    )
    _check(
        f"<key>PATH</key>\n    <string>{_PATH_ENV}</string>" in xml,
        "and the render still carries the module literal even while the ambient "
        "PATH is the sentinel — proving the renderer ignores the environment",
        xml,
    )
    _check(
        os.environ.get("PATH") == previous,
        "the ambient PATH is restored exactly after the sentinel render",
        f"{os.environ.get('PATH')!r} vs {previous!r}",
    )


def _check_install_load_idempotent_reload(root: Path) -> None:
    fake_launchctl = _FakeLaunchctl()
    renderer = _make_renderer(root, fake_launchctl)

    result1 = renderer.install()
    _check("first install reports success + prior_state absent", result1.status == "success" and result1.prior_state == "absent", str(result1))
    _check("first install writes the plist file", renderer.plist_path.is_file())
    _check("first install creates the runtime dir", renderer.runtime_dir.is_dir())
    _check("first install calls launchctl load (no unload -- nothing was loaded yet)", fake_launchctl.calls[-1][1] == "load", str(fake_launchctl.calls))

    calls_before_second = len(fake_launchctl.calls)
    result2 = renderer.install()
    _check(
        "second install (already current) unloads THEN reloads -- idempotent, not a stale no-op",
        result2.status == "success" and result2.prior_state == "present_already_current",
        str(result2),
    )
    new_calls = fake_launchctl.calls[calls_before_second:]
    verbs = [c[1] for c in new_calls]
    _check(
        "second install's launchctl sequence probes (list), then unloads, then reloads",
        verbs == ["list", "unload", "load"],
        str(verbs),
    )


def _check_status_reflects_observed_state(root: Path) -> None:
    fake_launchctl = _FakeLaunchctl()
    renderer = _make_renderer(root, fake_launchctl)

    before = renderer.status()
    _check("status before install is not_installed", before.status == "not_installed", str(before))

    renderer.install()
    after = renderer.status()
    _check("status after install is installed_loaded", after.status == "installed_loaded", str(after))


def _check_uninstall_idempotent(root: Path) -> None:
    fake_launchctl = _FakeLaunchctl()
    renderer = _make_renderer(root, fake_launchctl)

    absent_result = renderer.uninstall()
    _check(
        "uninstall on an absent LaunchAgent succeeds with prior_state absent (idempotent)",
        absent_result.status == "success" and absent_result.prior_state == "absent",
        str(absent_result),
    )

    renderer.install()
    _check("plist exists after install (precondition for the next check)", renderer.plist_path.is_file())
    present_result = renderer.uninstall()
    _check(
        "uninstall on a loaded LaunchAgent unloads + removes the plist",
        present_result.status == "success"
        and present_result.prior_state == "present_and_loaded"
        and not renderer.plist_path.exists(),
        f"result={present_result!r} plist_exists={renderer.plist_path.exists()}",
    )

    second_uninstall = renderer.uninstall()
    _check(
        "a second uninstall is still idempotent (absent, no error)",
        second_uninstall.status == "success" and second_uninstall.prior_state == "absent",
        str(second_uninstall),
    )


def _check_launchctl_failure_raises() -> None:
    def _always_fails(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "list":
            # A legitimate not-loaded observation, so _observe() succeeds
            # and install() proceeds to the REAL failure under test: the
            # requested `load` verb itself failing.
            return _fake_completed(113, stderr='Could not find service "x" in domain for ...')
        return _fake_completed(1, stderr="simulated launchctl failure")

    with tempfile.TemporaryDirectory() as tmp:
        renderer = _make_renderer(Path(tmp), _always_fails)  # type: ignore[arg-type]
        try:
            renderer.install()
        except AutostartError as exc:
            _check("a launchctl load failure raises AutostartError", "simulated launchctl failure" in str(exc), str(exc))
        else:
            raise SmokeFailureError("launchctl-failure-raises: install() did not raise")


def _check_status_raises_on_launchctl_execution_failure() -> None:
    """Codex F must-fix pin (a): a launchctl list EXECUTION failure (the
    subprocess itself cannot run -- launchctl missing, times out) must
    never be silently reported as "not loaded". `status()` must surface
    the failure rather than fabricate an observation it never made.
    """

    def _list_execution_fails(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError(f"simulated: {cmd[0]} binary missing")

    with tempfile.TemporaryDirectory() as tmp:
        renderer = _make_renderer(Path(tmp), _list_execution_fails)  # type: ignore[arg-type]
        try:
            renderer.status()
        except LaunchctlObservationError as exc:
            _check(
                "status() on a launchctl execution failure raises LaunchctlObservationError, "
                "not a fabricated not_installed result",
                "could not be executed" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("status-raises-on-launchctl-execution-failure: status() did not raise")


def _check_uninstall_refuses_on_launchctl_execution_failure(root: Path) -> None:
    """Codex F must-fix pin (b): on a launchctl list EXECUTION failure,
    uninstall() must REFUSE to act -- no unload attempt, plist left in
    place -- rather than reading the unobserved state as "not loaded"
    and unlinking a plist that may still back a live, loaded launchd
    job (the exact orphaned-job scenario Codex traced).
    """
    fake_launchctl = _FakeLaunchctl()
    renderer = _make_renderer(root, fake_launchctl)
    renderer.install()
    _check("plist exists after install (precondition)", renderer.plist_path.is_file())

    calls: list[list[str]] = []

    def _list_execution_fails(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=_LAUNCHCTL_TIMEOUT_S_FOR_SMOKE)

    renderer.run = _list_execution_fails  # type: ignore[assignment]
    try:
        renderer.uninstall()
    except LaunchctlObservationError:
        pass
    else:
        raise SmokeFailureError("uninstall-refuses-on-launchctl-execution-failure: uninstall() did not raise")

    _check(
        "uninstall() attempted exactly one launchctl call (the failed probe) -- never reached unload",
        len(calls) == 1 and calls[0][1] == "list",
        str(calls),
    )
    _check(
        "the plist is STILL PRESENT -- uninstall took no action on the unobserved state (fail-loud, no orphaned job)",
        renderer.plist_path.is_file(),
    )


def _check_unrecognized_nonzero_exit_is_not_coerced_to_not_loaded() -> None:
    """The discrimination boundary itself: a nonzero exit that does NOT
    carry launchctl's real not-found marker text must not be coerced
    into a legitimate not-loaded observation either -- only the exact
    documented not-found signal maps to False. Guards against a
    regression that keeps the raise for execution failures but widens
    "any nonzero" back into "not loaded".
    """

    def _weird_failure(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "list":
            return _fake_completed(1, stderr="launchd: internal error, try again")
        return _fake_completed(0)

    with tempfile.TemporaryDirectory() as tmp:
        renderer = _make_renderer(Path(tmp), _weird_failure)  # type: ignore[arg-type]
        try:
            renderer.status()
        except LaunchctlObservationError as exc:
            _check(
                "an unrecognized nonzero exit (not the documented not-found signal) raises "
                "rather than being read as not-loaded",
                "unrecognized failure shape" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("unrecognized-nonzero-exit-not-coerced: status() did not raise")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _check_label_and_paths(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_rendered_plist_fields(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_path_is_not_an_ambient_capture(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_install_load_idempotent_reload(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_status_reflects_observed_state(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_uninstall_idempotent(Path(tmp))
        _check_launchctl_failure_raises()
        _check_status_raises_on_launchctl_execution_failure()
        with tempfile.TemporaryDirectory() as tmp:
            _check_uninstall_refuses_on_launchctl_execution_failure(Path(tmp))
        _check_unrecognized_nonzero_exit_is_not_coerced_to_not_loaded()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"autostart_render_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
