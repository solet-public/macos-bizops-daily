"""iss_33637918, iss_2d4061a9: a name owned by the other registry is refused with the right verb, before any state write.

``solet create`` writes the v1 registry; ``solet-manager import`` writes only the v2 maintenance inventory.
Each CLI reads only its own, so each used to refuse the other's names with no or wrong direction:

* ``solet-manager doctor`` of a create instance told the operator ``update --dry-run`` enrolls it (it only
  previews); the approved ``--yes`` enrolls;
* ``solet status|start|doctor|attest|reconcile-*`` of an IMPORTED solet said "managed instance ... does not
  exist" (or, for ``status``, to review ``solet create``); they now refuse as ``instance_imported`` and name
  ``solet-manager doctor`` (``start`` names the launchd kickstart: the Manager has no start verb).

Controls: a create instance, a create-origin enrolled name (both registries) and an unknown name keep their
behaviour; a refusal changes no byte under the manager home.  Fixture-free: the real registry and inventory
writers on a temporary manager home.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager import cli  # noqa: E402
from solet_manager.errors import InstanceUnmanagedV2Error, ManagerError, StateConflictError  # noqa: E402
from solet_manager.existing_install_doctor import _load_record  # noqa: E402
from solet_manager.maintenance_inventory import write_maintenance_inventory_v2  # noqa: E402
from solet_manager.models import (  # noqa: E402
    ChannelIdentity,
    CommandResult,
    ContractIdentities,
    FilesystemIdentity,
    InstanceInventoryRecordV2,
    InstanceRecord,
    ManagementOrigin,
    ManagementState,
    ObservedProvenanceIdentity,
    ReleaseIdentity,
    ServiceIdentity,
    TargetIdentity,
    UpdateEligibility,
    UpdateEligibilityState,
)
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402

_NOW = "2026-09-30T00:00:00Z"
_REPOSITORY = "https://github.com/example/seed.git"
_IMPORTED = "dax"
_CREATED = "made"
_ENROLLED = "both"
_UNKNOWN = "nobody"
_IMPORTED_VERBS = ("status", "start", "doctor", "attest", "reconcile-contract", "reconcile-adapter", "reconcile-identity")
_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _v2_row(name: str, digit: str) -> InstanceInventoryRecordV2:
    return InstanceInventoryRecordV2(
        "ins_" + digit * 32,
        name,
        TargetIdentity(f"/tmp/{name}", FilesystemIdentity(1, 2), FilesystemIdentity(1, 3)),
        ManagementOrigin.IMPORT,
        ManagementState.DIAGNOSTIC,
        UpdateEligibility(UpdateEligibilityState.AVAILABLE, ()),
        ServiceIdentity(f"/tmp/{name}/.venv/bin/solet", f"/tmp/{name}/.venv/bin/solet-bridge", f"/tmp/bin/{name}", None, None, f"/tmp/{name}/profile", f"local.solet.{name}", None, None),
        ChannelIdentity("stable", "sha256:" + "1" * 64, _REPOSITORY),
        ObservedProvenanceIdentity("strict", "sha256:" + "2" * 64, "seed", "origin", "sha256:" + "3" * 64, None),
        ReleaseIdentity(_REPOSITORY, "4" * 40, "5" * 40, "r1"),
        ReleaseIdentity(_REPOSITORY, "4" * 40, "5" * 40, "r1"),
        None,
        ContractIdentities("sha256:" + "6" * 64, None, None, "sha256:" + "7" * 64, None),
        "sha256:" + "8" * 64,
        None,
        None,
        _NOW,
        _NOW,
        _NOW,
        _NOW,
    )


def _v1_row(name: str) -> InstanceRecord:
    return InstanceRecord(name, f"/tmp/{name}", f"/tmp/{name}/client/bin/{name}", _REPOSITORY, None, "a" * 40, "b" * 40, "profile", "flow", "revision", "sha256:" + "a" * 64, _NOW, _NOW)


def _build(home: Path) -> ManagerPaths:
    """v1-only ``made``; v2-only ``dax``; ``both`` in both registries."""
    paths = ManagerPaths.resolve(explicit_home=home)
    for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
        directory.mkdir(parents=True, mode=0o700)
    registry = InstanceRegistry(paths.registry_path)
    registry.add(_v1_row(_CREATED))
    registry.add(_v1_row(_ENROLLED))
    write_maintenance_inventory_v2(paths.maintenance_inventory_path, (_v2_row(_ENROLLED, "a"), _v2_row(_IMPORTED, "b")))
    return paths


def _snapshot(home: Path) -> dict[str, bytes | None]:
    return {str(path.relative_to(home)): path.read_bytes() if path.is_file() else None for path in sorted(home.rglob("*"))}


def _argv(home: Path, verb: str, name: str) -> list[str]:
    return ["--home", str(home), verb, name]


def _raised(action: Callable[[], object]) -> ManagerError | None:
    try:
        action()
    except ManagerError as exc:
        return exc
    return None


def _assert_imported_refused(home: Path) -> None:
    before = _snapshot(home)
    for verb in _IMPORTED_VERBS:
        exc = _raised(lambda verb=verb: cli.run(_argv(home, verb, _IMPORTED)))
        _check(exc is not None and exc.error_kind == "instance_imported", f"{verb}: an imported solet is refused as instance_imported, not {exc!r}")
        assert exc is not None
        _check(exc.exit_code == 3, f"{verb}: exit code 3: {exc.exit_code}")
        _check("does not exist" not in str(exc), f"{verb}: no raw 'does not exist': {exc}")
        _check(f"solet {verb}" in str(exc) and "solet create" in str(exc), f"{verb}: the refusal names the verb and why: {exc}")
        repair = exc.repair or ""
        _check(f"solet-manager doctor {_IMPORTED}" in repair or verb == "start", f"{verb}: the repair names the Manager verb: {repair}")
    _check(_snapshot(home) == before, "refusals changed no byte or entry under the manager home")


def _assert_start_repair(home: Path) -> None:
    exc = _raised(lambda: cli.run(_argv(home, "start", _IMPORTED)))
    repair = (exc.repair if exc is not None else None) or ""
    _check("launchctl kickstart -k gui/$(id -u)/local.solet.dax" in repair, f"start: the repair carries the launchd label: {repair}")
    _check(f"{_IMPORTED} health" in repair and "no start verb" in repair, f"start: the repair says the Manager cannot start it: {repair}")


def _assert_controls(home: Path) -> None:
    for name in (_CREATED, _ENROLLED):
        result = cli.run(_argv(home, "status", name))
        _check(isinstance(result, CommandResult) and result.error_kind != "instance_absent", f"status {name}: a registered name keeps its v1 status: {result}")
        for verb in ("doctor", "attest"):
            exc = _raised(lambda verb=verb, name=name: cli.run(_argv(home, verb, name)))
            _check(getattr(exc, "error_kind", None) != "instance_imported", f"{verb} {name}: the guard is silent for a name with a v1 row: {exc!r}")
    absent = cli.run(_argv(home, "status", _UNKNOWN))
    _check(absent.error_kind == "instance_absent" or "instance_absent" in str(absent), f"status of an unknown name keeps instance_absent: {absent}")
    for verb in ("start", "doctor", "attest"):
        exc = _raised(lambda verb=verb: cli.run(_argv(home, verb, _UNKNOWN)))
        _check(isinstance(exc, StateConflictError) and "does not exist" in str(exc), f"{verb}: an unknown name keeps 'does not exist': {exc!r}")


def _assert_doctor_repair(paths: ManagerPaths) -> None:
    exc = _raised(lambda: _load_record(paths, _CREATED))
    _check(isinstance(exc, InstanceUnmanagedV2Error) and exc.error_kind == "instance_unmanaged_v2" and exc.exit_code == 3, f"a create instance stays instance_unmanaged_v2: {exc!r}")
    assert exc is not None
    repair = exc.repair or ""
    _check("--dry-run` enrolls" not in repair and "--dry-run enrolls" not in repair, f"the repair no longer says --dry-run enrolls: {repair}")
    _check("enrolls nothing" in repair, f"the repair says the dry run enrolls nothing: {repair}")
    _check("--yes" in repair and "--approval-fingerprint" in repair, f"the repair names the approved --yes: {repair}")
    _check(f"solet doctor {_CREATED}" in repair, f"the repair keeps the working verb: {repair}")


def main() -> int:
    with TemporaryDirectory() as temporary:
        home = Path(temporary).resolve() / "home"
        paths = _build(home)
        _assert_doctor_repair(paths)
        _assert_imported_refused(home)
        _assert_start_repair(home)
        _assert_controls(home)
    print(f"registry_owner_refusal_smoke OK ({_CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
