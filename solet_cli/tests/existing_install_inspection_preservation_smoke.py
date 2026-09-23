"""Effect tracker smoke test: forbidden capabilities fail before a result."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Never, cast
from unittest.mock import patch

from existing_install_inspection_call_boundary import called_symbols as _called_symbols

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.existing_install_inspection import (  # noqa: E402, I001
    InspectionBoundaryViolation,
    ChannelInspectionIdentity,
    ExistingInstallContractIdentity,
    InspectionProbe,
    InspectionProbeOutput,
    InspectionEffectTracker,
    InspectionAnchor,
    InspectionAnchorKind,
    ChannelRelation,
    InspectionPreservationFacts,
    InstalledInspectionMetadata,
    PinnedInspectionDirectory,
    ReadOnlyInspectionRunner,
    ExistingInstallInspectionRequest,
    ExistingInstallInspectionResult,
    PreservationEffect,
    _ProductionInspectionEffectTracker,
    _pin_target_directory,
    inspect_existing_install,
    subprocess_read_only_inspection_runner,
)
from solet_manager._existing_install_inspection_target import target_checks  # noqa: E402, I001
from solet_manager.maintenance_inventory import read_maintenance_inventory  # noqa: E402, I001
from solet_manager.paths import ManagerPaths  # noqa: E402, I001
from solet_manager.release_lock import load_seed_lock  # noqa: E402, I001
from solet_manager.release_lock import SeedLock  # noqa: E402, I001
from solet_manager.seed_lock_parser import parse_seed_lock  # noqa: E402, I001


def _assert_forbidden_effects() -> None:
    counter_names = {
        PreservationEffect.TARGET_BYTE_WRITE: "target_byte_writes",
        PreservationEffect.MANAGER_STATE_WRITE: "manager_state_writes",
        PreservationEffect.SECRET_VALUE_READ: "secret_value_reads",
        PreservationEffect.SECRET_VALUE_WRITE: "secret_value_writes",
        PreservationEffect.DATABASE_READ: "database_reads",
        PreservationEffect.DATABASE_WRITE: "database_writes",
        PreservationEffect.TARGET_PROCESS_EXECUTION: "target_process_executions",
        PreservationEffect.PERMISSION_PROMPT: "permission_prompts",
    }
    for effect, counter_name in counter_names.items():
        tracker = _ProductionInspectionEffectTracker()
        tracker.record_resource_read("target_directory")
        try:
            tracker.record_forbidden(effect)
        except InspectionBoundaryViolation:
            pass
        else:
            raise AssertionError(f"forbidden {effect.value} did not fail")
        assert getattr(tracker.snapshot(), counter_name) == 1


def _assert_call_boundary() -> None:
    production_root = _ROOT / "solet_cli" / "src" / "solet_manager"
    production = tuple(
        production_root / name
        for name in (
            "existing_install_inspection.py",
            "_existing_install_inspection_target.py",
            "_existing_install_inspection_metadata.py",
        )
    )
    # These names cover every forbidden capability family, including the
    # coding-agent state surfaces that must remain unreachable from inspection.
    forbidden_calls = {
        "target_adapter",
        "target_cli",
        "target_python",
        "genesis",
        "credential_generator",
        "keychain",
        "state_management_interface",
        "database_tool",
        "package_install",
        "launchctl",
        "shell",
        "router",
        "service",
        "network",
        "generic_subprocess",
        "codex_config",
        "codex_sessions",
        "codex_history",
        "claude_settings",
        "claude_plugins",
        "claude_projects",
        "claude_history",
        "parse_seed_lock",
        "_read_lock",
        "load_seed_lock",
        "read_maintenance_inventory",
    }
    called = {
        symbol
        for path in production
        for symbol in _called_symbols(ast.parse(path.read_text(encoding="utf-8")))
    }
    _assert_no_forbidden_calls(called, forbidden_calls)
    assert "ManagerPaths.resolve" not in called
    _assert_adversarial_forbidden_fixtures(forbidden_calls)
    _assert_alias_resolution_bounds()
    _assert_scope_local_shadowing_bounds()


def _assert_no_forbidden_calls(called: set[str], forbidden_calls: set[str]) -> None:
    assert not {item.rsplit(".", 1)[-1] for item in called} & forbidden_calls


def _assert_adversarial_forbidden_fixtures(forbidden_calls: set[str]) -> None:
    fixtures = (
        ("dynamic_forbidden_call_fixture.py", "target_adapter.target_adapter"),
        ("partial_forbidden_call_fixture.py", "target_adapter.target_adapter"),
        ("stored_attribute_forbidden_call_fixture.py", "target_adapter.target_adapter"),
        ("stored_getattr_forbidden_call_fixture.py", "target_adapter.target_adapter"),
        ("methodcaller_forbidden_call_fixture.py", "methodcaller.target_adapter"),
        ("definition_header_forbidden_call_fixture.py", "target_adapter.target_adapter"),
        ("reimport_ordering_forbidden_call_fixture.py", "target_adapter.target_adapter"),
        ("assign_target_forbidden_call_fixture.py", "target_adapter.target_adapter"),
        ("annassign_target_forbidden_call_fixture.py", "target_adapter.target_adapter"),
        ("lambda_default_forbidden_call_fixture.py", "target_adapter.target_adapter"),
    )
    fixture_directory = _ROOT / "solet_cli" / "tests" / "fixtures" / "existing_install_inspection"
    for name, forbidden_symbol in fixtures:
        fixture = fixture_directory / name
        called = _called_symbols(ast.parse(fixture.read_text(encoding="utf-8")))
        assert forbidden_symbol in called
        try:
            _assert_no_forbidden_calls(called, forbidden_calls)
        except AssertionError:
            continue
        raise AssertionError(f"{name} bypassed the preservation boundary")
    _assert_nested_rebind_fixture(fixture_directory)
    _assert_lexical_alias_fixture(fixture_directory)
    _assert_import_alias_shadow_fixtures(fixture_directory)
    _assert_unresolvable_shadow_fixtures(fixture_directory)
    _assert_reimport_ordering_fixture(fixture_directory)
    _assert_round14_control_flow_and_scope_fixtures(fixture_directory)
    _assert_round15_iteration_and_exception_fixtures(fixture_directory)
    _assert_match_guard_walrus_fallthrough()


def _assert_nested_rebind_fixture(fixture_directory: Path) -> None:
    fixture = fixture_directory / "nested_rebind_forbidden_call_fixture.py"
    called = _called_symbols(ast.parse(fixture.read_text(encoding="utf-8")))
    assert "<dynamic>" in called
    assert "safe_adapter.harmless_method" not in called


def _assert_lexical_alias_fixture(fixture_directory: Path) -> None:
    fixture = fixture_directory / "lexical_alias_scope_fixture.py"
    called = _called_symbols(ast.parse(fixture.read_text(encoding="utf-8")))
    assert "safe_paths.ManagerPaths.resolve" in called
    assert "target_adapter.resolve" in called


def _assert_import_alias_shadow_fixtures(fixture_directory: Path) -> None:
    assign_fixture = fixture_directory / "import_alias_assign_shadow_fixture.py"
    assign_called = _called_symbols(ast.parse(assign_fixture.read_text(encoding="utf-8")))
    assert "target_adapter.target_adapter" in assign_called
    assert "safe_adapter.harmless_method" not in assign_called
    for name in (
        "import_alias_for_shadow_fixture.py",
        "import_alias_parameter_shadow_fixture.py",
    ):
        called = _called_symbols(ast.parse((fixture_directory / name).read_text(encoding="utf-8")))
        assert "<dynamic>" in called
        assert "safe_adapter.harmless_method" not in called


def _assert_unresolvable_shadow_fixtures(fixture_directory: Path) -> None:
    for name in (
        "walrus_shadow_fixture.py",
        "annotated_assign_shadow_fixture.py",
        "except_shadow_fixture.py",
    ):
        called = _called_symbols(ast.parse((fixture_directory / name).read_text(encoding="utf-8")))
        assert "<dynamic>" in called
        assert "safe_adapter.harmless_method" not in called


def _assert_reimport_ordering_fixture(fixture_directory: Path) -> None:
    fixture = fixture_directory / "reimport_ordering_forbidden_call_fixture.py"
    called = _called_symbols(ast.parse(fixture.read_text(encoding="utf-8")))
    assert "target_adapter.target_adapter" in called
    assert "safe_adapter.harmless_method" in called


def _assert_round14_control_flow_and_scope_fixtures(fixture_directory: Path) -> None:
    """Exercise conservative joins and writes that escape local scope state."""
    for name in (
        "for_else_shadow_fixture.py",
        "while_else_shadow_fixture.py",
        "comprehension_walrus_escape_fixture.py",
        "global_nonlocal_rebind_fixture.py",
    ):
        called = _called_symbols(ast.parse((fixture_directory / name).read_text(encoding="utf-8")))
        assert "<dynamic>" in called
        assert "safe_adapter.harmless_method" not in called


def _assert_round15_iteration_and_exception_fixtures(fixture_directory: Path) -> None:
    iteration_called = _called_symbols(
        ast.parse(
            (fixture_directory / "loop_carried_iteration_fixture.py").read_text(encoding="utf-8")
        )
    )
    assert "safe_adapter.harmless_method" in iteration_called
    assert "<dynamic>" in iteration_called
    for name in ("break_skips_orelse_fixture.py", "except_try_prefix_rebind_fixture.py"):
        called = _called_symbols(ast.parse((fixture_directory / name).read_text(encoding="utf-8")))
        assert "<dynamic>" in called
        assert "safe_adapter.harmless_method" not in called


def _assert_match_guard_walrus_fallthrough() -> None:
    """A failed guard's walrus binding must reach the later-case call state."""
    called = _called_symbols(
        ast.parse(
            """
from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

match 0:
    case 0 if (slot := forbidden) == 0:
        pass
    case _:
        slot()
"""
        )
    )
    assert "<dynamic>" in called
    assert "safe_adapter.harmless_method" not in called


