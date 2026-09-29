"""Regression smoke: a colourless process must not become a second live colour.

``launchctl load`` of the primary LaunchAgent while the blue-green router already
has a healthy active colour starts a second ``ananta.cli`` with no
``SOLET_COLOR``.  The plugin defaulted that process to blue, so it registered as
an inactive colour that never drained -- and, worse, its cold-start scrub deleted
the live active instance's socket and port files before it ever looked at the
router.  The production seam is ``MacosSelfDeploymentPlugin.prepare_for_readiness``.

Cases, all through the real ``prepare_for_readiness`` with a scripted router:

* live active colour, no ``SOLET_COLOR``  -> exit 0 (KeepAlive.SuccessfulExit=false
  must not relaunch it) and the live instance's runtime files are untouched;
* no router answering                     -> a genuine cold boot still proceeds;
* explicit ``SOLET_COLOR``                -> a swap candidate still proceeds;
* stale active binding that the router GC clears (crash relaunch) -> proceeds;
* the roster is already serving (a live plugin re-install) -> never exits;
* active binding whose heartbeat never advances and never clears -> fails loudly.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from macos_self_deployment_plugin import plugin as plugin_module  # noqa: E402
from macos_self_deployment_plugin import stale_runtime_cleanup  # noqa: E402

_NAME = "stray"
_LIVE_ID = "solet-green-live0001"
_LIVE_FILES = (f"{_NAME}.sock", f"{_NAME}.bridge.port", f"{_NAME}.rest.port")
_FAST_POLL_SECONDS = 0.005
_SHORT_WINDOW_SECONDS = 0.25


def _load_guard() -> ModuleType | None:
    """The guard module, or ``None`` before the fix exists (the RED run)."""
    try:
        return importlib.import_module("macos_self_deployment_plugin.stray_start_guard")
    except ImportError:
        return None


class ScriptedRouter:
    """A router whose ``status`` reply is a function of how often it was asked."""

    def __init__(
        self,
        *,
        answering: bool = True,
        active: bool = True,
        heartbeat_advances: bool = True,
        clears_after_calls: int | None = None,
    ) -> None:
        self.answering = answering
        self.active = active
        self.heartbeat_advances = heartbeat_advances
        self.clears_after_calls = clears_after_calls
        self.calls = 0

    def status(self, socket_path: Path) -> dict[str, object] | None:
        del socket_path
        self.calls += 1
        if not self.answering:
            return None
        cleared = self.clears_after_calls is not None and self.calls > self.clears_after_calls
        if not self.active or cleared:
            return {"active_color": None, "active_instance_id": None, "colors": [], "drain_entries": []}
        beat = 1000.0 + (self.calls * 10.0 if self.heartbeat_advances else 0.0)
        return {
            "active_color": "green",
            "active_instance_id": _LIVE_ID,
            "colors": [
                {
                    "color": "green",
                    "port": 8123,
                    "instance_id": _LIVE_ID,
                    "status": "active",
                    "last_heartbeat": beat,
                    "streamable_port": None,
                }
            ],
            "drain_entries": [],
        }


class Outcome:
    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.error: BaseException | None = None
        self.self_color: str = ""
        self.surviving_files: tuple[str, ...] = ()


@contextlib.contextmanager
def _scripted_runtime(
    router: ScriptedRouter, explicit_color: str | None, guard: ModuleType | None,
) -> Iterator[Path]:
    """A temp runtime dir holding a live instance's files, with the router scripted."""
    with tempfile.TemporaryDirectory(prefix="stray-guard-") as temp_dir:
        runtime = Path(temp_dir) / ".ananta" / "runtime"
        runtime.mkdir(parents=True)
        for filename in _LIVE_FILES:
            (runtime / filename).write_text("live", encoding="utf-8")
        saved_env = {key: os.environ.get(key) for key in ("SOLET_NAME", "SOLET_COLOR", "SOLET_INSTANCE_ID")}
        saved = (
            stale_runtime_cleanup.runtime_dir,
            stale_runtime_cleanup.router_mgmt_status,
            plugin_module._wait_for_router_socket,
            plugin_module._runtime_dir,
        )
        guard_saved = _patch_guard_timing(guard)
        try:
            stale_runtime_cleanup.runtime_dir = lambda: runtime
            stale_runtime_cleanup.router_mgmt_status = router.status  # type: ignore[assignment]
            plugin_module._wait_for_router_socket = lambda _path: None  # type: ignore[assignment]
            plugin_module._runtime_dir = lambda: runtime  # type: ignore[assignment]
            os.environ["SOLET_NAME"] = _NAME
            os.environ.pop("SOLET_INSTANCE_ID", None)
            if explicit_color is None:
                os.environ.pop("SOLET_COLOR", None)
            else:
                os.environ["SOLET_COLOR"] = explicit_color
            yield runtime
        finally:
            (
                stale_runtime_cleanup.runtime_dir,
                stale_runtime_cleanup.router_mgmt_status,
                plugin_module._wait_for_router_socket,
                plugin_module._runtime_dir,
            ) = saved
            _unpatch_guard_timing(guard, guard_saved)
            for key, value in saved_env.items():
                _restore_env(key, value)


