"""Exact create-origin matching remains v1-byte-compatible and fail-closed."""

from __future__ import annotations

import stat
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

import solet_manager.create_origin_enrollment as create_origin_enrollment  # noqa: E402
import solet_manager.import_enrollment as enrollment  # noqa: E402
from import_enrollment_rerun_smoke import _inspection  # noqa: E402
from solet_manager.errors import ManagedIdentityDriftError, OperationInProgressError  # noqa: E402
from solet_manager.import_enrollment import ImportRequest  # noqa: E402
from solet_manager.models import CheckpointStatus, InstanceRecord, TransactionStatus  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, write_transaction  # noqa: E402
from solet_manager.update_execution import UpdateRequest, load_update_record, preview_update_instance  # noqa: E402


def _record(target: Path, *, commit: str = "a" * 40) -> InstanceRecord:
    return InstanceRecord(
        "fixture",
        str(target),
        str(target / "client" / "bin" / "fixture"),
        "https://example.invalid/seed.git",
        None,
        commit,
        "b" * 40,
        "profile",
        "flow",
        "revision",
        "sha256:" + "a" * 64,
        "2026-09-18T00:00:00Z",
        "2026-09-18T00:00:00Z",
    )


def _transaction(target: Path) -> Transaction:
    return replace(
        Transaction.create(
            name="fixture",
            target=target,
            input_fingerprint="sha256:" + "1" * 64,
            answers={},
            seed=SeedLock("https://example.invalid/seed.git", None, "a" * 40, "b" * 40, "c" * 64, "profile"),
            flow_id="flow",
            flow_source_revision="revision",
            flow_contract_digest="sha256:" + "a" * 64,
            stage_ids=("install",),
            completion_probe_ids=("doctor",),
        ),
        status=TransactionStatus.VERIFIED,
        stages={"install": CheckpointStatus.VERIFIED},
        completion={"doctor": CheckpointStatus.VERIFIED},
    )


def _assert_no_import_state(paths: ManagerPaths) -> None:
    assert not paths.maintenance_inventory_path.exists()
    assert not paths.operations_dir.exists()
    assert not (paths.cache_dir / "contracts").exists()


def _assert_refusals(request: ImportRequest, transaction: Transaction) -> None:
    paths = request.manager_paths
    preview = enrollment.preview_import(request)
    for value, error in (
        (None, ManagedIdentityDriftError),
        (replace(transaction, status=TransactionStatus.PENDING), OperationInProgressError),
        (replace(transaction, target="/different"), ManagedIdentityDriftError),
        (replace(transaction, seed=replace(transaction.seed, commit="c" * 40)), ManagedIdentityDriftError),
    ):
        with patch.object(create_origin_enrollment, "load_transaction", return_value=value):
            try:
                enrollment.enroll_import(request, preview.fingerprint)
            except error:
                pass
            else:
                raise AssertionError("unproven create transaction accepted")
        _assert_no_import_state(paths)
    try:
        enrollment.enroll_import(request, "sha256:" + "0" * 64)
    except ValueError as error:
        assert str(error) == "probe_drift"
    else:
        raise AssertionError("invalid approval accepted")
    _assert_no_import_state(paths)


def _tree_snapshot(root: Path) -> dict[Path, tuple[int, bytes]]:
    return {path.relative_to(root): (path.lstat().st_mode, path.read_bytes() if stat.S_ISREG(path.lstat().st_mode) else b"") for path in root.rglob("*")}


def _assert_only_lock_changes(
    before: dict[Path, tuple[int, bytes]],
    after: dict[Path, tuple[int, bytes]],
    locks: Path,
) -> None:
    allowed = {path for path in before.keys() | after.keys() if path.parent == locks and path.suffix == ".lock"}
    assert {path: value for path, value in before.items() if path not in allowed} == {path: value for path, value in after.items() if path not in allowed}, "refusal changed Manager state or target outside lock inodes"
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    assert changed and changed <= allowed, "expected only lock creation or chmod"
    for path in changed:
        _assert_empty_lock_transition(before.get(path), after[path])


def _assert_empty_lock_transition(before: tuple[int, bytes] | None, after: tuple[int, bytes]) -> None:
    mode, contents = after
    assert stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600 and contents == b""
    if before is not None:
        before_mode, before_contents = before
        assert stat.S_ISREG(before_mode) and before_contents == b"", "only empty regular lock creation/chmod allowed"


def _assert_refused_approval(request: ImportRequest, fingerprint: str, field: str) -> None:
    approval = "sha256:" + "0" * 64 if field == "approval" else fingerprint
    expected_error = ValueError if field == "approval" else ManagedIdentityDriftError
    try:
        enrollment.enroll_import(request, approval)
    except expected_error as error:
        if field == "approval":
            assert str(error) == "probe_drift"
    else:
        raise AssertionError(f"refused enrollment {field} accepted")


