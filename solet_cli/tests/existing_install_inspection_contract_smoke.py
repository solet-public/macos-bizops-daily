"""Focused pure-contract smoke tests for the restricted inspection model."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.existing_install_inspection import (  # noqa: E402, I001
    ChannelRelation,
    ExistingInstallClass,
    ExistingInstallFacts,
    ExistingInstallInspectionRequest,
    ExistingInstallInspectionResult,
    InstalledInspectionMetadata,
    ExistingInstallContractIdentity,
    ChannelInspectionIdentity,
    InspectionAnchorKind,
    InspectionStatus,
    ObservationAvailability,
    ObservedBoolean,
    ObservedPathPairs,
    ObservedPaths,
    ObservedRawRows,
    ProvenanceCondition,
    RepositoryRelation,
    WorkingTreeCondition,
    ExistingInstallClassification,
    InspectionCheck,
    InspectionPreservationFacts,
    TargetFilesystemIdentity,
    _ProductionInspectionEffectTracker,
    _inventory_check,
    _reduce_exit,
    classify_existing_install,
    inspect_existing_install,
)
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager._existing_install_inspection_classification import _ROWS  # noqa: E402


def _facts(repository: RepositoryRelation) -> ExistingInstallFacts:
    observed = ObservedPaths(ObservationAvailability.OBSERVED, ())
    raw = ObservedRawRows(ObservationAvailability.OBSERVED, ())
    return ExistingInstallFacts(
        ProvenanceCondition.STRICT,
        InspectionAnchorKind.CURRENT_CHANNEL,
        None,
        InspectionStatus.VERIFIED,
        repository,
        ChannelRelation.CURRENT,
        "a" * 40,
        "b" * 40,
        WorkingTreeCondition.CLEAN,
        observed,
        observed,
        observed,
        observed,
        observed,
        observed,
        ObservedPathPairs(ObservationAvailability.OBSERVED, ()),
        observed,
        observed,
        observed,
        ObservedBoolean.FALSE,
        ObservedBoolean.FALSE,
        staged_paths=observed,
        tracked_entries=raw,
    )


def _check(
    status: InspectionStatus,
    *,
    required: bool = True,
    reason: str | None = None,
) -> InspectionCheck:
    return InspectionCheck("check", "test", required, status, reason, "", None, None, "test")


def _classification_witnesses() -> dict[ExistingInstallClass, ExistingInstallFacts]:
    base = _facts(RepositoryRelation.CANONICAL)
    return {
        ExistingInstallClass.DEVELOPMENT_CHECKOUT: replace(
            base, anchor_kind=InspectionAnchorKind.DEVELOPMENT_CHECKOUT, anchor_id="development"
        ),
        ExistingInstallClass.PROVENANCE_UNAVAILABLE: replace(
            base,
            provenance_condition=ProvenanceCondition.MISSING,
            anchor_kind=InspectionAnchorKind.NONE,
            identity_status=InspectionStatus.FAILED,
            channel_relation=ChannelRelation.UNKNOWN,
        ),
        ExistingInstallClass.SOURCE_IDENTITY_UNPROVEN: replace(
            base,
            anchor_kind=InspectionAnchorKind.NONE,
            identity_status=InspectionStatus.FAILED,
            channel_relation=ChannelRelation.UNKNOWN,
        ),
        ExistingInstallClass.INSPECTION_INCOMPLETE: _facts(RepositoryRelation.UNKNOWN),
        ExistingInstallClass.LEGACY_PROVENANCE: replace(
            base,
            provenance_condition=ProvenanceCondition.MISSING,
            anchor_kind=InspectionAnchorKind.LEGACY_PROVENANCE,
            anchor_id="legacy",
            channel_relation=ChannelRelation.LEGACY_BRIDGE_REQUIRED,
        ),
        ExistingInstallClass.DIVERGED_SEED_HISTORY: replace(
            base,
            anchor_kind=InspectionAnchorKind.PRE_MANAGER_SEED,
            anchor_id="pre-manager",
            channel_relation=ChannelRelation.DIVERGED,
        ),
        ExistingInstallClass.BLOCKING_LOCAL_STATE: replace(
            base, submodules=ObservedPaths(ObservationAvailability.OBSERVED, ("nested",))
        ),
        ExistingInstallClass.REVIEWED_HISTORICAL_REPOSITORY: _facts(
            RepositoryRelation.REVIEWED_HISTORICAL
        ),
        ExistingInstallClass.UNKNOWN_REPOSITORY_CANONICAL_COMMIT: _facts(RepositoryRelation.OTHER),
        # Step 7 section 6.4: row 10 is the real-clone shape -- a mixed tree (genesis rewrites + genesis untracked paths).
        ExistingInstallClass.LOCAL_CHANGES_PRESENT: replace(
            base,
            working_tree=WorkingTreeCondition.MIXED_CHANGES,
            tracked_paths=ObservedPaths(ObservationAvailability.OBSERVED, ("root_manifest.yaml",)),
            untracked_paths=ObservedPaths(ObservationAvailability.OBSERVED, (".gitignore", ".solet/genesis.json")),
        ),
        ExistingInstallClass.PRE_MANAGER_SEED_CLONE: replace(
            base, anchor_kind=InspectionAnchorKind.PRE_MANAGER_SEED, anchor_id="pre-manager"
        ),
        ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE: base,
    }


def _result_with_inventory(
    check: InspectionCheck, paths: ManagerPaths
) -> ExistingInstallInspectionResult:
    facts = _facts(RepositoryRelation.CANONICAL)
    identity = TargetFilesystemIdentity(Path("/fixed"), Path("/fixed"), 1, 2, 3, 4)
    channel = ChannelInspectionIdentity(
        "stable",
        "https://example.invalid/repository.git",
        "r1",
        "a" * 40,
        "b" * 40,
        "profile",
        "c" * 64,
        "123e4567-e89b-12d3-a456-426614174001",
        "123e4567-e89b-12d3-a456-426614174001",
        "d" * 64,
        ExistingInstallContractIdentity("existing-install", 1, "sha256:" + "e" * 64),
        "catalog",
        "f" * 64,
        "seed",
        "0" * 64,
        "sha256:" + "2" * 64,
        "anchors",
        "1" * 64,
    )
    preservation = InspectionPreservationFacts(0, 0, 0, 0, 0, 0, 0, 0, (), ())
    return ExistingInstallInspectionResult(
        ExistingInstallInspectionRequest(Path("/fixed"), "stable", paths),
        identity,
        channel,
        facts,
        classify_existing_install(facts),
        (_check(InspectionStatus.VERIFIED), check),
        preservation,
    )


def _canonical_result(result: ExistingInstallInspectionResult) -> bytes:
    return json.dumps(
        result.to_command_result().to_dict(), sort_keys=True, separators=(",", ":")
    ).encode()


def _assert_fixture_repositories() -> None:
    root = _ROOT / "solet_cli" / "tests" / "fixtures" / "existing_install_inspection"
    manifest = json.loads((root / "fixture_manifest.json").read_text(encoding="utf-8"))
    names = manifest["classification_repositories"] + manifest["adversarial_repositories"]
    assert len(names) == 19 and len(set(names)) == 19
    with tempfile.TemporaryDirectory() as directory:
        materialized = Path(directory)
        for name in names:
            fixture = materialized / name
            bundle = root / f"{name}.bundle"
            assert bundle.is_file()
            subprocess.run(("git", "clone", "--quiet", str(bundle), str(fixture)), check=True)
            _assert_git_fixture(fixture)
            _lay_down_local_state(fixture, name)
            _assert_fixture_reaches_production(fixture, materialized / "manager")


def _lay_down_local_state(fixture: Path, name: str) -> None:
    """Step 7 section 6.4 (B4): rows 8/9/10 are exercised on a MIXED tree -- a tracked edit plus the genesis untracked
    paths -- and the facade must classify it rather than raise ``inconsistent_existing_install_facts``."""
    if name not in {"row_08_reviewed_historical_repository", "row_09_unknown_repository_canonical_commit", "row_10_local_changes_present"}:
        return
    manifest = fixture / "fixture_manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    (fixture / "workbench").mkdir(exist_ok=True)
    (fixture / "workbench" / "operator_notes.md").write_text("operator notes\n", encoding="utf-8")
    (fixture / "knowledge_bases").mkdir(exist_ok=True)
    (fixture / "knowledge_bases" / "fixture_plugin").symlink_to("../plugins/fixture_plugin/knowledge_base")


def _assert_git_fixture(fixture: Path) -> None:
    completed = subprocess.run(
        ("git", "-C", str(fixture), "rev-parse", "--is-inside-work-tree"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "true"


def _assert_fixture_reaches_production(fixture: Path, manager: Path) -> None:
    """Every shipped Git fixture must traverse the real inspection facade."""
    paths = ManagerPaths(manager / "config", manager / "state", manager / "cache")
    result = inspect_existing_install(
        ExistingInstallInspectionRequest(fixture, "stable", paths),
        metadata_loader=lambda channel, tracker: _fixture_metadata(),
    ).to_command_result()
    assert result.exit_code in {1, 3}
    assert result.kind == "existing_install_inspection"


def _fixture_metadata() -> InstalledInspectionMetadata:
    identity = ChannelInspectionIdentity(
        "stable",
        "https://example.invalid/repository.git",
        "r1",
        "a" * 40,
        "b" * 40,
        "profile",
        "c" * 64,
        "123e4567-e89b-12d3-a456-426614174001",
        "123e4567-e89b-12d3-a456-426614174001",
        "d" * 64,
        ExistingInstallContractIdentity("existing-install", 1, "sha256:" + "e" * 64),
        "catalog",
        "f" * 64,
        "seed",
        "0" * 64,
        "sha256:" + "2" * 64,
        "anchors",
        "1" * 64,
    )
    return InstalledInspectionMetadata(
        identity,
        SeedLock(
            "https://example.invalid/repository.git", "r1", "a" * 40, "b" * 40, None, "profile"
        ),
        (),
    )


def _assert_classifications() -> None:
    witnesses = _classification_witnesses()
    assert len(witnesses) == 12
    classified = {
        kind: classify_existing_install(facts).installation_class
        for kind, facts in witnesses.items()
    }
    assert classified == {kind: kind for kind in ExistingInstallClass}
    assert len(_ROWS) == 12
    for kind, facts in witnesses.items():
        matching_rows = [result for predicate, result in _ROWS if predicate(facts)]
        assert len(matching_rows) == 1
        assert matching_rows[0].installation_class is kind
    _assert_row_four_and_nine(witnesses)
    _assert_pairwise_rows(witnesses)
    _assert_invalid_observation()
    _assert_local_state_facets(witnesses)


def _assert_local_state_facets(witnesses: dict[ExistingInstallClass, ExistingInstallFacts]) -> None:
    """Step 7 section 6.4: untracked presence is a facet, rows 8/9 admit mixed trees, and the facets are in reason codes."""
    base = witnesses[ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE]
    untracked_only = replace(base, working_tree=WorkingTreeCondition.UNTRACKED_ONLY, untracked_paths=ObservedPaths(ObservationAvailability.OBSERVED, (".gitignore",)))
    classified = classify_existing_install(untracked_only)
    assert classified.installation_class is ExistingInstallClass.LOCAL_CHANGES_PRESENT
    assert classified.reason_codes == ("local_changes_present", "untracked_paths")
    assert classified.update_disposition == "allowed_after_import" and classified.import_disposition == "allow"
    mixed = witnesses[ExistingInstallClass.LOCAL_CHANGES_PRESENT]
    assert classify_existing_install(mixed).reason_codes == ("local_changes_present", "tracked_changes", "untracked_paths")
    staged = replace(mixed, staged_paths=ObservedPaths(ObservationAvailability.OBSERVED, ("NOTICE",)))
    assert classify_existing_install(staged).reason_codes == ("local_changes_present", "tracked_changes", "untracked_paths", "staged_changes")
    for relation, kind in ((RepositoryRelation.REVIEWED_HISTORICAL, ExistingInstallClass.REVIEWED_HISTORICAL_REPOSITORY), (RepositoryRelation.OTHER, ExistingInstallClass.UNKNOWN_REPOSITORY_CANONICAL_COMMIT)):
        _assert_widened_row(base, relation, kind)


def _assert_widened_row(base: ExistingInstallFacts, relation: RepositoryRelation, kind: ExistingInstallClass) -> None:
    """Rows 8/9 (widened): both local-state tree conditions classify to the row, blocked, with the untracked facet."""
    for tree in (WorkingTreeCondition.UNTRACKED_ONLY, WorkingTreeCondition.MIXED_CHANGES):
        tracked = ("root_manifest.yaml",) if tree is WorkingTreeCondition.MIXED_CHANGES else ()
        facts = replace(base, repository_relation=relation, working_tree=tree, tracked_paths=ObservedPaths(ObservationAvailability.OBSERVED, tracked), untracked_paths=ObservedPaths(ObservationAvailability.OBSERVED, (".gitignore",)))
        result = classify_existing_install(facts)
        assert result.installation_class is kind and result.update_disposition == "blocked"
        assert result.reason_codes[0] == kind.value and "untracked_paths" in result.reason_codes


def _assert_row_four_and_nine(witnesses: dict[ExistingInstallClass, ExistingInstallFacts]) -> None:
    # R6's explicit row-4/row-9 discriminator changes only repository relation.
    row4 = witnesses[ExistingInstallClass.INSPECTION_INCOMPLETE]
    row9 = replace(row4, repository_relation=RepositoryRelation.OTHER)
    assert (
        classify_existing_install(row4).installation_class
        is ExistingInstallClass.INSPECTION_INCOMPLETE
    )
    assert (
        classify_existing_install(row9).installation_class
        is ExistingInstallClass.UNKNOWN_REPOSITORY_CANONICAL_COMMIT
    )


def _assert_pairwise_rows(witnesses: dict[ExistingInstallClass, ExistingInstallFacts]) -> None:
    # Every constructible witness reaches one literal conjunction, not a priority fallthrough.
    for left, left_facts in witnesses.items():
        for right in witnesses:
            if left is not right:
                assert classify_existing_install(left_facts).installation_class is not right


def _assert_invalid_observation() -> None:
    try:
        ObservedPaths(ObservationAvailability.MISSING, ("must-not-be-present",))
    except ValueError as exc:
        assert str(exc) == "inconsistent_existing_install_facts"
    else:
        raise AssertionError("missing observation accepted values")


def _assert_call_boundary() -> None:
    production = _ROOT / "solet_cli" / "src" / "solet_manager" / "existing_install_inspection.py"
    tree = ast.parse(production.read_text(encoding="utf-8"))
    banned = {"parse_seed_lock", "load_seed_lock", "read_maintenance_inventory"}
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & banned


def _assert_exit_precedence() -> None:
    attention = ExistingInstallClassification(
        ExistingInstallClass.INSPECTION_INCOMPLETE, (), "diagnostic_only", "blocked", True
    )
    complete = ExistingInstallClassification(
        ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE,
        (),
        "allow",
        "allowed_after_import",
        False,
    )
    failed = _check(InspectionStatus.FAILED)
    invalid = _check(InspectionStatus.FAILED, reason="target_identity_invalid")
    assert _reduce_exit((failed, invalid), attention)[0] == 2
    assert _reduce_exit((failed,), attention)[0] == 1
    assert _reduce_exit((_check(InspectionStatus.MISSING),), complete)[0] == 3
    assert _reduce_exit((), complete)[0] == 0
    # A malformed provenance never outranks a later target namespace mismatch.
    assert _reduce_exit((_check(InspectionStatus.FAILED), invalid), attention)[0] == 2
    assert _reduce_exit((_check(InspectionStatus.FAILED, required=False),), complete)[0] == 0


def _assert_inventory_invariance() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        paths = {
            name: ManagerPaths(base / name, base / "state", base / "cache")
            for name in ("valid", "malformed", "absent")
        }
        paths["valid"].config_dir.mkdir()
        paths["valid"].registry_path.write_bytes(b'{"schema_version":2,"records":[]}')
        paths["malformed"].config_dir.mkdir()
        paths["malformed"].registry_path.write_bytes(b"not-json")
        checks = {
            name: _inventory_check(path, _ProductionInspectionEffectTracker())
            for name, path in paths.items()
        }
        assert checks["valid"].status is InspectionStatus.VERIFIED
        assert checks["malformed"].status is InspectionStatus.FAILED
        assert checks["absent"].status is InspectionStatus.MISSING
        normalized = _normalized_inventory_results(paths, checks)
        assert normalized[0] == normalized[1] == normalized[2]


def _assert_installed_wheel_record_proof() -> None:
    """Exercise the loader against a real wheel and its installed RECORD.

    A source checkout has no wheel RECORD and is deliberately not a valid
    authority.  This test therefore builds the candidate package, installs it
    into isolated roots, and proves the loader accepts only the intact
    wheel-owned metadata resource.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wheel_dir = root / "wheel"
        subprocess.run(
            (
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(wheel_dir),
                str(_ROOT / "solet_cli"),
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        wheel = next(wheel_dir.glob("solet_cli-*.whl"))
        for case in ("intact", "tampered", "unsafe_mode", "missing_record"):
            site = root / case / "site"
            subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--target",
                    str(site),
                    str(wheel),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            resource = (
                site
                / "solet_manager"
                / "released_metadata"
                / "existing_install_inspection_catalog.v1.json"
            )
            record = next(site.glob("solet_cli-*.dist-info/RECORD"))
            expected_error = ""
            if case == "tampered":
                resource.write_bytes(resource.read_bytes() + b" ")
                expected_error = "wheel RECORD mismatch"
            elif case == "unsafe_mode":
                resource.chmod(0o666)
                expected_error = "writable by group or other"
            elif case == "missing_record":
                # A real Homebrew-poured keg measurably omits RECORD (see
                # _read_recorded_package_bytes); the loader falls back to the
                # ownership/symlink/write-bit checks alone and succeeds.
                record.unlink()
            completed = _run_installed_wheel_reader(site)
            if expected_error:
                assert completed.returncode != 0
                assert expected_error in completed.stderr
            else:
                assert completed.returncode == 0, completed.stderr


def _assert_released_pre_manager_anchor_proof() -> None:
    """Use the installed wheel's actual anchor file, never injected metadata."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wheel_dir = root / "wheel"
        subprocess.run(
            (
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(wheel_dir),
                str(_ROOT / "solet_cli"),
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        site = root / "site"
        subprocess.run(
            (
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--target",
                str(site),
                str(next(wheel_dir.glob("solet_cli-*.whl"))),
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        seed = site / "share" / "solet" / "seed.lock.json"
        seed.parent.mkdir(parents=True)
        seed_bytes = _installed_seed_lock_bytes()
        seed.write_bytes(seed_bytes)
        catalog = site / "share" / "solet" / "existing_install_inspection_seed_lock_catalog.v1.json"
        catalog.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "channels": [
                        {
                            "channel_id": "stable",
                            "seed_lock_sha256": hashlib.sha256(seed_bytes).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        target = root / "target"
        _create_released_anchor_target(target)
        completed = _run_installed_anchor_inspection(site, target)
        assert completed.returncode == 0, completed.stderr


def _installed_seed_lock_bytes() -> bytes:
    path = (
        _ROOT
        / "solet_cli"
        / "tests"
        / "fixtures"
        / "existing_install_inspection"
        / "installed_seed.lock.json"
    )
    value = path.read_bytes()
    assert (
        hashlib.sha256(value).hexdigest()
        == "ed6f512cdf25de483d5fb3b090f2a133e324214a8d95e2c7cf9bd9b024f464ab"
    )
    return value


def _create_released_anchor_target(target: Path) -> None:
    target.mkdir()
    subprocess.run(("git", "init", "--quiet", str(target)), check=True)
    subprocess.run(("git", "-C", str(target), "config", "user.name", "Inspection"), check=True)
    subprocess.run(
        ("git", "-C", str(target), "config", "user.email", "inspection@example.invalid"), check=True
    )
    provenance = {
        "ancestry": [],
        "bundle": {"name": "macos-bizops", "platform": "local"},
        "lineage": [],
        "manifest_sha256": "b" * 64,
        "origin_id": "123e4567-e89b-12d3-a456-426614174001",
        "schema_version": 1,
        "seed_id": "bb782efa-a331-5748-bc29-7f012b9a5f9d",
        "signature": None,
        "source_commit": "a" * 40,
        "source_date": "2026-09-16T00:00:00+00:00",
    }
    (target / "PROVENANCE.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    subprocess.run(("git", "-C", str(target), "add", "PROVENANCE.json"), check=True)
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Inspection",
            "GIT_AUTHOR_EMAIL": "inspection@example.invalid",
            "GIT_COMMITTER_NAME": "Inspection",
            "GIT_COMMITTER_EMAIL": "inspection@example.invalid",
            "GIT_AUTHOR_DATE": "2026-09-16T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-09-16T00:00:00+0000",
        }
    )
    message = "\n".join(
        (
            "Seed bundle (factory-sealed)",
            "",
            "Seed-Id: bb782efa-a331-5748-bc29-7f012b9a5f9d",
            "Origin-Id: 123e4567-e89b-12d3-a456-426614174001",
            "Manifest-SHA256: " + "b" * 64,
            "Assembled-Ref: " + "a" * 40,
            "License-Policy: public_apache",
            "Minted-At: 2026-09-16T00:00:00+00:00",
        )
    )
    subprocess.run(
        ("git", "-C", str(target), "commit", "--quiet", "-m", message), check=True, env=environment
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(target),
            "remote",
            "add",
            "origin",
            "https://github.com/solet-public/macos-bizops.git",
        ),
        check=True,
    )
    assert (
        subprocess.run(
            ("git", "-C", str(target), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "fab22b6f2c832a5b86176f6916166f6c6bc7677b"
    )


def _run_installed_anchor_inspection(site: Path, target: Path) -> subprocess.CompletedProcess[str]:
    source = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            f"sys.prefix = {str(site)!r}",
            (
                "from solet_manager.existing_install_inspection import "
                "ExistingInstallInspectionRequest, inspect_existing_install, "
                "load_installed_inspection_metadata"
            ),
            "from solet_manager.paths import ManagerPaths",
            f"target = Path({str(target)!r})",
            (
                f"paths = ManagerPaths(Path({str(site / 'manager' / 'config')!r}), "
                f"Path({str(site / 'manager' / 'state')!r}), "
                f"Path({str(site / 'manager' / 'cache')!r}))"
            ),
            (
                "result = inspect_existing_install(ExistingInstallInspectionRequest(target, "
                "'stable', paths), metadata_loader=load_installed_inspection_metadata)"
            ),
            "assert result.facts.anchor_kind.value == 'pre_manager_seed'",
            "assert result.facts.identity_status.value == 'verified'",
            "assert result.classification.installation_class.value == 'pre_manager_seed_clone'",
        )
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(site), str(_ROOT / "solet_setup_contracts" / "src"))
    )
    return subprocess.run(
        (sys.executable, "-c", source),
        check=False,
        capture_output=True,
        text=True,
        cwd=site.parent,
        env=environment,
    )


def _run_installed_wheel_reader(site: Path) -> subprocess.CompletedProcess[str]:
    source = "\n".join(
        (
            "import hashlib",
            "from solet_manager._existing_install_inspection_metadata import _package_bytes",
            "class Tracker:",
            "    def record_resource_read(self, value): pass",
            "raw = _package_bytes('existing_install_inspection_catalog.v1.json', Tracker())",
            "assert hashlib.sha256(raw).hexdigest() == '"
            + hashlib.sha256(
                (
                    _ROOT
                    / "solet_cli"
                    / "src"
                    / "solet_manager"
                    / "released_metadata"
                    / "existing_install_inspection_catalog.v1.json"
                ).read_bytes()
            ).hexdigest()
            + "'",
        )
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(site), str(_ROOT / "solet_setup_contracts" / "src"))
    )
    return subprocess.run(
        (sys.executable, "-c", source),
        check=False,
        capture_output=True,
        text=True,
        cwd=site.parent,
        env=environment,
    )


def _normalized_inventory_results(
    paths: dict[str, ManagerPaths], checks: dict[str, InspectionCheck]
) -> list[bytes]:
    results = [
        _result_with_inventory(checks[name], paths[name])
        for name in ("valid", "malformed", "absent")
    ]
    normalized = []
    for result in results:
        payload = json.loads(_canonical_result(result))
        result_checks = payload["data"]["checks"]
        assert len(result_checks) == 2 and result_checks[1]["check_id"] == "manager_inventory"
        result_checks[1] = {"manager_inventory": "sentinel"}
        normalized.append(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return normalized


def main() -> int:
    _assert_fixture_repositories()
    _assert_classifications()
    _assert_call_boundary()
    _assert_exit_precedence()
    _assert_inventory_invariance()
    _assert_installed_wheel_record_proof()
    _assert_released_pre_manager_anchor_proof()
    print("existing_install_inspection_contract_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
