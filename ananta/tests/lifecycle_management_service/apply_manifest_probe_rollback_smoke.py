#!/usr/bin/env python3
"""S3 — core plumbing pin for the GTE-06 probe_failed path (no pytest).

First-ever exercise of core's (previously dormant) probe seam, against
the REAL ``LifecycleManagementService.apply_manifest`` with a real temp
``APP_HOME`` and real on-disk manifest bytes — the fake is ONLY the
bound deployment plugin (the exact seam the real macOS plugin fills).

Pins:

* [1-3] ``PROBE_FAILED`` → ``probe_failed_manifest_rolled_back``:
  manifest + bindings restored BYTE-IDENTICAL (sha256 compare), the
  probe payload rides the envelope (proves ``_delegate_restart`` copies
  ``RestartResult.probe``), rejection_reasons carry the failing-step
  detail.
* [4-5] A1 CAS-guarded restore: a concurrent commit during the probe
  window (the fake plugin rewrites the manifest before returning
  ``PROBE_FAILED``) is NOT stomped — envelope
  ``probe_failed_manifest_changed_during_probe``, concurrent bytes left
  in place.
* [6] OSError during restore → ``probe_failed_rollback_failed`` (loud,
  operator-facing).
* [7-8] Q5: a QUEUED result carrying probe success evidence lands on
  the applied envelope as ``data.probe``.

Run:
    SOLET_NAME=<name> .venv/bin/python3 ananta/tests/lifecycle_management_service/apply_manifest_probe_rollback_smoke.py
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _probe_fixture_support import (  # noqa: E402
    FIXTURE_PLUGIN_NAME,
    GOOD_SOURCE,
    write_fixture,
)
from ananta.interfaces.lifecycle_result_types import (  # noqa: E402
    RestartResult,
    RestartStatus,
)
from ananta.services.lifecycle_management_service.service import (  # noqa: E402
    LifecycleManagementService,
)

_passed = 0
_failed: list[str] = []

_PROBE_PAYLOAD: dict[str, object] = {
    "failing_step": "L1.1_import",
    "error_class": "RuntimeError",
    "detail": "planted probe rejection",
    "failures": [
        {
            "check": "L1.1_import",
            "plugin": FIXTURE_PLUGIN_NAME,
            "message": "planted probe rejection",
            "error_class": "RuntimeError",
        },
    ],
    "release_id": "rel-smoke",
}


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _FakeDeploymentPlugin:
    """The bound self_deployment_service seam: returns a scripted result.

    ``pre_return_hook`` runs before returning — the A1 case uses it to
    simulate a concurrent commit landing during the build+probe window.
    """

    def __init__(self, result: RestartResult) -> None:
        self._result = result
        self.pre_return_hook: Any = None
        self.calls = 0

    def restart_with_manifest(
        self, *, new_manifest: dict[str, Any], expected_etag: str,
        reason: str, dry_run: bool,
    ) -> RestartResult:
        del new_manifest, expected_etag, reason, dry_run
        self.calls += 1
        if self.pre_return_hook is not None:
            self.pre_return_hook()
        return self._result


class _FakeOrchestratorRef:
    def __init__(self, app_home: Path, plugin: _FakeDeploymentPlugin) -> None:
        self.APP_HOME = str(app_home)
        self._plugin = plugin

    def get_service(self, name: str) -> _FakeDeploymentPlugin | None:
        return self._plugin if name == "self_deployment_service" else None


def _seed_app_home(app_home: Path) -> tuple[bytes, bytes]:
    """Write a known-good pre-commit manifest + bindings; return their bytes.

    ``self_deployment_service`` must be bound (the binding validator
    requires it on every apply) — the fixture plugin doubles as its
    provider; binding validation is list-membership, not interface
    conformance.
    """
    config = app_home / "config"
    config.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (
        f"profile_name: local\nplugins:\n- {FIXTURE_PLUGIN_NAME}\n"
    ).encode()
    bindings_bytes = (
        '{"self_deployment_service": "' + FIXTURE_PLUGIN_NAME + '"}\n'
    ).encode("utf-8")
    (config / "manifest.yaml").write_bytes(manifest_bytes)
    (config / "service_bindings.json").write_bytes(bindings_bytes)
    return manifest_bytes, bindings_bytes


def _probe_failed_result() -> RestartResult:
    return RestartResult(
        status=RestartStatus.PROBE_FAILED,
        restart_action_id="",
        message="L2 fresh-source preflight probe rejected the deploy (smoke)",
        reason="smoke",
        expected_etag="",
        dry_run=False,
        reason_code="probe_rejected",
        probe=dict(_PROBE_PAYLOAD),
    )


def _failed_result() -> RestartResult:
    return RestartResult(
        status=RestartStatus.FAILED,
        restart_action_id="",
        message="restart scheduling failed (smoke)",
        reason="smoke",
        expected_etag="",
        dry_run=False,
    )


def _apply(service: LifecycleManagementService) -> dict[str, Any]:
    return service.apply_manifest(
        new_manifest={"plugins": [FIXTURE_PLUGIN_NAME], "service_bindings": {}},
        reason="gte06-probe-rollback-smoke",
    )


def _case_rolled_back(app_home: Path) -> None:
    manifest_bytes, bindings_bytes = _seed_app_home(app_home)
    plugin = _FakeDeploymentPlugin(_probe_failed_result())
    service = LifecycleManagementService(_FakeOrchestratorRef(app_home, plugin))
    envelope = _apply(service)
    data = envelope["data"]
    _check(
        data.get("status") == "probe_failed_manifest_rolled_back",
        f"[1] PROBE_FAILED → probe_failed_manifest_rolled_back (got {data.get('status')!r})",
    )
    restored_manifest = (app_home / "config" / "manifest.yaml").read_bytes()
    restored_bindings = (app_home / "config" / "service_bindings.json").read_bytes()
    _check(
        restored_manifest == manifest_bytes and restored_bindings == bindings_bytes,
        "[2] manifest + bindings restored BYTE-IDENTICAL to the pre-commit state",
    )
    reasons = " ".join(data.get("rejection_reasons") or [])
    _check(
        data.get("probe") == _PROBE_PAYLOAD and "L1.1_import" in reasons,
        "[3] probe payload rides the envelope (delegate copies RestartResult.probe) "
        "+ rejection_reasons carry the failing step",
    )


def _case_concurrent_commit(app_home: Path) -> None:
    _seed_app_home(app_home)
    plugin = _FakeDeploymentPlugin(_probe_failed_result())
    concurrent_bytes = b"profile_name: local\nplugins:\n- somebody_elses_plugin\n"

    def _concurrent_commit() -> None:
        (app_home / "config" / "manifest.yaml").write_bytes(concurrent_bytes)

    plugin.pre_return_hook = _concurrent_commit
    service = LifecycleManagementService(_FakeOrchestratorRef(app_home, plugin))
    envelope = _apply(service)
    data = envelope["data"]
    _check(
        data.get("status") == "probe_failed_manifest_changed_during_probe",
        f"[4] A1: concurrent commit during the probe window → loud no-restore "
        f"envelope (got {data.get('status')!r})",
    )
    on_disk = (app_home / "config" / "manifest.yaml").read_bytes()
    _check(
        on_disk == concurrent_bytes
        and data.get("written_etag") != data.get("on_disk_etag"),
        "[5] A1: the concurrent commit's bytes were NOT stomped (no restore ran; "
        "written_etag != on_disk_etag surfaced)",
    )


def _case_restore_oserror(app_home: Path) -> None:
    _seed_app_home(app_home)
    plugin = _FakeDeploymentPlugin(_probe_failed_result())
    config_dir = app_home / "config"

    def _lock_config_dir() -> None:
        os.chmod(config_dir, stat.S_IRUSR | stat.S_IXUSR)

    plugin.pre_return_hook = _lock_config_dir
    service = LifecycleManagementService(_FakeOrchestratorRef(app_home, plugin))
    try:
        envelope = _apply(service)
        data = envelope["data"]
        _check(
            data.get("status") == "probe_failed_rollback_failed",
            f"[6] restore OSError → probe_failed_rollback_failed (got {data.get('status')!r})",
        )
    finally:
        os.chmod(config_dir, stat.S_IRWXU)


def _case_restart_failed_rolled_back(app_home: Path) -> None:
    manifest_bytes, bindings_bytes = _seed_app_home(app_home)
    plugin = _FakeDeploymentPlugin(_failed_result())
    service = LifecycleManagementService(_FakeOrchestratorRef(app_home, plugin))
    envelope = _apply(service)
    data = envelope["data"]
    _check(
        data.get("status") == "restart_failed_after_manifest_commit",
        f"[7] FAILED restart stays an honest failure envelope (got {data.get('status')!r})",
    )
    _check(
        (app_home / "config" / "manifest.yaml").read_bytes() == manifest_bytes
        and (app_home / "config" / "service_bindings.json").read_bytes() == bindings_bytes,
        "[8] FAILED restart restores manifest + bindings BYTE-IDENTICAL to pre-write state",
    )
    _check(
        data.get("manifest_restored_to") and data.get("service_bindings_restored_to"),
        "[9] FAILED restart envelope exposes the restoration paths",
    )


def _case_q5_success_evidence(app_home: Path) -> None:
    _seed_app_home(app_home)
    evidence: dict[str, object] = {
        "ok": True, "duration_ms": 42, "release_id": "rel-smoke",
    }
    plugin = _FakeDeploymentPlugin(RestartResult(
        status=RestartStatus.QUEUED,
        restart_action_id="ae-smoke",
        message="queued (smoke)",
        reason="smoke",
        expected_etag="",
        dry_run=False,
        probe=evidence,
    ))
    service = LifecycleManagementService(_FakeOrchestratorRef(app_home, plugin))
    envelope = _apply(service)
    data = envelope["data"]
    _check(
        data.get("status") == "applied" and data.get("probe") == evidence,
        f"[7] Q5: QUEUED + probe evidence → applied envelope carries data.probe "
        f"(got status={data.get('status')!r}, probe={data.get('probe')!r})",
    )
    _check(
        data.get("preflight_deferred") == [],
        "[8] applied envelope carries an (empty) preflight_deferred list",
    )


def run_smoke() -> int:
    print("=== apply_manifest_probe_rollback_smoke (S3: core probe_failed plumbing) ===")
    with tempfile.TemporaryDirectory() as tmp:
        fixture_dir = Path(tmp) / "fixture"
        write_fixture(fixture_dir, GOOD_SOURCE)
        sys.path.insert(0, str(fixture_dir))
        try:
            for case in (
                _case_rolled_back,
                _case_concurrent_commit,
                _case_restore_oserror,
                _case_restart_failed_rolled_back,
                _case_q5_success_evidence,
            ):
                app_home = Path(tempfile.mkdtemp(dir=tmp, prefix="app_home_"))
                case(app_home)
        finally:
            sys.path.remove(str(fixture_dir))

    print(f"\napply_manifest_probe_rollback_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