def _drift_fixture(root: Path, field: str) -> tuple[Path, ManagerPaths, Transaction, ImportRequest, Path]:
    target = root / field
    target.mkdir()
    marker = target / "operator-owned.txt"
    marker.write_bytes(b"unchanged")
    paths = ManagerPaths(root / (field + "config"), root / (field + "state"), root / (field + "cache"))
    for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
        directory.mkdir(mode=0o700)
    InstanceRegistry(paths.registry_path).add(_record(target))
    transaction = _transaction(target)
    write_transaction(paths.transaction_path("fixture"), transaction)
    locks = paths.state_dir / "locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    registry_lock = locks / "registry.lock"
    registry_lock.touch(mode=0o600, exist_ok=True)
    registry_lock.chmod(0o644)
    return marker, paths, transaction, ImportRequest("fixture", target, "stable", paths), locks


def _assert_drifted_preview_refused(request: ImportRequest, root: Path, field: str) -> dict[Path, tuple[int, bytes]]:
    """A fresh preview of a drifted create transaction is refused and changes nothing."""
    before = _tree_snapshot(root)
    try:
        enrollment.preview_import(request)
    except ManagedIdentityDriftError:
        pass
    else:
        raise AssertionError(f"preview accepted a create transaction whose {field} drifted")
    assert _tree_snapshot(root) == before, "refused preview changed Manager state or target"
    return before


def _assert_persisted_seed_drift(root: Path) -> None:
    for field, value in (("profile", "different-profile"), ("release_tag", "different-tag"), ("approval", "invalid")):
        marker, paths, transaction, request, locks = _drift_fixture(root, field)
        before = _tree_snapshot(root)
        preview = enrollment.preview_import(request)
        assert _tree_snapshot(root) == before, "preview changed Manager state or target"
        _assert_no_import_state(paths)
        if field != "approval":
            # The transaction drifts after a clean preview: apply refuses it, and so does a fresh preview.
            drifted = replace(transaction, seed=replace(transaction.seed, **{field: value}))
            write_transaction(paths.transaction_path("fixture"), drifted)
            before = _assert_drifted_preview_refused(request, root, field)
        registry_before = paths.registry_path.read_bytes()
        transaction_before = paths.transaction_path("fixture").read_bytes()
        _assert_no_import_state(paths)
        _assert_refused_approval(request, preview.fingerprint, field)
        _assert_only_lock_changes(before, _tree_snapshot(root), locks.relative_to(root))
        _assert_no_import_state(paths)
        assert paths.registry_path.read_bytes() == registry_before
        assert paths.transaction_path("fixture").read_bytes() == transaction_before
        assert marker.read_bytes() == b"unchanged"


def _assert_crash_retry(root: Path) -> None:
    advance = enrollment._advance_journal
    for seam in ("_publish_inventory", "_finalize_import", "_advance_journal"):
        target = root / seam
        target.mkdir()
        paths = ManagerPaths(root / (seam + "config"), root / (seam + "state"), root / (seam + "cache"))
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        InstanceRegistry(paths.registry_path).add(_record(target))
        write_transaction(paths.transaction_path("fixture"), _transaction(target))
        v1_before = paths.registry_path.read_bytes()
        request = ImportRequest("fixture", target, "stable", paths)
        preview = enrollment.preview_import(request)

        def crash_after_verified(preview: enrollment.ImportPreview, stage_id: str, status: str, code: str) -> None:
            advance(preview, stage_id, status, code)
            if status == "verified":
                raise RuntimeError("simulated crash")

        effect = crash_after_verified if seam == "_advance_journal" else RuntimeError("simulated crash")
        with patch.object(enrollment, seam, side_effect=effect):
            try:
                enrollment.enroll_import(request, preview.fingerprint)
            except RuntimeError as error:
                assert str(error) == "simulated crash"
            else:
                raise AssertionError("crash injection missed")
        resumed = enrollment.enroll_import(request, preview.fingerprint)
        _assert_already_managed_message(resumed, f"resumed prepared journal after {seam}")
        assert load_update_record(UpdateRequest("fixture", paths)).active_operation is None
        assert paths.registry_path.read_bytes() == v1_before


def _assert_already_managed_message(result: enrollment.ImportEnrollmentResult, path: str) -> None:
    """iss_6a27a24b: every already_managed path says the enrollment is recorded and verified, never that nothing changed."""
    rendered = result.to_command_result()
    expected = "Existing Solet is already managed by the Manager; its enrollment is recorded and verified."
    assert (result.status, rendered.status, rendered.message) == ("already_managed", "already_managed", expected), (path, rendered.message)