def _assert_alias_resolution_bounds() -> None:
    tree = ast.parse(
        """
import safe_adapter as safe
import target_adapter as x
single = x.target_adapter
single()
rebound = safe.harmless_method
if True:
    rebound = x.target_adapter
rebound()
def inner():
    local = x.target_adapter
    local()
"""
    )
    called = _called_symbols(tree)
    assert "target_adapter.target_adapter" in called
    assert "<dynamic>" in called
    assert "safe_adapter.harmless_method" not in called
    conditional_tree = ast.parse(
        """
import target_adapter as x
if enabled:
    conditional = x.target_adapter
conditional()
"""
    )
    assert _called_symbols(conditional_tree) == {"<dynamic>"}


def _assert_scope_local_shadowing_bounds() -> None:
    import_prefix = """
from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden
"""
    shadowing_sources = (
        """
with source as slot:
    slot()
""",
        """
[slot() for slot in (forbidden,)]
""",
        """
(lambda slot: slot())(forbidden)
""",
        """
match forbidden:
    case slot:
        slot()
""",
    )
    for source in shadowing_sources:
        called = _called_symbols(ast.parse(import_prefix + source))
        assert "<dynamic>" in called
        assert "safe_adapter.harmless_method" not in called


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, str], ...]:
    """Record every path's mode and bytes, including the complete .git tree."""
    rows: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = metadata.st_mode & 0o7777
        if path.is_symlink():
            digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = "directory"
        rows.append((relative, mode, digest))
    return tuple(rows)


