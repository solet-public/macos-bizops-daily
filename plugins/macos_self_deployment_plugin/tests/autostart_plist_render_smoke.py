#!/usr/bin/env python3
"""Standalone smoke for AutostartManager._render_plist (no pytest).

Validates the Option-B LaunchAgent plist shape (2026-06-28) + the §5
CWD-hygiene fix (design ``2026-06-27_true_local_blue_green_materialized_
artifacts_design.md``):

* ``ProgramArguments`` launches the colour-agnostic supervisor module
  (``-m macos_self_deployment_plugin.supervisor``), NOT ``ananta.cli``
  directly — the launchd-managed process is never a solet colour.
* ``KeepAlive`` is an unconditional ``<true/>`` (a bool, not a dict): the
  supervisor is an infinite loop, so any exit while loaded means restart.
  The interim ``Crashed``/``SuccessfulExit`` dict (and the earlier Slice-4
  ``PathState`` predicate) are gone — no solet colour is launchd-managed, so
  the ghost-respawn class is structurally impossible. The ``.draining``
  sentinel does NOT appear in the plist (it gates the *supervisor*, not
  launchd).
* ``WorkingDirectory`` is the out-of-tree runtime dir
  (``get_runtime_dir(name)``), NOT the repo root — a managed process
  must never have its CWD set to a code tree, or a stray relative-path
  write would mutate it (§5).
* ``RunAtLoad=true`` retained — cold-start-at-login.

Also asserts the plist is valid XML and parseable via the stdlib
``plistlib``.

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/autostart_plist_render_smoke.py
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.runtime import get_runtime_dir  # noqa: E402
from macos_self_deployment_plugin.autostart_manager import AutostartManager  # noqa: E402
from macos_self_deployment_plugin.constants import AUTOSTART_PATH_ENV  # noqa: E402

# A value no real PATH can plausibly hold, used to MANUFACTURE a distinguishable
# ambient environment for the anti-capture leg rather than hoping the real one
# happens to be distinguishable from the rendered literal.
_PATH_CAPTURE_SENTINEL = "/sentinel-capture-canary"

# A non-existent, non-/tmp scratch path. Since the §4.5 role-1 fix
# (2026-06-28) the rendered interpreter is the baked LITERAL ``current``
# symlink path (from the default releases_root), no longer derived from
# ``project_root``'s ``.venv`` — so ``project_root`` now only feeds the
# profile path here. Per the operator no-/tmp rule, scratch lives under
# ``~/.ananta/``, never ``/tmp``. (The interpreter-path behavior itself is
# covered by ``autostart_seed_smoke.py``.)
_FAKE_PROJECT_ROOT = (
    Path.home() / ".ananta" / "_smoke_scratch" / "autostart-render-fake-root"
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


def _render() -> tuple[bytes, str]:
    mgr = AutostartManager(
        solet_name="example",
        project_root=_FAKE_PROJECT_ROOT,
    )
    raw = mgr._render_plist()  # noqa: SLF001
    return raw, raw.decode("utf-8")


def test_render_plist_xml_strings_present() -> None:
    _, body = _render()
    for assertion, label in (
        ("<key>KeepAlive</key>" in body,
         "KeepAlive key present in plist body"),
        ("<key>KeepAlive</key>\n  <true/>" in body,
         "KeepAlive is an unconditional <true/> (bool, not a dict)"),
        ("<key>KeepAlive</key>\n  <dict>" not in body,
         "KeepAlive is NOT a dict (Crashed/SuccessfulExit dance is gone)"),
        ("<key>Crashed</key>" not in body,
         "no Crashed key (no solet colour is launchd-managed)"),
        ("<key>SuccessfulExit</key>" not in body,
         "no SuccessfulExit key (exit-code dance obsolete)"),
        ("<string>macos_self_deployment_plugin.supervisor</string>" in body,
         "ProgramArguments runs the colour-agnostic supervisor module"),
        ("<string>ananta.cli</string>" not in body,
         "ProgramArguments does NOT launch ananta.cli directly"),
        ("<key>PathState</key>" not in body,
         "no PathState entry"),
        (".draining" not in body,
         "drain sentinel not referenced in plist (gates the supervisor, not launchd)"),
        ("<key>RunAtLoad</key>\n  <true/>" in body,
         "RunAtLoad=true retained (cold-start-at-login)"),
    ):
        _check(assertion, label)


def test_render_plist_parses_as_dict() -> None:
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    _check(isinstance(parsed, dict), "plist parses as a dict")
    if not isinstance(parsed, dict):
        return
    _check(
        parsed.get("Label") == "local.solet.example",
        f"Label = 'local.solet.example' (got {parsed.get('Label')!r})",
    )


def test_parsed_keepalive_is_unconditional_true() -> None:
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    keep_alive = parsed.get("KeepAlive") if isinstance(parsed, dict) else None
    _check(
        keep_alive is True,
        f"parsed KeepAlive is an unconditional bool True (got {keep_alive!r})",
    )


def test_parsed_program_arguments_launch_supervisor() -> None:
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    args = parsed.get("ProgramArguments") if isinstance(parsed, dict) else None
    if not isinstance(args, list):
        _check(False, "parsed ProgramArguments is a list")
        return
    _check(
        "macos_self_deployment_plugin.supervisor" in args,
        f"ProgramArguments includes the supervisor module (got {args!r})",
    )
    _check(
        "ananta.cli" not in args,
        "ProgramArguments does NOT launch ananta.cli directly",
    )
    _check(
        args[:2] == [args[0], "-m"] and args[0].endswith("/current/venv/bin/python3"),
        f"interpreter is the literal current/venv/bin/python3, run with -m (got {args[:3]!r})",
    )


def test_working_directory_out_of_tree() -> None:
    """§5: WorkingDirectory must be the out-of-tree runtime dir, never a code tree."""
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    working_dir = parsed.get("WorkingDirectory") if isinstance(parsed, dict) else None
    expected = str(get_runtime_dir("example"))
    _check(
        working_dir == expected,
        f"WorkingDirectory == get_runtime_dir('example') "
        f"(expected {expected!r}, got {working_dir!r})",
    )
    _check(
        working_dir != str(_FAKE_PROJECT_ROOT),
        "WorkingDirectory is NOT the project_root (no code-tree CWD)",
    )
    _check(
        isinstance(working_dir, str) and str(REPO_ROOT) not in working_dir,
        f"WorkingDirectory is outside the repo tree (got {working_dir!r})",
    )


def test_environment_variables_carry_homebrew_path() -> None:
    """§39.2 (adopter-reported, field-verified): a launchd process with no PATH
    key inherits the bare ``/usr/bin:/bin:/usr/sbin:/sbin``, so in-daemon
    ``shutil.which("tmux")`` returns None even with tmux installed at
    ``/opt/homebrew/bin/tmux`` -- the swap-durable tmux fleet host is
    unreachable from the daemon that has to spawn it.

    Asserted on the PARSED plist, not a substring of the XML: a rendered key
    that launchd cannot parse would still satisfy a substring check.
    """
    raw, _ = _render()
    parsed = plistlib.loads(raw)
    env = parsed.get("EnvironmentVariables") if isinstance(parsed, dict) else None
    if not isinstance(env, dict):
        _check(False, "parsed EnvironmentVariables is a dict")
        return
    path = env.get("PATH")
    # FAILING MUTATION: drop the PATH line from _render_plist -> reds here.
    _check(
        isinstance(path, str) and bool(path),
        f"EnvironmentVariables carries a PATH (daemon cannot find Homebrew tmux without it), got {path!r}",
    )
    # FAILING MUTATION: reorder the literal so /usr/bin precedes
    # /opt/homebrew/bin, or drop either Homebrew prefix -> reds here. Exact
    # equality, not per-component containment: a partial match stays green
    # while the daemon still resolves a system binary ahead of the Homebrew one.
    _check(
        path == AUTOSTART_PATH_ENV
        == "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        f"PATH is the exact deterministic literal, both Homebrew prefixes first (got {path!r})",
    )
    _check(
        env.get("SOLET_NAME") == "example",
        f"SOLET_NAME still rendered alongside PATH (got {env.get('SOLET_NAME')!r})",
    )
    _check(
        env.get("FLEET_SESSION_HOST") == "tmux",
        "FLEET_SESSION_HOST is pinned to tmux for LaunchAgent-spawned sessions",
    )


def test_path_is_not_an_ambient_capture() -> None:
    """Anti-capture negative control, ENVIRONMENT-INDEPENDENT by construction.

    The previous form asserted ``path != os.environ["PATH"]``, which relied on
    the ambient PATH happening to differ from the rendered literal. That is the
    same weak class that made this smoke's ``github_midwife_plugin`` sibling
    false-positive in the born-clone publish gate's declared-minimum
    environment (there the sibling used substring containment and ambient
    ``/usr/bin:/bin`` IS contained in the correct literal; this equality form
    did not fail, but rests on the same assumption). Both are now rebuilt to
    MANUFACTURE a distinguishable ambient value instead of hoping for one.

    FAILING MUTATION: make ``_render_plist`` emit ``os.environ["PATH"]``
    instead of ``AUTOSTART_PATH_ENV`` -> the sentinel appears in the render and
    this reds, deterministically, on every machine including the constrained
    gate environment.

    Why this matters beyond the smoke: the plist is byte-compared by
    ``_classify_install_prior``, so a machine-varying render would make every
    install read as ``present_but_stale`` forever.
    """
    previous = os.environ.get("PATH")
    os.environ["PATH"] = _PATH_CAPTURE_SENTINEL
    try:
        raw, body = _render()
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous
    parsed = plistlib.loads(raw)
    env = parsed.get("EnvironmentVariables") if isinstance(parsed, dict) else None
    rendered_path = env.get("PATH") if isinstance(env, dict) else None
    _check(
        _PATH_CAPTURE_SENTINEL not in body,
        "PATH is a fixed literal, NOT a capture of the ambient $PATH "
        "(rendered under a sentinel PATH; a capturing renderer would leak it)",
    )
    _check(
        rendered_path == AUTOSTART_PATH_ENV,
        "the render still carries the module constant while the ambient PATH is "
        f"the sentinel — the renderer ignores the environment (got {rendered_path!r})",
    )
    _check(
        os.environ.get("PATH") == previous,
        "the ambient PATH is restored exactly after the sentinel render",
    )


def main() -> int:
    print("=== autostart_plist_render_smoke (Option-B supervisor KeepAlive + §5 CWD + §39.2 PATH) ===")
    test_render_plist_xml_strings_present()
    test_render_plist_parses_as_dict()
    test_parsed_keepalive_is_unconditional_true()
    test_parsed_program_arguments_launch_supervisor()
    test_working_directory_out_of_tree()
    test_environment_variables_carry_homebrew_path()
    test_path_is_not_an_ambient_capture()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