def _assert_preview_reads_enrollment(request: ImportRequest) -> None:
    # Exercise the real preview inventory lookup; stop before external candidate probing.
    with patch("solet_manager.update_preview_render.probe_update", side_effect=RuntimeError("probe boundary")) as probe:
        try:
            preview_update_instance(UpdateRequest(request.name, request.manager_paths))
        except RuntimeError as error:
            assert str(error) == "probe boundary"
        else:
            raise AssertionError("preview did not reach candidate probe")
        assert probe.call_count == 1
        assert probe.call_args.args[1].management_origin.value == "create"


def _assert_enrollment() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        target = root / "existing"
        target.mkdir()
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        registry = InstanceRegistry(paths.registry_path)
        registry.add(_record(target))
        write_transaction(paths.transaction_path("fixture"), _transaction(target))
        v1_before = paths.registry_path.read_bytes()
        request = ImportRequest("fixture", target, "stable", paths)
        original = enrollment.inspect_existing_install
        enrollment.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
        try:
            _assert_persisted_seed_drift(root)
            _assert_refusals(request, _transaction(target))
            _assert_crash_retry(root)
            preview = enrollment.preview_import(request)
            result = enrollment.enroll_import(request, preview.fingerprint)
            _assert_already_managed_message(result, "v1_match tail")
            rendered = result.to_command_result().data
            assert rendered["management_origin"] == "create"
            assert rendered["instance_id"] == "fixture"
            assert paths.registry_path.read_bytes() == v1_before
            record = load_update_record(UpdateRequest("fixture", paths))
            _assert_preview_reads_enrollment(request)
            assert record.management_origin.value == "create"
            assert record.source_release.commit == "a" * 40
            assert record.runtime_release is None and record.verified_release is None
            inventory_before = paths.maintenance_inventory_path.read_bytes()
            early = enrollment.enroll_import(request, preview.fingerprint)
            _assert_already_managed_message(early, "early return on a finalized enrollment")
            assert paths.maintenance_inventory_path.read_bytes() == inventory_before
        finally:
            enrollment.inspect_existing_install = original


def _assert_seed_drift() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        target = root / "existing"
        target.mkdir()
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
            directory.mkdir(mode=0o700)
        InstanceRegistry(paths.registry_path).add(_record(target, commit="c" * 40))
        write_transaction(paths.transaction_path("fixture"), _transaction(target))
        request = ImportRequest("fixture", target, "stable", paths)
        original = enrollment.inspect_existing_install
        enrollment.inspect_existing_install = lambda request, metadata_loader: _inspection(request)
        try:
            try:
                enrollment.preview_import(request)
            except ManagedIdentityDriftError:
                pass
            else:
                raise AssertionError("partial create-origin seed identity was accepted")
            _assert_no_import_state(paths)
        finally:
            enrollment.inspect_existing_install = original


def _assert_real_launcher_enrollment() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        target = root / "existing"
        client = target / "client" / "bin" / "fixture"
        client.parent.mkdir(parents=True)
        client.write_bytes(b"legacy client")
        bridge = target / ".venv" / "bin" / "solet-bridge"
        bridge.parent.mkdir(parents=True)
        bridge.write_bytes(b"bridge")
        named = root / ".local" / "bin" / "fixture"
        named.parent.mkdir(parents=True)
        named.symlink_to(bridge)
        assert client.resolve() != named.resolve()
        paths = ManagerPaths(root / "config", root / "state", root / "cache")
        InstanceRegistry(paths.registry_path).add(_record(target))
        write_transaction(paths.transaction_path("fixture"), _transaction(target))
        before = paths.registry_path.read_bytes()
        request = ImportRequest("fixture", target, "stable", paths)
        with (
            patch.object(Path, "home", return_value=root),
            patch.object(
                enrollment,
                "inspect_existing_install",
                side_effect=lambda request, metadata_loader: _inspection(request),
            ),
        ):
            preview = enrollment.preview_import(request)
            assert enrollment.enroll_import(request, preview.fingerprint).status == "already_managed"
            record = load_update_record(UpdateRequest("fixture", paths))
            assert record.management_origin.value == "create"
            assert Path(record.service_identity.named_launcher_path).resolve() == bridge.resolve()
            inventory_before = paths.maintenance_inventory_path.read_bytes()
            enrollment.enroll_import(request, preview.fingerprint)
        assert paths.registry_path.read_bytes() == before
        assert paths.maintenance_inventory_path.read_bytes() == inventory_before
        assert client.read_bytes() == b"legacy client" and bridge.read_bytes() == b"bridge"


def main() -> int:
    _assert_real_launcher_enrollment()
    _assert_enrollment()
    _assert_seed_drift()
    print("import_v1_already_managed_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