def _fail_on_call(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("inspection reached a forbidden path-reading facade")


def _make_valid_target(root: Path) -> tuple[Path, InstalledInspectionMetadata]:
    target = root / "target"
    target.mkdir()
    subprocess.run(("git", "init", "--quiet", str(target)), check=True)
    subprocess.run(("git", "-C", str(target), "config", "user.name", "Inspection"), check=True)
    subprocess.run(
        ("git", "-C", str(target), "config", "user.email", "inspection@example.invalid"),
        check=True,
    )
    origin_id = "123e4567-e89b-12d3-a456-426614174001"
    source_commit, manifest_sha256 = "a" * 40, "b" * 64
    seed_id = str(uuid.uuid5(uuid.UUID(origin_id), f"{source_commit}:{manifest_sha256}::"))
    stamp = {
        "ancestry": [],
        "bundle": {"name": "macos-bizops", "platform": "local"},
        "lineage": [],
        "manifest_sha256": manifest_sha256,
        "origin_id": origin_id,
        "schema_version": 1,
        "seed_id": seed_id,
        "signature": None,
        "source_commit": source_commit,
        "source_date": "2026-09-16T00:00:00+00:00",
    }
    provenance = (json.dumps(stamp, indent=2, sort_keys=True) + "\n").encode()
    (target / "PROVENANCE.json").write_bytes(provenance)
    subprocess.run(("git", "-C", str(target), "add", "PROVENANCE.json"), check=True)
    message = "\n".join(
        (
            "Seed bundle (factory-sealed)",
            "",
            f"Seed-Id: {seed_id}",
            f"Origin-Id: {origin_id}",
            f"Manifest-SHA256: {manifest_sha256}",
            f"Assembled-Ref: {source_commit}",
            "License-Policy: public_apache",
            "Minted-At: 2026-09-16T00:00:00+00:00",
        )
    )
    subprocess.run(("git", "-C", str(target), "commit", "--quiet", "-m", message), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(target),
            "remote",
            "add",
            "origin",
            "https://example.invalid/repository.git",
        ),
        check=True,
    )
    head = subprocess.run(
        ("git", "-C", str(target), "rev-parse", "HEAD"), check=True, capture_output=True, text=True
    ).stdout.strip()
    tree = subprocess.run(
        ("git", "-C", str(target), "rev-parse", "HEAD^{tree}"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    metadata = InstalledInspectionMetadata(
        ChannelInspectionIdentity(
            "stable",
            "https://example.invalid/repository.git",
            "r1",
            head,
            tree,
            "macos-bizops",
            hashlib.sha256(provenance).hexdigest(),
            seed_id,
            origin_id,
            manifest_sha256,
            ExistingInstallContractIdentity("existing-install", 1, "sha256:" + "c" * 64),
            "catalog",
            "d" * 64,
            "seed",
            "e" * 64,
            "sha256:" + "2" * 64,
            "anchors",
            "f" * 64,
        ),
        cast(SeedLock, None),
        (),
    )
    return target, metadata


def _assert_production_run_preserves_every_observed_tree() -> None:
    """Run the production facade and compare recursive target/manager snapshots."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target, metadata = _make_valid_target(root)
        paths = ManagerPaths(
            root / "manager" / "config", root / "manager" / "state", root / "manager" / "cache"
        )
        paths.config_dir.mkdir(parents=True)
        paths.state_dir.mkdir(parents=True)
        paths.cache_dir.mkdir(parents=True)
        paths.registry_path.write_bytes(b'{"schema_version":2,"records":[]}')
        (paths.transactions_dir).mkdir()
        (paths.transactions_dir / "journal.json").write_bytes(b"journal")
        (paths.locks_dir).mkdir()
        (paths.locks_dir / "inspect.lock").write_bytes(b"lock")
        (paths.cache_dir / "contract-cache").write_bytes(b"cache")
        before_target = _tree_snapshot(target)
        before_manager = _tree_snapshot(root / "manager")
        tracker = _ProductionInspectionEffectTracker()
        with ExitStack() as stack:
            stack.enter_context(
                patch("solet_manager.seed_lock_parser.parse_seed_lock", _fail_on_call)
            )
            stack.enter_context(patch("solet_manager.release_lock.load_seed_lock", _fail_on_call))
            stack.enter_context(
                patch(
                    "solet_manager.maintenance_inventory.read_maintenance_inventory", _fail_on_call
                )
            )
            result = inspect_existing_install(
                ExistingInstallInspectionRequest(target, "stable", paths),
                metadata_loader=lambda channel, tracker: metadata,
                effect_tracker=tracker,
            )
        _assert_production_result(result, tracker)
        assert before_target == _tree_snapshot(target)
        assert before_manager == _tree_snapshot(root / "manager")


def _assert_production_result(
    result: ExistingInstallInspectionResult, tracker: _ProductionInspectionEffectTracker
) -> None:
    preservation = result.preservation
    assert preservation == tracker.snapshot()
    assert result.facts.identity_status.value == "verified"
    assert result.to_command_result().exit_code == 0
    source = cast(dict[str, object], result.to_command_result().data["source"])
    assert source["branch"] is not None
    assert source["origins"] == ["https://example.invalid/repository.git"]
    counts = cast(dict[str, int], result.to_command_result().data["counts"])
    assert counts["total"] == sum(value for key, value in counts.items() if key != "total")
    _assert_exact_read_accounting(preservation)
    _assert_zero_forbidden_counts(preservation)


def _assert_exact_read_accounting(preservation: InspectionPreservationFacts) -> None:
    assert preservation.opened_resources.count("target_directory") == 1
    assert preservation.opened_resources.count("target:PROVENANCE.json") == 1
    assert preservation.opened_resources.count("manager_inventory") == 1


def _assert_zero_forbidden_counts(preservation: InspectionPreservationFacts) -> None:
    assert preservation.target_byte_writes == 0
    assert preservation.manager_state_writes == 0
    assert preservation.secret_value_reads == 0
    assert preservation.secret_value_writes == 0
    assert preservation.database_reads == 0
    assert preservation.database_writes == 0
    assert preservation.target_process_executions == 0
    assert preservation.permission_prompts == 0


def _assert_forbidden_path_facades_are_not_used() -> None:
    """Keep the imported facade symbols referenced so accidental use is caught."""
    assert callable(parse_seed_lock)
    assert callable(load_seed_lock)
    assert callable(read_maintenance_inventory)


def _assert_detached_and_anchor_production_paths() -> None:
    """Exercise the production probes which were previously classifier-only."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target, metadata = _make_valid_target(root)
        paths = ManagerPaths(
            root / "manager" / "state", root / "manager" / "state", root / "manager" / "cache"
        )
        paths.config_dir.mkdir(parents=True)
        subprocess.run(("git", "-C", str(target), "checkout", "--detach", "--quiet"), check=True)
        detached = inspect_existing_install(
            ExistingInstallInspectionRequest(target, "stable", paths),
            metadata_loader=lambda channel, tracker: metadata,
        )
        assert detached.facts.detached.value == "true"
        assert detached.classification.installation_class.value == "blocking_local_state"
        assert detached.to_command_result().exit_code == 3
        subprocess.run(("git", "-C", str(target), "checkout", "--quiet", "-"), check=True)
        anchor = InspectionAnchor(
            "pre-manager",
            InspectionAnchorKind.PRE_MANAGER_SEED,
            "stable",
            metadata.channel_identity.repository,
            metadata.channel_identity.commit,
            metadata.channel_identity.tree_hash,
            metadata.channel_identity.provenance_sha256,
            metadata.channel_identity.seed_id,
            metadata.channel_identity.origin_id,
            metadata.channel_identity.manifest_sha256,
            ChannelRelation.FAST_FORWARD,
            (),
        )
        anchored_metadata = InstalledInspectionMetadata(
            replace(metadata.channel_identity, commit="0" * 40, tree_hash="1" * 40),
            metadata.seed_lock,
            (anchor,),
        )
        anchored = inspect_existing_install(
            ExistingInstallInspectionRequest(target, "stable", paths),
            metadata_loader=lambda channel, tracker: anchored_metadata,
        )
        assert anchored.facts.anchor_kind is InspectionAnchorKind.PRE_MANAGER_SEED
        assert anchored.facts.identity_status.value == "verified"


def _assert_toctou_pinning() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        adversary = root / "adversary"
        target.mkdir()
        adversary.mkdir()
        (target / "sentinel").write_text("original", encoding="utf-8")
        (adversary / "sentinel").write_text("adversarial", encoding="utf-8")
        tracker = _ProductionInspectionEffectTracker()
        pinned, _identity = _pin_target_directory(target, tracker)
        try:
            os.rename(target, root / "original_parked")
            os.rename(adversary, target)
            with pinned.open_readonly(PurePosixPath("sentinel")) as source:
                assert source.read() == b"original"
            assert not pinned.namespace_matches()
        finally:
            pinned.close()


def _assert_pinned_git_probe() -> None:
    """A Git subprocess must use the pinned directory descriptor as its cwd."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "target"
        target.mkdir()
        subprocess.run(("git", "init", "--quiet", str(target)), check=True)
        subprocess.run(("git", "-C", str(target), "config", "user.name", "Inspection"), check=True)
        subprocess.run(
            ("git", "-C", str(target), "config", "user.email", "inspection@example.invalid"),
            check=True,
        )
        (target / "probe.txt").write_text("pinned git probe\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(target), "add", "probe.txt"), check=True)
        subprocess.run(("git", "-C", str(target), "commit", "--quiet", "-m", "probe"), check=True)
        tracker = _ProductionInspectionEffectTracker()
        pinned, _identity = _pin_target_directory(target, tracker)
        try:
            observed = subprocess_read_only_inspection_runner(InspectionProbe.HEAD_COMMIT, pinned)
        finally:
            pinned.close()
        assert observed.returncode == 0, observed.stderr.decode("utf-8", "replace")
        assert len(observed.stdout.decode("ascii").strip()) == 40


def _assert_working_provenance_is_an_identity_conjunct() -> None:
    """A changed working stamp must fail identity even with a matching HEAD."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "target"
        target.mkdir()
        subprocess.run(("git", "init", "--quiet", str(target)), check=True)
        subprocess.run(("git", "-C", str(target), "config", "user.name", "Inspection"), check=True)
        subprocess.run(
            ("git", "-C", str(target), "config", "user.email", "inspection@example.invalid"),
            check=True,
        )
        origin_id = "123e4567-e89b-12d3-a456-426614174001"
        source_commit, manifest_sha256 = "a" * 40, "b" * 64
        seed_id = str(uuid.uuid5(uuid.UUID(origin_id), f"{source_commit}:{manifest_sha256}::"))
        stamp = {
            "ancestry": [],
            "bundle": {"name": "macos-bizops", "platform": "local"},
            "lineage": [],
            "manifest_sha256": manifest_sha256,
            "origin_id": origin_id,
            "schema_version": 1,
            "seed_id": seed_id,
            "signature": None,
            "source_commit": source_commit,
            "source_date": "2026-09-16T00:00:00+00:00",
        }
        provenance = (json.dumps(stamp, indent=2, sort_keys=True) + "\n").encode()
        (target / "PROVENANCE.json").write_bytes(provenance)
        subprocess.run(("git", "-C", str(target), "add", "PROVENANCE.json"), check=True)
        message = "\n".join(
            (
                "Seed bundle (factory-sealed)",
                "",
                f"Seed-Id: {seed_id}",
                f"Origin-Id: {origin_id}",
                f"Manifest-SHA256: {manifest_sha256}",
                f"Assembled-Ref: {source_commit}",
                "License-Policy: public_apache",
                "Minted-At: 2026-09-16T00:00:00+00:00",
            )
        )
        subprocess.run(("git", "-C", str(target), "commit", "--quiet", "-m", message), check=True)
        head = subprocess.run(
            ("git", "-C", str(target), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ("git", "-C", str(target), "rev-parse", "HEAD^{tree}"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        metadata = InstalledInspectionMetadata(
            ChannelInspectionIdentity(
                "stable",
                "https://example.invalid/repository.git",
                "r1",
                head,
                tree,
                "macos-bizops",
                hashlib.sha256(provenance).hexdigest(),
                seed_id,
                "123e4567-e89b-12d3-a456-426614174001",
                "b" * 64,
                ExistingInstallContractIdentity("existing-install", 1, "sha256:" + "b" * 64),
                "catalog",
                "c" * 64,
                "seed",
                "d" * 64,
                "sha256:" + "2" * 64,
                "anchors",
                "e" * 64,
            ),
            cast(SeedLock, None),
            (),
        )
        tracker = _ProductionInspectionEffectTracker()
        pinned, _identity = _pin_target_directory(target, tracker)
        try:

            def run_probe(
                runner: ReadOnlyInspectionRunner,
                probe: InspectionProbe,
                pinned_target: PinnedInspectionDirectory,
                tracker: InspectionEffectTracker,
            ) -> InspectionProbeOutput:
                return subprocess_read_only_inspection_runner(probe, pinned_target)

            checks, values = target_checks(
                pinned, metadata, subprocess_read_only_inspection_runner, tracker, run_probe
            )
            assert values["identity"] is True
            assert all(check.status.value == "verified" for check in checks[:5])
            (target / "PROVENANCE.json").write_bytes(provenance + b"\n")
            checks, values = target_checks(
                pinned, metadata, subprocess_read_only_inspection_runner, tracker, run_probe
            )
            assert values["identity"] is False
            assert (
                next(
                    check for check in checks if check.check_id == "working_provenance"
                ).status.value
                == "failed"
            )
        finally:
            pinned.close()


def main() -> int:
    _assert_forbidden_effects()
    _assert_call_boundary()
    _assert_forbidden_path_facades_are_not_used()
    _assert_toctou_pinning()
    _assert_pinned_git_probe()
    _assert_working_provenance_is_an_identity_conjunct()
    _assert_production_run_preserves_every_observed_tree()
    _assert_detached_and_anchor_production_paths()
    print("existing_install_inspection_preservation_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