def _build_plugin(serving: bool) -> plugin_module.MacosSelfDeploymentPlugin:
    instance = plugin_module.MacosSelfDeploymentPlugin()
    instance._reconcile_releases = lambda: None  # type: ignore[method-assign]
    instance.set_ready = lambda: None  # type: ignore[method-assign]
    if serving:
        instance.orchestrator_ref = SimpleNamespace(  # type: ignore[assignment]
            plugin_manager=SimpleNamespace(_is_serving=True),
        )
    return instance


def _run(
    router: ScriptedRouter,
    *,
    explicit_color: str | None = None,
    serving: bool = False,
    guard: ModuleType | None,
) -> Outcome:
    outcome = Outcome()
    with _scripted_runtime(router, explicit_color, guard) as runtime:
        instance = _build_plugin(serving)
        try:
            instance.prepare_for_readiness()
        except SystemExit as exc:
            outcome.exit_code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001 -- the smoke records the failure mode
            outcome.error = exc
        outcome.self_color = instance._self_color
        outcome.surviving_files = tuple(f for f in _LIVE_FILES if (runtime / f).exists())
    return outcome


def _patch_guard_timing(guard: ModuleType | None) -> tuple[float, float] | None:
    if guard is None:
        return None
    previous = (guard.STRAY_GUARD_POLL_SECONDS, guard.STRAY_GUARD_WINDOW_SECONDS)
    guard.STRAY_GUARD_POLL_SECONDS = _FAST_POLL_SECONDS
    guard.STRAY_GUARD_WINDOW_SECONDS = _SHORT_WINDOW_SECONDS
    return previous


def _unpatch_guard_timing(guard: ModuleType | None, previous: tuple[float, float] | None) -> None:
    if guard is not None and previous is not None:
        guard.STRAY_GUARD_POLL_SECONDS, guard.STRAY_GUARD_WINDOW_SECONDS = previous


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def _case_live_active_colour_exits_zero(guard: ModuleType | None) -> bool:
    outcome = _run(ScriptedRouter(), guard=guard)
    return (
        outcome.exit_code == 0
        and outcome.error is None
        and outcome.surviving_files == _LIVE_FILES
        and outcome.self_color == ""
    )


def _case_no_router_still_boots(guard: ModuleType | None) -> bool:
    outcome = _run(ScriptedRouter(answering=False), guard=guard)
    return outcome.exit_code is None and outcome.error is None and outcome.self_color == "blue"


def _case_explicit_colour_still_boots(guard: ModuleType | None) -> bool:
    outcome = _run(ScriptedRouter(), explicit_color="blue", guard=guard)
    return outcome.exit_code is None and outcome.error is None and outcome.self_color == "blue"


def _case_stale_active_binding_boots_after_gc(guard: ModuleType | None) -> bool:
    router = ScriptedRouter(heartbeat_advances=False, clears_after_calls=3)
    outcome = _run(router, guard=guard)
    return outcome.exit_code is None and outcome.error is None and outcome.self_color == "blue"


def _case_serving_roster_never_exits(guard: ModuleType | None) -> bool:
    outcome = _run(ScriptedRouter(), serving=True, guard=guard)
    return outcome.exit_code is None and outcome.error is None and outcome.self_color == "blue"


def _case_undecidable_binding_fails_loudly(guard: ModuleType | None) -> bool:
    outcome = _run(ScriptedRouter(heartbeat_advances=False), guard=guard)
    return outcome.exit_code is None and isinstance(outcome.error, RuntimeError)


_CASES: tuple[tuple[str, Callable[[ModuleType | None], bool]], ...] = (
    ("live active colour, no SOLET_COLOR: exit 0, live files untouched", _case_live_active_colour_exits_zero),
    ("no router answering: cold boot proceeds", _case_no_router_still_boots),
    ("explicit SOLET_COLOR: candidate boot proceeds", _case_explicit_colour_still_boots),
    ("stale active binding cleared by router GC: crash relaunch proceeds", _case_stale_active_binding_boots_after_gc),
    ("roster already serving: live re-install never exits", _case_serving_roster_never_exits),
    ("active binding that neither beats nor clears: fails loudly", _case_undecidable_binding_fails_loudly),
)


def main() -> int:
    guard = _load_guard()
    failures = 0
    for label, case in _CASES:
        passed = case(guard)
        failures += 0 if passed else 1
        print(f"{label}: {'PASS' if passed else 'FAIL'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
