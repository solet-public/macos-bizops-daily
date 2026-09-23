"""Focused behavioral smoke for config, paths, plans, rendering, and dry-run."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

_PACKAGE_ROOT = Path(
    os.environ.get(
        "SOLET_MANAGER_TEST_PACKAGE_ROOT",
        Path(__file__).resolve().parents[1] / "src",
    )
)
sys.path.insert(0, str(_PACKAGE_ROOT))

from connector_graph_checks import (  # noqa: E402
    connector_graph_plan_check,
    declared_operation_probe_dispatch_check,
    genesis_topology_probe_activation_is_correct,
)
from operation_probe_adapter_checks import (  # noqa: E402
    operation_owned_probe_requests_use_probe_identity,
    planned_action_drift_uses_operation_route,
)
from solet_manager import (  # noqa: E402
    cli_commands,
    config_loading,
    preview_engine,
)
from solet_manager.adapters import (  # noqa: E402
    AdapterRegistry,
    OperationRequest,
    invoke_adapter,
)
from solet_manager.cli import _parse_decisions, build_parser, run  # noqa: E402
from solet_manager.config import CreateConfig, load_create_config  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.create import CreateManager  # noqa: E402
from solet_manager.doctor import InstallationDoctor  # noqa: E402
from solet_manager.errors import (  # noqa: E402
    ConfigError,
    ContractError,
    SourceError,
    StateConflictError,
)
from solet_manager.flow import (  # noqa: E402
    SetupPlan,
    active_probe_ids,
    build_setup_plan,
)
from solet_manager.lifecycle import LifecycleManager  # noqa: E402
from solet_manager.models import (  # noqa: E402
    CheckpointStatus,
    CommandResult,
    ExitCode,
    InstanceRecord,
)
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.plan_builder import remediation_operations  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import (  # noqa: E402
    SeedLock,
    load_seed_lock,
    seed_lock_from_identity,
)
from solet_manager.rendering import render_human, render_json  # noqa: E402
from solet_manager.transaction import (  # noqa: E402
    Transaction,
    canonical_sha256,
    write_transaction,
)

_REPO = Path(__file__).resolve().parents[2]
_CONTRACTS = _REPO / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS = 0

_RECONCILIATION_EXPECTED: dict[str, tuple[str, bool, str | None]] = {
    "unregistered_healthy": ("unregistered_runtime_healthy", False, "manager"),
    "registered_healthy": ("registered_runtime_healthy", True, None),
    "unregistered_dead": ("unregistered_runtime_dead", False, "manager_and_runtime"),
    "registered_dead": ("registered_runtime_dead", False, "runtime"),
    "registered_indeterminate": (
        "registered_runtime_indeterminate",
        False,
        "runtime_observation",
    ),
    "unregistered_indeterminate": (
        "unregistered_runtime_indeterminate",
        False,
        "runtime_observation",
    ),
    "registered_absent": ("registered_target_absent", False, "manager"),
    "absent": ("absent", True, None),
}

_RECONCILIATION_ACTION_FRAGMENTS = {
    "unregistered_healthy": "Do not restart, recreate, or delete the target.",
    "registered_healthy": "registration and health agree",
    "unregistered_dead": "Do not adopt, start, recreate, or delete",
    "registered_dead": "Preserve the manager record.",
    "registered_indeterminate": "Inspect the target-local health executable identity",
    "unregistered_indeterminate": "Inspect the target-local health executable identity",
    "registered_absent": "Preserve the manager record as evidence",
}

_RECONCILIATION_PUBLIC_ENVELOPES = {
    "registered_indeterminate": (
        "awaiting_user",
        ExitCode.HUMAN_ACTION,
        "instance_registered_runtime_indeterminate",
    ),
    "unregistered_indeterminate": (
        "awaiting_user",
        ExitCode.HUMAN_ACTION,
        "instance_unregistered_runtime_indeterminate",
    ),
    "registered_absent": (
        "failed",
        ExitCode.FAILED,
        "instance_registered_target_absent",
    ),
}


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _raises(error: type[BaseException], fn: object, label: str) -> None:
    try:
        fn()  # type: ignore[operator]
    except error:
        _check(True, label)
    else:
        _check(False, label)


def _seed_lock(
    path: Path,
    *,
    commit: str = "a" * 40,
    repository: str = "https://github.com/solet-public/macos-bizops.git",
    profile: str = "macos-bizops",
    archive_sha256: str | None = "c" * 64,
) -> None:
    lock: dict[str, object] = {
        "schema_version": 1,
        "repository": repository,
        "release_tag": "release-2026-08-20",
        "commit": commit,
        "tree_hash": "b" * 40,
        "profile": profile,
    }
    if archive_sha256 is not None:
        lock["archive_sha256"] = archive_sha256
    path.write_text(
        json.dumps(lock),
        encoding="utf-8",
    )


def _tagless_seed_lock(
    path: Path,
    *,
    repository: str = "https://github.com/solet-public/tagless-seed.git",
    profile: str = "tagless-seed",
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "repository": repository,
                "commit": "a" * 40,
                "tree_hash": "b" * 40,
                "profile": profile,
            }
        ),
        encoding="utf-8",
    )


def _decision_precedence_is_correct(config: CreateConfig) -> bool:
    return all((
        config.decisions
        == {"execution_topology": "solo", "coding_agents": ["claude_code", "codex"]},
        config.decision_sources == {"execution_topology": "config", "coding_agents": "flag"},
    ))


def _lifecycle_probe_filter_is_correct(bundle: ContractBundle) -> bool:
    free = active_probe_ids(
        bundle,
        {"setup_profile": "free", "coding_agents": ["codex"]},
        ("router_ready", "peer_identity_valid"),
    )
    business_profile = active_probe_ids(
        bundle,
        {"setup_profile": "macos-bizops", "coding_agents": ["codex"]},
        ("router_ready", "peer_identity_valid"),
    )
    return all((
        free == ("peer_identity_valid",),
        business_profile == ("router_ready", "peer_identity_valid"),
    ))


def _static_prompt_blocks(
    preview: CommandResult,
    prompts: object,
) -> bool:
    return bool(
        preview.error_kind == "decisions_required"
        and isinstance(preview.data.get("approval_fingerprint"), str)
        and isinstance(prompts, list)
        and prompts
        and isinstance(prompts[0], dict)
        and prompts[0].get("id") == "inference_implementation"
    )


def _flow_only_change_alters_plan(
    base: SetupPlan,
    changed: SetupPlan,
) -> bool:
    base_ids = {operation.operation_id for operation in base.operations}
    changed_ids = {operation.operation_id for operation in changed.operations}
    return all((
        "install_shell_integration" in base_ids,
        "build_instance_environment" in base_ids,
        "install_shell_integration" not in changed_ids,
    ))


def _tree_paths(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _tree_census(root: Path) -> tuple[tuple[str, str, int], ...]:
    census: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            census.append((relative, f"symlink:{os.readlink(path)}", path.lstat().st_mode))
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            census.append((relative, f"file:{digest}", path.stat().st_mode))
        elif path.is_dir():
            census.append((relative, "directory", path.stat().st_mode))
    return tuple(census)


def _instance_record(name: str, target: Path) -> InstanceRecord:
    return InstanceRecord(
        name=name,
        target=str(target),
        launcher=str(target / ".venv/bin/solet-bridge"),
        seed_repository="https://example.invalid/seed.git",
        seed_tag="release-fixture",
        seed_commit="a" * 40,
        seed_tree_hash="b" * 40,
        profile="fixture",
        flow_id="fixture.flow",
        flow_source_revision="c" * 40,
        flow_contract_digest="sha256:" + "d" * 64,
        created_at="2026-08-25T00:00:00Z",
        updated_at="2026-08-25T00:00:00Z",
    )


def _write_health_fixture(target: Path, *, status: str, exit_code: int = 0) -> None:
    executable = target / ".venv/bin/solet-bridge"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '{{\"status\":\"{status}\"}}'\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _fixture_transaction(*, name: str, target: Path) -> Transaction:
    seed = SeedLock(
        "https://example.invalid/seed.git",
        "release-fixture",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "fixture",
    )
    return Transaction.create(
        name=name,
        target=target,
        input_fingerprint="sha256:" + "d" * 64,
        answers={},
        seed=seed,
        flow_id="fixture.flow",
        flow_source_revision="c" * 40,
        flow_contract_digest="sha256:" + "d" * 64,
        stage_ids=("fixture",),
        completion_probe_ids=("fixture",),
    )


def _write_verified_transaction(paths: ManagerPaths, *, name: str, target: Path) -> None:
    transaction = _fixture_transaction(name=name, target=target).with_statuses(
        stages={"fixture": CheckpointStatus.VERIFIED},
        completion={"fixture": CheckpointStatus.VERIFIED},
    )
    write_transaction(paths.transaction_path(name), transaction)


def _public_status(
    *,
    manager_home: Path,
    subject_home: Path,
    name: str,
) -> CommandResult | None:
    try:
        with patch.object(Path, "home", return_value=subject_home):
            return run(["--home", str(manager_home), "status", name])
    except StateConflictError:
        return None


def _reconciliation(result: CommandResult | None) -> dict[str, object]:
    if result is None:
        return {}
    value = result.data.get("reconciliation")
    return value if isinstance(value, dict) else {}


def _prepare_reconciliation_runtime(target: Path, control: str) -> None:
    if control in {"unregistered_healthy", "registered_healthy"}:
        _write_health_fixture(target, status="healthy")
        return
    if control in {"unregistered_dead", "registered_dead"}:
        _write_health_fixture(target, status="unhealthy")
        return
    if control in {"registered_indeterminate", "unregistered_indeterminate"}:
        target.mkdir(parents=True)
        return
    if control not in {"absent", "registered_absent"}:
        raise AssertionError(f"unknown lifecycle reconciliation control: {control}")


def _prepare_reconciliation_manager(
    *,
    paths: ManagerPaths,
    name: str,
    target: Path,
    control: str,
) -> None:
    if control.startswith("registered_"):
        InstanceRegistry(paths.registry_path).add(_instance_record(name, target))
    if control == "registered_indeterminate":
        _write_verified_transaction(paths, name=name, target=target)


def _check_reconciliation_fields(
    *,
    control: str,
    name: str,
    result: CommandResult | None,
) -> None:
    reconciliation = _reconciliation(result)
    expected = _RECONCILIATION_EXPECTED[control]
    _check(
        all((
            reconciliation.get("classification") == expected[0],
            reconciliation.get("agreement") is expected[1],
            reconciliation.get("wrong_side") == expected[2],
        )),
        f"{control} identifies agreement and the wrong side; result={result}",
    )
    next_action = reconciliation.get("safe_next_action")
    action_fragment = _RECONCILIATION_ACTION_FRAGMENTS.get(
        control,
        f"solet create {name} --dry-run",
    )
    _check(
        isinstance(next_action, str) and action_fragment in next_action,
        f"{control} supplies a safe next action; result={result}",
    )


def _check_reconciliation_public_envelope(
    *,
    control: str,
    result: CommandResult | None,
) -> None:
    public_envelope = _RECONCILIATION_PUBLIC_ENVELOPES.get(control)
    if public_envelope is None or result is None:
        return
    _check(
        all((
            result.status == public_envelope[0],
            result.exit_code is public_envelope[1],
            result.error_kind == public_envelope[2],
        )),
        f"{control} public envelope refuses false green; result={result}",
    )


def _lifecycle_reconciliation_control(root: Path, control: str) -> None:
    case_root = root / f"reconciliation-{control}"
    manager_home = case_root / "manager"
    subject_home = case_root / "subject-home"
    name = "convergence-fixture"
    target = subject_home / "Solets" / name
    paths = ManagerPaths.resolve(environ={}, home=subject_home, explicit_home=manager_home)

    _prepare_reconciliation_runtime(target, control)
    _prepare_reconciliation_manager(
        paths=paths,
        name=name,
        target=target,
        control=control,
    )

    before = _tree_census(case_root)
    result = _public_status(
        manager_home=manager_home,
        subject_home=subject_home,
        name=name,
    )
    after = _tree_census(case_root)
    _check_reconciliation_fields(control=control, name=name, result=result)
    _check(
        before == after,
        f"{control} public status is read-only; before={before}, after={after}",
    )
    _check_reconciliation_public_envelope(control=control, result=result)


def _lifecycle_reconciliation_checks(root: Path) -> None:
    selected = os.environ.get("SOLET_MANAGER_RECONCILIATION_CONTROL")
    controls = (
        (selected,)
        if selected
        else (
            "unregistered_healthy",
            "registered_healthy",
            "unregistered_dead",
            "registered_dead",
            "registered_indeterminate",
            "unregistered_indeterminate",
            "registered_absent",
            "absent",
        )
    )
    for control in controls:
        _lifecycle_reconciliation_control(root, control)


def _provisional_manager_record_checks(root: Path) -> None:
    """A materialized transaction must be discoverable without being verified."""

    paths, name, transaction = _provisional_manager_record_fixture(root)
    status = LifecycleManager(paths).status(name)
    doctor = InstallationDoctor(paths=paths, contract_directory=None).run(name)
    instances = LifecycleManager(paths).list_instances().data["instances"]
    _assert_provisional_status(status, name)
    _assert_provisional_doctor(doctor, name)
    _assert_provisional_listing(instances, name, transaction)


def _provisional_manager_record_fixture(root: Path) -> tuple[ManagerPaths, str, Transaction]:
    case_root = root / "provisional-manager-record"
    manager_home = case_root / "manager"
    subject_home = case_root / "subject-home"
    name = "incomplete"
    target = subject_home / "Solets" / name
    paths = ManagerPaths.resolve(environ={}, home=subject_home, explicit_home=manager_home)
    transaction = _fixture_transaction(name=name, target=target)
    write_transaction(paths.transaction_path(name), transaction)
    InstanceRegistry(paths.registry_path).add(
        _provisional_instance_record(transaction, subject_home)
    )
    return paths, name, transaction


def _provisional_instance_record(
    transaction: Transaction,
    subject_home: Path,
) -> InstanceRecord:
    name = transaction.name
    return replace(
        _instance_record(name, Path(transaction.target)),
        launcher=str(Path(transaction.target) / "client" / "bin" / name),
        lifecycle_state="setup_incomplete",
        input_fingerprint=transaction.input_fingerprint,
        expected_router_name=name,
        expected_router_socket=str(subject_home / ".ananta/runtime/incomplete.router.sock"),
        expected_router_port_range="8800-8999",
    )


def _assert_provisional_status(status: CommandResult, name: str) -> None:
    _check(
        (status.status, status.error_kind, status.repair, status.data["lifecycle_state"])
        == (
            "awaiting_user",
            "instance_setup_incomplete",
            f"Resume with: solet create {name}",
            "setup_incomplete",
        ),
        "status reports the provisional manager record before runtime reconciliation",
    )


def _assert_provisional_doctor(doctor: CommandResult, name: str) -> None:
    _check(
        (doctor.status, doctor.error_kind, doctor.repair)
        == ("awaiting_user", "instance_setup_incomplete", f"Resume with: solet create {name}"),
        "doctor refuses to represent setup-incomplete materialization as verified",
    )


def _assert_provisional_listing(
    instances: object,
    name: str,
    transaction: Transaction,
) -> None:
    _check(
        isinstance(instances, list)
        and (len(instances), instances[0]["input_fingerprint"], instances[0]["expected_router_name"])
        == (1, transaction.input_fingerprint, name),
        "list retains the transaction fingerprint and expected router identity",
    )


def _human_render_matches(rendered: str) -> bool:
    return all(("Status: verified" in rendered, '"value": 1' in rendered))


def _required_help_is_present(help_text: str) -> bool:
    return all(("create" in help_text, "doctor" in help_text))


def _assert_seedless_diagnostic_matches_list_seeds(unavailable: CommandResult) -> None:
    seedless_error: SourceError | None = None
    try:
        seedless_path = cli_commands._resolve_seed_lock(
            build_parser().parse_args(["create", "sample"])
        )
        load_seed_lock(seedless_path)
    except SourceError as exc:
        seedless_error = exc
    _check(
        seedless_error is not None
        and str(seedless_error) == unavailable.message
        and seedless_error.repair == unavailable.repair
        and unavailable.repair is not None
        and "solet --seed-lock <path> create <name>" in unavailable.repair,
        "seedless create and list-seeds share one actionable missing-seed "
        f"diagnostic; list-seeds={unavailable.message!r}, "
        f"create={None if seedless_error is None else str(seedless_error)!r}, "
        f"list-repair={unavailable.repair!r}, "
        f"create-repair={None if seedless_error is None else seedless_error.repair!r}",
    )


def _assert_empty_target_conflict_is_actionable(
    *,
    paths: ManagerPaths,
    seed_lock: Path,
    config: CreateConfig,
) -> None:
    unmanaged_error: StateConflictError | None = None
    try:
        CreateManager(
            paths=paths,
            contract_directory=_CONTRACTS,
            seed_lock_path=seed_lock,
        ).preview(config)
    except StateConflictError as exc:
        unmanaged_error = exc
    _check(
        unmanaged_error is not None
        and "expected a target path that does not exist" in str(unmanaged_error)
        and "found an empty directory" in str(unmanaged_error)
        and str(config.target) in str(unmanaged_error)
        and unmanaged_error.repair is not None
        and f"rmdir -- {config.target}" in unmanaged_error.repair
        and "rerun the same create command" in unmanaged_error.repair,
        "empty unmanaged target refusal states expected, found, and executable "
        f"recovery; message={None if unmanaged_error is None else str(unmanaged_error)!r}, "
        f"repair={None if unmanaged_error is None else unmanaged_error.repair!r}",
    )


def _preview_has_no_secret_marker(preview: CommandResult) -> bool:
    return not any("SECRET" in str(value) for value in preview.to_dict().values())


def _named_seed_resolution_checks(root: Path) -> None:
    prefix = root / "prefix"
    seed_root = prefix / "share" / "solet" / "seeds"
    named_lock = seed_root / "macos-samantha" / "seed.lock.json"
    named_lock.parent.mkdir(parents=True)
    _seed_lock(
        named_lock,
        repository="https://github.com/solet-public/macos-samantha.git",
        profile="macos-samantha",
        archive_sha256=None,
    )
    with patch.object(cli_commands.sys, "prefix", str(prefix)):
        _check(
            cli_commands._resolve_named_seed_lock("macos-samantha") == named_lock,
            "named seed resolves from the installed seed set",
        )
        listing = cli_commands.run_list_seeds_command()
        _check(
            all((
                listing.status == "available",
                listing.data["seeds"] == [{"name": "macos-samantha", "profile": "macos-samantha"}],
                listing.data["inventory"] == "live tap view",
            )),
            "list-seeds reports each valid named seed as a live tap view",
        )
        malformed_lock = seed_root / "malformed-seed" / "seed.lock.json"
        malformed_lock.parent.mkdir()
        malformed_lock.write_text("{}", encoding="utf-8")
        listing = cli_commands.run_list_seeds_command()
        _check(
            listing.data["invalid_seeds"]
            == [{"name": "malformed-seed", "error": "seed lock fields differ from v1; missing=['commit', 'profile', 'release_tag', 'repository', 'schema_version', 'tree_hash'], unknown=[]"}],
            "list-seeds reports a malformed entry without crashing",
        )
        _raises(
            SourceError,
            lambda: cli_commands._resolve_named_seed_lock("malformed-seed"),
            "create --seed surfaces a malformed lock rather than falling back",
        )
        _raises(
            SourceError,
            lambda: cli_commands._resolve_named_seed_lock("missing-seed"),
            "unknown named seed fails rather than falling back to the bundled default",
        )
        mismatched_lock = seed_root / "directory-name" / "seed.lock.json"
        mismatched_lock.parent.mkdir()
        _seed_lock(
            mismatched_lock,
            repository="https://github.com/solet-public/macos-other.git",
            profile="macos-other",
        )
        _raises(
            SourceError,
            lambda: cli_commands._resolve_named_seed_lock("directory-name"),
            "named seed directory must agree with its lock profile",
        )
    absent_prefix = root / "absent-prefix"
    with patch.object(cli_commands.sys, "prefix", str(absent_prefix)):
        unavailable = cli_commands.run_list_seeds_command()
        _check(
            all((
                unavailable.status == "unavailable",
                unavailable.exit_code == ExitCode.OK,
                unavailable.message.startswith("no seed set available"),
            )),
            "list-seeds reports an absent seed set without failing",
        )
        _assert_seedless_diagnostic_matches_list_seeds(unavailable)
        _raises(
            SourceError,
            lambda: cli_commands._resolve_named_seed_lock("macos-samantha"),
            "create --seed reports an absent seed set loudly",
        )
    broken_prefix = root / "broken-prefix"
    broken_root = broken_prefix / "share" / "solet"
    broken_root.mkdir(parents=True)
    (broken_root / "seeds").symlink_to(root / "missing-tap-seeds", target_is_directory=True)
    with patch.object(cli_commands.sys, "prefix", str(broken_prefix)):
        unavailable = cli_commands.run_list_seeds_command()
        _check(
            all((
                unavailable.status == "unavailable",
                unavailable.message.startswith("tap not reachable"),
                "tap appears to have been removed" in unavailable.message,
            )),
            "list-seeds diagnoses a broken tap symlink without failing",
        )
        _raises(
            SourceError,
            lambda: cli_commands._resolve_named_seed_lock("macos-samantha"),
            "create --seed diagnoses a broken tap symlink loudly",
        )


def _configuration_and_path_checks(root: Path) -> tuple[Path, ManagerPaths]:
    home = root / "home"
    home.mkdir(mode=0o700)
    config_path = root / "solet.toml"
    config_path.write_text(
        ('schema_version = 1\nname = "from_config"\ntarget = "/tmp/from-config"\nautostart = false\n[decisions]\nexecution_topology = "solo"\ncoding_agents = ["codex"]\n'),
        encoding="utf-8",
    )
    config = load_create_config(
        config_path=config_path,
        flag_name="from_flag",
        flag_target=root / "target",
        flag_autostart=True,
        flag_decisions={"coding_agents": ["claude_code", "codex"]},
        home=home,
    )
    _check(config.name == "from_flag", "flags override TOML")
    _check(config.target == (root / "target").resolve(), "target flag override")
    _check(config.autostart is True, "autostart flag override")
    _check(
        _decision_precedence_is_correct(config),
        "repeatable flag decisions override public TOML decisions with source evidence",
    )
    _check(
        _parse_decisions(["coding_agents=codex", "coding_agents=claude_code"])
        == {"coding_agents": ["codex", "claude_code"]},
        "repeatable decision flags preserve ordered-multiple selection order",
    )
    _check(
        _parse_decisions(["session_sources="]) == {"session_sources": []},
        "an empty decision flag preserves an explicit zero-selection answer",
    )
    default = load_create_config(
        config_path=None,
        flag_name="defaulted",
        flag_target=None,
        flag_autostart=None,
        home=home,
    )
    _check(default.target == home.resolve() / "Solets" / "defaulted", "default target")
    _check(default.autostart is True, "default autostart")
    _path_collision_check(home)
    _invalid_config_checks(root, home)
    paths = ManagerPaths.resolve(environ={}, home=home, explicit_home=root / "manager")
    _check(
        paths.registry_path == root.resolve() / "manager" / "config" / "instances.json",
        "SOLET_HOME config",
    )
    _check(paths.transaction_path("x").name == "x.json", "transaction location")
    _lifecycle_reconciliation_checks(root)
    return home, paths


def _path_collision_check(home: Path) -> None:
    with patch.object(config_loading.shutil, "which", return_value="/usr/bin/python3"):
        _raises(
            ConfigError,
            lambda: config_loading.validate_name_path_collision(
                name="python3",
                target=home / "Solets" / "python3",
                is_matching_resume=False,
            ),
            "create rejects a name that would collide with a PATH command",
        )


def _resume_path_collision_checks(root: Path) -> None:
    """Pin own-launcher resume and foreign-collision behavior independently."""

    home = root / "launcher-owner"
    manager_home = root / "launcher-manager"
    name = "resumable"
    target = home / "Solets" / name
    bridge = target / ".venv" / "bin" / "solet-bridge"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = home / ".local" / "bin" / name
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(bridge)
    paths = ManagerPaths.resolve(environ={}, home=home, explicit_home=manager_home)
    config = CreateConfig(name=name, target=target, autostart=True)
    transaction = replace(
        _fixture_transaction(name=name, target=target),
        input_fingerprint=canonical_sha256(config.to_identity_dict()),
    )
    expected = CommandResult(
        kind="fixture",
        status="preview_ready",
        message="fixture",
        exit_code=ExitCode.OK,
    )
    with (
        patch.object(config_loading.Path, "home", return_value=home),
        patch.object(config_loading.shutil, "which", return_value=str(launcher)),
        patch.object(preview_engine.ContractBundle, "load", return_value=object()),
        patch.object(preview_engine, "setup_preview", return_value=expected),
    ):
        result = preview_engine._resume_preview(
            paths=paths,
            registry=InstanceRegistry(paths.registry_path),
            config=config,
            transaction=transaction,
            selections={},
            decision_source="flag",
            sources={},
        )
    _check(
        result is expected,
        "matching resumed transaction accepts its installed named PATH launcher",
    )
    with (
        patch.object(config_loading.Path, "home", return_value=home),
        patch.object(config_loading.shutil, "which", return_value=str(launcher)),
    ):
        _raises(
            ConfigError,
            lambda: config_loading.validate_name_path_collision(
                name=name,
                target=target,
                is_matching_resume=False,
            ),
            "first create rejects a PATH command even when it has launcher shape",
        )

    foreign = root / "foreign-bin" / name
    foreign.parent.mkdir()
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")
    with (
        patch.object(config_loading.Path, "home", return_value=home),
        patch.object(config_loading.shutil, "which", return_value=str(foreign)),
    ):
        _raises(
            ConfigError,
            lambda: config_loading.validate_name_path_collision(
                name=name,
                target=target,
                is_matching_resume=True,
            ),
            "resume rejects a genuinely foreign PATH command",
        )


def _invalid_config_checks(root: Path, home: Path) -> None:
    bad = root / "bad.toml"
    bad.write_text('schema_version = 1\nname = "good"\npassword = "no"\n', encoding="utf-8")
    _raises(
        ConfigError,
        lambda: load_create_config(
            config_path=bad,
            flag_name=None,
            flag_target=None,
            flag_autostart=None,
            home=home,
        ),
        "secret config field rejected",
    )
    reserved = root / "reserved.toml"
    reserved.write_text(
        'schema_version = 1\nname = "good"\n[decisions]\nsetup_profile = "free"\n',
        encoding="utf-8",
    )
    _raises(
        ConfigError,
        lambda: load_create_config(
            config_path=reserved,
            flag_name=None,
            flag_target=None,
            flag_autostart=None,
            home=home,
        ),
        "reserved setup_profile decision carrier is rejected",
    )
    _raises(
        ConfigError,
        lambda: ManagerPaths.resolve(environ={"XDG_STATE_HOME": "relative"}, home=home),
        "relative XDG path rejected",
    )


def _seed_lock_schema_checks(root: Path) -> tuple[Path, Path]:
    seed_lock = root / "seed.lock.json"
    _seed_lock(seed_lock)
    _check(
        load_seed_lock(seed_lock).profile == "macos-bizops",
        "bundled seed lock continues to parse",
    )
    additional_lock = root / "macos-samantha.seed.lock.json"
    _seed_lock(
        additional_lock,
        repository="https://github.com/solet-public/macos-samantha.git",
        profile="macos-samantha",
        archive_sha256=None,
    )
    additional_seed = load_seed_lock(additional_lock)
    _check(
        all((
            additional_seed.repository == "https://github.com/solet-public/macos-samantha.git",
            additional_seed.profile == "macos-samantha",
            additional_seed.archive_sha256 is None,
        )),
        "additional seed locks parse with their declared identity and no payload checksum",
    )
    _check(
        seed_lock_from_identity(additional_seed.identity_dict()).archive_sha256 is None,
        "an absent payload checksum remains absent through transaction identity serialization",
    )
    tagless_lock = root / "tagless.seed.lock.json"
    _tagless_seed_lock(tagless_lock, profile="macos-bizops")
    tagless_seed = load_seed_lock(tagless_lock)
    _check(
        all((
            tagless_seed.release_tag is None,
            tagless_seed.commit == "a" * 40,
            tagless_seed.tree_hash == "b" * 40,
        )),
        "schema v2 tagless seed lock parses with its commit and tree identity",
    )
    _check(
        seed_lock_from_identity(tagless_seed.identity_dict()).release_tag is None,
        "tagless identity remains tagless through transaction serialization",
    )
    mixed_lock = root / "mixed-v2.seed.lock.json"
    _tagless_seed_lock(mixed_lock)
    mixed = json.loads(mixed_lock.read_text(encoding="utf-8"))
    mixed["release_tag"] = "release-2026-08-20"
    mixed_lock.write_text(json.dumps(mixed), encoding="utf-8")
    _raises(
        SourceError,
        lambda: load_seed_lock(mixed_lock),
        "schema v2 rejects a mixed v1 release_tag field",
    )
    _named_seed_resolution_checks(root)
    return seed_lock, tagless_lock


def _tagless_preview_check(
    paths: ManagerPaths,
    tagless_lock: Path,
    preview_config: CreateConfig,
    root: Path,
) -> None:
    tagless_preview = CreateManager(
        paths=paths,
        contract_directory=_CONTRACTS,
        seed_lock_path=tagless_lock,
    ).preview(replace(preview_config, name="tagless", target=root / "tagless-target"))
    tagless_actions = tagless_preview.data["manager_actions"]
    _check(
        all((
            tagless_preview.status == "preview_ready",
            tagless_preview.data["source"]["seed_tag"] is None,
            isinstance(tagless_actions, list),
            tagless_actions[0]["title"] == f"Materialize locked seed commit:{'a' * 40}",
        )),
        "--seed-lock accepts schema v2 without a CLI flag change",
    )


def _preview_and_contract_checks(
    root: Path,
    home: Path,
    paths: ManagerPaths,
    bundle: ContractBundle,
    seed_lock: Path,
    tagless_lock: Path,
) -> tuple[Path, CommandResult]:
    target = root / "new-target"
    unresolved_config = load_create_config(
        config_path=None,
        flag_name="bizops",
        flag_target=target,
        flag_autostart=True,
        home=home,
    )
    unresolved_static = CreateManager(
        paths=paths,
        contract_directory=_CONTRACTS,
        seed_lock_path=seed_lock,
    ).preview(unresolved_config)
    _check(
        _static_prompt_blocks(unresolved_static, unresolved_static.data["decision_prompts"]),
        "ambiguous reviewed static decision renders in wizard order and blocks acquisition",
    )
    preview_config = load_create_config(
        config_path=None,
        flag_name="bizops",
        flag_target=target,
        flag_autostart=True,
        flag_decisions={
            "inference_implementation": "lm_studio",
            "execution_topology": "solo",
            "git_mutation_control": "single_session",
            "session_sources": [],
        },
        home=home,
    )
    seed = SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )
    _flow_plan_change_check(root, paths, bundle, preview_config, seed)
    connector_graph_plan_check(
        root=root,
        paths=paths,
        bundle=bundle,
        config=preview_config,
        seed=seed,
        contracts=_CONTRACTS,
    )
    _permission_remediation_plan_checks(bundle)
    preview = _preview_create(paths, seed_lock, preview_config)
    _tagless_preview_check(paths, tagless_lock, preview_config, root)
    _preview_contract_error_checks(paths, seed_lock, preview_config)
    target.mkdir()
    _assert_empty_target_conflict_is_actionable(
        paths=paths,
        seed_lock=seed_lock,
        config=preview_config,
    )
    return target, preview


def _flow_plan_change_check(
    root: Path,
    paths: ManagerPaths,
    bundle: ContractBundle,
    preview_config: CreateConfig,
    seed: SeedLock,
) -> None:
    base_plan = build_setup_plan(
        bundle=bundle,
        config=preview_config,
        seed=seed,
        journal_path=paths.transaction_path("bizops"),
    )
    changed_contracts = root / "flow-only-contracts"
    shutil.copytree(_CONTRACTS, changed_contracts)
    changed_flow_path = changed_contracts / "macos_setup_flow.json"
    changed_flow = json.loads(changed_flow_path.read_text(encoding="utf-8"))
    changed_flow["plugins"]["github_midwife_plugin"]["setup_operation_refs"].remove(
        "install_shell_integration"
    )
    changed_flow_path.write_text(json.dumps(changed_flow, indent=2), encoding="utf-8")
    changed_bundle = ContractBundle.load(source_revision="a" * 40, directory=changed_contracts)
    changed_plan = build_setup_plan(
        bundle=changed_bundle,
        config=preview_config,
        seed=seed,
        journal_path=paths.transaction_path("bizops"),
    )
    _check(
        _flow_only_change_alters_plan(base_plan, changed_plan),
        "flow-only bundled-plugin change alters the plan without manager code changes",
    )


def _permission_remediation_plan_checks(
    bundle: ContractBundle,
) -> None:
    remediation = remediation_operations(
        bundle=bundle,
        stage_id="genesis",
        operation_ids=("open_background_items_settings",),
        public_inputs={},
        decisions={"setup_profile": "macos-bizops", "autostart": "enabled"},
    )
    _check(
        all((
            len(remediation) == 1,
            remediation[0].stage_id == "genesis",
            remediation[0].operation_id == "open_background_items_settings",
            remediation[0].operation_ref == "macos::settings.background_items",
        )),
        "a failed permission probe materializes its closed settings remediation operation",
    )
    _check(
        remediation_operations(
            bundle=bundle,
            stage_id="genesis",
            operation_ids=(),
            public_inputs={},
            decisions={"setup_profile": "macos-bizops", "autostart": "enabled"},
        )
        == (),
        "a successful permission probe materializes no settings remediation operation",
    )


def _preview_create(paths: ManagerPaths, seed_lock: Path, config: CreateConfig) -> CommandResult:
    before = _tree_paths(config.target.parent)
    preview = CreateManager(
        paths=paths,
        contract_directory=_CONTRACTS,
        seed_lock_path=seed_lock,
    ).preview(config)
    after = _tree_paths(config.target.parent)
    _check(before == after, "dry-run preview writes nothing")
    _check(preview.status == "preview_ready", "outer acquisition preview ready")
    _check(preview.data["dry_run_writes"] == 0, "dry-run reports zero writes")
    _check(
        all((
            isinstance(preview.data["manager_actions"], list),
            bool(preview.data["manager_actions"]),
        )),
        "acquisition action is concrete",
    )
    _check(
        "embedding_model" in preview.data["deferred_setup_decisions"],
        "discovery decision explicitly deferred until target probes",
    )
    _check("approval_fingerprint" in preview.data, "approval fingerprint emitted")
    return preview


def _preview_contract_error_checks(
    paths: ManagerPaths,
    seed_lock: Path,
    preview_config: CreateConfig,
) -> None:
    manager = CreateManager(paths=paths, contract_directory=_CONTRACTS, seed_lock_path=seed_lock)
    _check(
        manager.preview(
            replace(
                preview_config,
                decisions={**preview_config.decisions, "coding_agents": ["codex"]},
                decision_sources={**preview_config.decision_sources, "coding_agents": "config"},
            )
        ).data["approval_fingerprint"]
        != manager.preview(
            replace(
                preview_config,
                decisions={**preview_config.decisions, "coding_agents": ["claude_code"]},
                decision_sources={**preview_config.decision_sources, "coding_agents": "config"},
            )
        ).data["approval_fingerprint"],
        "static decision drift changes the approval fingerprint",
    )
    _raises(
        ContractError,
        lambda: manager.preview(
            replace(
                preview_config,
                decisions={**preview_config.decisions, "coding_agents": "codex"},
                decision_sources={**preview_config.decision_sources, "coding_agents": "config"},
            )
        ),
        "TOML multiple decision requires an array shape",
    )
    _raises(
        ContractError,
        lambda: manager.preview(
            replace(
                preview_config,
                decisions={**preview_config.decisions, "not_declared": "value"},
                decision_sources={**preview_config.decision_sources, "not_declared": "flag"},
            )
        ),
        "unknown public decision carrier is rejected by the pinned flow",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        home, paths = _configuration_and_path_checks(root)
        _resume_path_collision_checks(root)
        _provisional_manager_record_checks(root)

        bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
        _check(bundle.flow_id == "macos.repository_setup", "flow loaded")
        _check(
            bundle.operations["request_homebrew_install"]["implementation_status"]
            == "implemented",
            "Homebrew installation status matches the implemented bootstrap route",
        )
        _check(
            all((
                len(bundle.completion_probe_ids) == 26,
                "instance_environment_dependency_closure_valid" in bundle.completion_probe_ids,
            )),
            "dependency closure probe is pinned as required completion evidence",
        )
        _check(
            _lifecycle_probe_filter_is_correct(bundle),
            "lifecycle postconditions are activation-filtered by recorded decisions",
        )
        _check(
            genesis_topology_probe_activation_is_correct(bundle),
            "Genesis completion probes follow resolved autostart and router topology",
        )
        declared_operation_probe_dispatch_check(bundle, root / "probe-dispatch-target")
        operation_owned_probe_requests_use_probe_identity()
        planned_action_drift_uses_operation_route()
        seed_lock, tagless_lock = _seed_lock_schema_checks(root)
        target, preview = _preview_and_contract_checks(
            root,
            home,
            paths,
            bundle,
            seed_lock,
            tagless_lock,
        )

        result = CommandResult(
            kind="fixture",
            status="verified",
            message="fixture",
            exit_code=ExitCode.OK,
            data={"value": 1},
        )
        rendered_json = json.loads(render_json(result))
        rendered_human = render_human(result)
        _check(rendered_json == result.to_dict(), "JSON renders typed result exactly")
        _check(_human_render_matches(rendered_human), "human uses same result")
        help_text = build_parser().format_help()
        _check(
            all((
                _required_help_is_present(help_text),
                "list-seeds" in help_text,
                "--seed-lock" in help_text,
                "place before `create`" in help_text,
                build_parser()
                .parse_args(["--seed-lock", "/fixture/seed.lock.json", "create", "sample"])
                .seed_lock
                == Path("/fixture/seed.lock.json"),
                build_parser().parse_args(["create", "sample", "--seed", "macos-samantha"]).seed
                == "macos-samantha",
            )),
            "public seed controls are discoverable and parse in their supported positions",
        )
        _check(" stop " not in f" {help_text} ", "stop deferred from help")

        request = OperationRequest(
            request_id="8f2f3ed3-03fc-4f58-915e-eb400a172a67",
            operation_id="missing",
            operation_ref="setup::missing",
            phase="probe",
            probe_purpose="preview",
            attempt=1,
            name="bizops",
            target=str(target),
            flow_id="macos.repository_setup",
            flow_source_revision="a" * 40,
            answers_fingerprint="sha256:" + "1" * 64,
            approval_fingerprint=None,
            dry_run=True,
            timeout_seconds=30,
            public_inputs={},
        )
        missing = invoke_adapter(AdapterRegistry(target=target), runner="hydration", request=request)
        _check(missing.checkpoint_status is CheckpointStatus.BLOCKED, "missing adapter blocks")
        _check(missing.error_kind == "adapter_missing", "missing adapter stable kind")

        private = root / "private"
        private.mkdir(mode=0o700)
        _check(stat.S_IMODE(private.stat().st_mode) == 0o700, "fixture private directory")
        _check(_preview_has_no_secret_marker(preview), "preview has no secret marker")
        _check(os.path.commonpath([target, root]) == str(root), "fixture target contained")

    print(f"manager_foundation_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
