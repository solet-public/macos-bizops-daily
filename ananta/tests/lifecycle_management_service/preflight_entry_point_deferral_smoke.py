#!/usr/bin/env python3
"""S6 — A2 deferral-placement pin (no pytest).

The GTE-06 A2 ruling: the ``EntryPointMissingError`` reject→defer
downgrade lives at the L1 CALL SITE ONLY
(``service._preflight_apply_manifest``). If the deferral leaked into the
shared ``run_manifest_preflight`` / ``check_imports``, the L2 probe —
which executes the same function — would ALSO defer it, and a
genuinely-missing entry point would sail through both gates to a green
register-timeout with NO manifest rollback.

Pins (all four quadrants of the placement):

* [1] the shared function, called directly, still REJECTS a missing
  entry point (context-free — the probe context's behavior);
* [2] the probe subprocess REJECTS a missing entry point with the typed
  failure (the deferral did NOT leak into the probe);
* [3] ``apply_manifest`` (dry-run) does NOT reject on
  ``EntryPointMissingError`` — it returns the dry-run envelope with the
  finding under ``preflight_deferred``;
* [4] positive control: a DIFFERENT L1 failure class (planted import
  error via a real broken entry point) still rejects pre-commit — the
  deferral is exactly one class wide.

Run:
    SOLET_NAME=<name> .venv/bin/python3 ananta/tests/lifecycle_management_service/preflight_entry_point_deferral_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _probe_fixture_support import (  # noqa: E402
    BROKEN_SOURCE,
    FIXTURE_PLUGIN_NAME,
    run_probe_subprocess,
    write_fixture,
)
from ananta.services.lifecycle_management_service.manifest_preflight import (  # noqa: E402
    ENTRY_POINT_MISSING_ERROR_CLASS,
    run_manifest_preflight,
)
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)

_passed = 0
_failed: list[str] = []

_MISSING_PLUGIN = "gte06_smoke_missing_entry_point_plugin"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _NeverCalledDeploymentPlugin:
    """dry_run never delegates a restart; loud if it somehow does."""

    def restart_with_manifest(self, **_kwargs: Any) -> None:
        raise AssertionError("restart_with_manifest must not fire on dry_run")


class _FakeOrchestratorRef:
    def __init__(self, app_home: Path) -> None:
        self.APP_HOME = str(app_home)
        self._plugin = _NeverCalledDeploymentPlugin()

    def get_service(self, name: str) -> _NeverCalledDeploymentPlugin | None:
        return self._plugin if name == "self_deployment_service" else None


def _seed_app_home(app_home: Path, provider: str) -> None:
    config = app_home / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "manifest.yaml").write_bytes(
        f"profile_name: local\nplugins:\n- {provider}\n".encode()
    )
    (config / "service_bindings.json").write_bytes(
        ('{"self_deployment_service": "' + provider + '"}\n').encode()
    )


def _dry_run(app_home: Path, plugins: list[str]) -> dict[str, Any]:
    service = LifecycleManagementService(_FakeOrchestratorRef(app_home))
    return service.apply_manifest(
        new_manifest={"plugins": plugins, "service_bindings": {}},
        reason="gte06-deferral-smoke",
        dry_run=True,
    )


def _case_shared_function_rejects() -> None:
    """[1] the shared function stays context-free and fully rejecting."""
    shared = run_manifest_preflight({"plugins": [_MISSING_PLUGIN]})
    shared_classes = [failure.error_class for failure in shared.failures]
    _check(
        not shared.ok and ENTRY_POINT_MISSING_ERROR_CLASS in shared_classes,
        f"[1] run_manifest_preflight itself still REJECTS a missing entry "
        f"point (classes={shared_classes})",
    )


def _case_probe_rejects() -> None:
    """[2] the probe context rejects it too — the deferral did not leak."""
    exit_code, envelope, _stderr = run_probe_subprocess(
        fixture_dir=None, plugins=[_MISSING_PLUGIN],
    )
    probe_classes = [
        failure.get("error_class")
        for failure in ((envelope or {}).get("failures") or [])
        if isinstance(failure, dict)
    ]
    _check(
        exit_code == 3 and ENTRY_POINT_MISSING_ERROR_CLASS in probe_classes,
        f"[2] the L2 probe REJECTS a missing entry point "
        f"(exit={exit_code}, classes={probe_classes})",
    )


def _case_l1_call_site_defers(tmp: Path) -> None:
    """[3] the L1 call site DEFERS: dry-run succeeds, finding surfaced."""
    app_home = tmp / "app_home_defer"
    _seed_app_home(app_home, _MISSING_PLUGIN)
    envelope_defer = _dry_run(app_home, [_MISSING_PLUGIN])
    data = envelope_defer["data"]
    deferred = data.get("preflight_deferred") or []
    _check(
        data.get("status") == "dry_run"
        and any(ENTRY_POINT_MISSING_ERROR_CLASS in entry for entry in deferred),
        f"[3] apply_manifest DEFERS EntryPointMissingError to the probe "
        f"(status={data.get('status')!r}, preflight_deferred={deferred})",
    )


def _case_other_class_still_rejects(tmp: Path) -> None:
    """[4] positive control: a planted import error still rejects pre-commit."""
    fixture_dir = tmp / "fixture_broken"
    write_fixture(fixture_dir, BROKEN_SOURCE)
    sys.path.insert(0, str(fixture_dir))
    try:
        app_home_broken = tmp / "app_home_broken"
        _seed_app_home(app_home_broken, FIXTURE_PLUGIN_NAME)
        envelope_broken = _dry_run(app_home_broken, [FIXTURE_PLUGIN_NAME])
        data_broken = envelope_broken["data"]
        reasons = " ".join(data_broken.get("rejection_reasons") or [])
        _check(
            data_broken.get("status") == "rejected"
            and "RuntimeError" in reasons
            and "planted cache-poison probe target" in reasons,
            f"[4] positive control: a planted import error still rejects "
            f"pre-commit (status={data_broken.get('status')!r})",
        )
    finally:
        sys.path.remove(str(fixture_dir))


def run_smoke() -> int:
    print("=== preflight_entry_point_deferral_smoke (S6: A2 deferral at the L1 call site ONLY) ===")
    _case_shared_function_rejects()
    _case_probe_rejects()
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        _case_l1_call_site_defers(tmp)
        _case_other_class_still_rejects(tmp)

    print(f"\npreflight_entry_point_deferral_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
