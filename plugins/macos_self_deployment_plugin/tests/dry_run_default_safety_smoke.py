#!/usr/bin/env python3
"""Offline regression smoke for fail-safe macOS destructive process defaults.

The action wrappers are the JSON-transport boundary for the four destructive
macOS self-deployment verbs.  This smoke replaces every downstream operation
with a recording fake, so it proves their received ``dry_run`` value without
writing a sentinel or plist, invoking launchctl, or spawning a process.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
for _path in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ananta.interfaces.lifecycle_result_types import (  # noqa: E402
    AutostartResult,
    AutostartStatus,
    RestartResult,
    RestartStatus,
    StopSelfResult,
    StopSelfStatus,
)
from ananta.utils.dry_run import coerce_dry_run  # noqa: E402
from macos_self_deployment_plugin.plugin import MacosSelfDeploymentPlugin  # noqa: E402


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        raise AssertionError(message)
    print(f"  OK  {message}")


def _restart_result(dry_run: bool) -> RestartResult:
    return RestartResult(
        status=RestartStatus.QUEUED,
        restart_action_id="fake-restart",
        message="recorded fake",
        reason="smoke",
        expected_etag="",
        dry_run=dry_run,
    )


def _stop_result(dry_run: bool) -> StopSelfResult:
    return StopSelfResult(
        status=StopSelfStatus.DRY_RUN if dry_run else StopSelfStatus.SUCCESS,
        reason="smoke",
        duration_seconds=0.0,
        stopped_at="",
        backend_action_id="",
        dry_run=dry_run,
        message="recorded fake",
    )


def _autostart_result(verb: str, dry_run: bool) -> AutostartResult:
    return AutostartResult(
        status=AutostartStatus.DRY_RUN if dry_run else AutostartStatus.SUCCESS,
        verb=verb,
        solet_name="smoke",
        label="local.solet.smoke",
        plist_path="/offline/plist",
        prior_state="absent",
        last_run_at="",
        dry_run=dry_run,
        message="recorded fake",
    )


def _recording_fake(
    calls: list[bool],
    result_factory: Callable[[bool], object],
) -> Callable[..., object]:
    def fake(**kwargs: object) -> object:
        dry_run = kwargs["dry_run"]
        _expect(isinstance(dry_run, bool), f"fake received bool (got {dry_run!r})")
        calls.append(dry_run)
        return result_factory(dry_run)

    return fake


def _exercise_action(
    action_name: str,
    method_name: str,
    params: dict[str, Any],
    result_factory: Callable[[bool], object],
    expected_dry_run: bool | None,
) -> None:
    plugin = MacosSelfDeploymentPlugin()
    calls: list[bool] = []
    setattr(plugin, method_name, _recording_fake(calls, result_factory))
    action = getattr(plugin, action_name)
    if expected_dry_run is None:
        try:
            action(params, {})
        except ValueError:
            pass
        else:
            _expect(False, f"{action_name} rejects malformed params={params!r}")
        _expect(calls == [], f"{action_name} rejects before its engine for params={params!r}")
        return
    action(params, {})
    _expect(
        calls == [expected_dry_run],
        f"{action_name} passes dry_run={expected_dry_run} for params={params!r}",
    )


def _exercise_all_falsy_forms() -> None:
    cases: tuple[tuple[str, dict[str, Any], bool | None], ...] = (
        ("omitted", {}, True),
        ("None", {"dry_run": None}, True),
        ("empty string", {"dry_run": ""}, None),
        ("zero", {"dry_run": 0}, None),
    )
    actions: tuple[tuple[str, str, dict[str, Any], Callable[[bool], object]], ...] = (
        (
            "restart_with_manifest_action",
            "restart_with_manifest",
            {"new_manifest": {}, "expected_etag": "etag", "reason": "smoke"},
            _restart_result,
        ),
        (
            "stop_self_action",
            "stop_self",
            {"reason": "smoke"},
            _stop_result,
        ),
        (
            "install_autostart_action",
            "install_autostart",
            {},
            lambda dry_run: _autostart_result("install_autostart", dry_run),
        ),
        (
            "uninstall_autostart_action",
            "uninstall_autostart",
            {},
            lambda dry_run: _autostart_result("uninstall_autostart", dry_run),
        ),
    )
    for case_name, dry_run_param, expected_dry_run in cases:
        print(f"Case: {case_name}")
        for action_name, method_name, base_params, result_factory in actions:
            _exercise_action(
                action_name,
                method_name,
                base_params | dry_run_param,
                result_factory,
                expected_dry_run,
            )


def _exercise_shared_coercer_vectors() -> None:
    _expect(coerce_dry_run(None) is True, "shared coercer makes None report-only")
    _expect(coerce_dry_run(False) is False, "shared coercer preserves false")
    _expect(coerce_dry_run(True) is True, "shared coercer preserves true")
    _expect(coerce_dry_run("false") is False, "shared coercer accepts spelled false")
    _expect(coerce_dry_run(" true ") is True, "shared coercer accepts trimmed true")
    for invalid in ("", 0, "not-a-boolean"):
        try:
            coerce_dry_run(invalid)
        except ValueError:
            print(f"  OK  shared coercer rejects malformed value {invalid!r}")
        else:
            _expect(False, f"shared coercer rejects malformed value {invalid!r}")


def main() -> int:
    print("macOS self-deployment dry_run safe-default smoke\n")
    _exercise_all_falsy_forms()
    _exercise_shared_coercer_vectors()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
