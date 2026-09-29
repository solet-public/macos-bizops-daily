"""iss_9cd4359a (r61): a fresh solet's own create edits no longer warn in ``solet doctor``; any other tracked edit still does.

``solet create`` edits five tracked files on purpose: Genesis writes the hydration block into ``AGENTS.md`` and
``CLAUDE.md`` and the name into ``root_manifest.yaml``, and the coding-agent stage pins both coordination-hook
manifests to the target venv.  Every healthy created solet then reported ``seed_tree_verification``
``tracked_tree_deviation`` and ``doctor::release_identity_v1`` ``working_tree_dirty`` for those writes (r60 Leg N).

The fixture is a real Git checkout carrying the repository's own two ``hooks.json`` manifests.  Two approved
apply operations from the real setup contract, ``run_genesis`` and ``install_claude_plugin``, run through the
real ``operation_executor._apply_operation``; only the adapter is faked, and it performs the create's writes
(the hook pin through the installer's own ``pin_hook_interpreter``).  The doctor is the real
``InstallationDoctor.run`` (``_doctor_seams``) with the real release-identity census over a fixture manifest.

- **fresh create**: the five edits are recorded at apply time; the seed tree verifies with them listed as
  ``accepted_manager_edits``, and the release-identity seed checkout is not dirty.
- **user edit to a recorded file**: appending to ``CLAUDE.md`` changes its digest; it warns again, by name.
- **mode change to a recorded file**: ``chmod +x CLAUDE.md`` after the create, or ``AGENTS.md`` before the
  apply, keeps the recorded bytes but not ``HEAD``'s mode; it warns by name in both checks.
- **staged user edit behind the Manager's bytes**: the worktree matches the record but the index does not;
  the index deviation still warns in both checks.
- **re-create under the same name**: the new create's first recorded edit drops the old create's rows.
- **user edit to another tracked file**: ``README.md`` was never recorded; it warns.
- **edit made before an apply**: a ``README.md`` edit made between stages is not attributed to the next
  operation, which never changed it.
- **failed apply**: an adapter that writes and then reports ``failed`` records nothing; its write warns.
- **another create transaction**: a ledger bound to a different create operation accepts nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(Path(__file__).resolve().parent), str(_ROOT)]

from _doctor_seams import doctor_seams  # noqa: E402
from _release_identity_fixture import build_package, manifest, seed_lock_v3, write_json, write_receipt  # noqa: E402
from operation_probe_adapter_support import _FixtureRegistry, _operation, _result  # noqa: E402
from solet_manager import operation_executor  # noqa: E402
from solet_manager.adapters import OperationRequest, OperationResult  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.doctor import InstallationDoctor  # noqa: E402
from solet_manager.doctor_release_identity_census import collect_release_identity_advisories  # noqa: E402
from solet_manager.models import CheckpointStatus, InstanceRecord, JsonValue  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, load_transaction  # noqa: E402
from solet_setup_contracts.hook_interpreter_pin import HOOK_MANIFEST_PATHS, instance_interpreter, pin_hook_interpreter  # noqa: E402

_NAME = "fixture"
_CONTRACTS = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_HYDRATION = "\n<!-- solet hydration -->\nAsk the solet first.\n"
_GENESIS_EDITS = ("AGENTS.md", "CLAUDE.md", "root_manifest.yaml")
_CREATE_EDITS = tuple(sorted((*_GENESIS_EDITS, *HOOK_MANIFEST_PATHS)))
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/"),
}
_checks = 0

type Write = Callable[[Path], None]


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


# --- fixture ------------------------------------------------------------------------------------------------


class Case:
    """One created checkout, its Manager state, the real contract bundle and a release manifest naming its seed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.target = root / "target"
        self.paths = ManagerPaths(root / "manager" / "config", root / "manager" / "state", root / "manager" / "cache")
        for directory in (self.paths.config_dir, self.paths.state_dir, self.paths.cache_dir):
            directory.mkdir(parents=True, mode=0o700)
        self.bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
        commit, tree = _seed_checkout(self.target)
        self.transaction = _create_transaction(self.bundle, self.target, commit, tree)
        self.release = _release_files(root / "release", commit, tree)


def _run(target: Path, *arguments: str) -> str:
    completed = subprocess.run(("git", "-C", str(target), *arguments), check=True, capture_output=True, text=True, env=_GIT_ENV)
    return completed.stdout.strip()


def _seed_checkout(target: Path) -> tuple[str, str]:
    """The seed's own bytes for the five files the create edits, plus two it never touches."""
    target.mkdir(parents=True)
    _run(target, "init", "-q", "-b", "main")
    for relative in HOOK_MANIFEST_PATHS:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((_ROOT / relative).read_bytes())
    (target / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (target / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    (target / "root_manifest.yaml").write_text("solet_name: solet\n", encoding="utf-8")
    (target / "README.md").write_text("fixture seed\n", encoding="utf-8")
    _run(target, "add", "-A")
    _run(target, "commit", "-q", "-m", "seed")
    return _run(target, "rev-parse", "HEAD"), _run(target, "rev-parse", "HEAD^{tree}")


def _create_transaction(bundle: ContractBundle, target: Path, commit: str, tree: str) -> Transaction:
    operations = dict.fromkeys(("run_genesis", "install_claude_plugin"), "system_dependencies")
    created = Transaction.create(
        name=_NAME,
        target=target,
        input_fingerprint="sha256:" + "d" * 64,
        answers={"decisions": {"autostart": "disabled", "coding_agents": ["claude"]}},
        seed=SeedLock("https://example.invalid/seed.git", "fixture", commit, tree, "c" * 64, "fixture"),
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=("system_dependencies",),
        completion_probe_ids=(),
    )
    return created.bind_operations(operations).approve("sha256:" + "f" * 64)


def _release_files(root: Path, commit: str, tree: str) -> dict[str, Path]:
    seed = {"commit": commit, "tree_hash": tree}
    package = root / "package"
    digests = build_package(package)
    return {
        "manifest_path": write_json(root / "release_manifest.json", manifest(seed=seed, file_digests=digests, components=False)),
        "install_source_path": write_receipt(root / "install_source.json"),
        "manager_seed_lock_path": write_json(root / "seed.lock.json", seed_lock_v3(seed)),
        "package_root": package,
    }


# --- the create's writes, applied through the real executor ------------------------------------------------


def _genesis_writes(target: Path) -> None:
    for relative in ("AGENTS.md", "CLAUDE.md"):
        path = target / relative
        path.write_text(path.read_text(encoding="utf-8") + _HYDRATION, encoding="utf-8")
    (target / "root_manifest.yaml").write_text(f"solet_name: {_NAME}\n", encoding="utf-8")


def _hook_pin_writes(target: Path) -> None:
    for relative in HOOK_MANIFEST_PATHS:
        path = target / relative
        pinned = pin_hook_interpreter(path.read_bytes(), instance_interpreter(target))
        if pinned is None:
            raise AssertionError(f"the installer's transform leaves {relative} unchanged; the fixture no longer pins")
        path.write_bytes(pinned)


def _apply(case: Case, operation_id: str, write: Write, *, status: CheckpointStatus = CheckpointStatus.APPLIED) -> None:
    """One approved apply through ``_apply_operation``: the adapter performs ``write`` and answers ``status``."""

    def adapter(_registry: object, *, runner: str, request: OperationRequest) -> OperationResult:
        del runner
        if request.phase == "apply":
            write(case.target)
            return _result(request, status, error_kind=None if status is CheckpointStatus.APPLIED else "genesis_failed")
        return _result(request, CheckpointStatus.VERIFIED)

    current = load_transaction(case.paths.transaction_path(_NAME)) or case.transaction
    with patch.object(operation_executor, "invoke_adapter", adapter):
        operation_executor._apply_operation(  # pyright: ignore[reportPrivateUsage]
            bundle=case.bundle,
            operation=_operation(case.bundle, operation_id),
            transaction=current,
            registry=cast(Any, _FixtureRegistry()),
            paths=case.paths,
            attempt=1,
        )


def _create(case: Case) -> None:
    _apply(case, "run_genesis", _genesis_writes)
    _apply(case, "install_claude_plugin", _hook_pin_writes)
    _enroll(case)


def _enroll(case: Case) -> None:
    """Exactly the v1 row a verified create records (``operation_executor.ensure_registry``)."""
    transaction = load_transaction(case.paths.transaction_path(_NAME))
    if transaction is None:
        raise AssertionError("the executor did not persist the create transaction")
    seed = transaction.seed
    InstanceRegistry(case.paths.registry_path).add(
        InstanceRecord(
            _NAME, str(case.target), str(case.target / "client" / "bin" / _NAME), seed.repository, seed.release_tag, seed.commit,
            seed.tree_hash, seed.profile, transaction.flow_id, transaction.flow_source_revision, transaction.flow_contract_digest,
            transaction.created_at, transaction.created_at,
        )
    )


# --- the doctor ---------------------------------------------------------------------------------------------


def _doctor(case: Case) -> tuple[dict[str, Any], dict[str, Any]]:
    """The real doctor's seed-tree verification and the real release-identity census's seed checkout."""

    def release_identity(record: InstanceRecord, transaction: Transaction, **kwargs: Any) -> list[JsonValue]:
        return collect_release_identity_advisories(record, transaction, **case.release, **kwargs)

    with doctor_seams([], release_identity):
        result = InstallationDoctor(paths=case.paths, contract_directory=None).run(_NAME)
    advisories = cast(list[dict[str, Any]], result.data["advisories"])
    identity = next(item for item in advisories if item["check_id"] == "doctor::release_identity_v1")
    return cast(dict[str, Any], result.data["seed_tree_verification"]), cast(dict[str, Any], identity["observed"]["seed_checkout"])


def _deviating(seed_tree: dict[str, Any]) -> list[str]:
    return sorted(path for item in seed_tree["deviations"] for path in item["paths"])


def _accepted(seed_tree: dict[str, Any]) -> list[str]:
    return sorted(path for item in seed_tree.get("accepted_manager_edits", []) for path in item["paths"])


def _append(target: Path, relative: str, text: str) -> None:
    path = target / relative
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


# --- legs ---------------------------------------------------------------------------------------------------


def _leg_fresh_create(root: Path) -> None:
    case = Case(root / "fresh")
    _create(case)
    _check(sorted(_run(case.target, "diff", "--name-only").splitlines()) == list(_CREATE_EDITS), "control: the create left exactly its five tracked edits")
    seed_tree, checkout = _doctor(case)
    _check(seed_tree["status"] == "verified" and seed_tree["reason"] == "seed_tree_matches", f"a fresh create's own edits do not warn ({seed_tree['reason']}: {_deviating(seed_tree)})")
    _check(_accepted(seed_tree) == list(_CREATE_EDITS), f"the five edits are named as accepted Manager edits ({_accepted(seed_tree)})")
    _check(checkout["dirty_paths"] == [] and checkout["reason"] != "working_tree_dirty", f"release identity: the checkout is not dirty ({checkout['reason']}: {checkout['dirty_paths']})")
    _check(sorted(checkout.get("accepted_manager_edits", [])) == list(_CREATE_EDITS), "release identity names the accepted edits")


def _leg_user_edits_recorded_file(root: Path) -> None:
    case = Case(root / "recorded")
    _create(case)
    _append(case.target, "CLAUDE.md", "My own note.\n")
    seed_tree, checkout = _doctor(case)
    _check(seed_tree["reason"] == "tracked_tree_deviation" and _deviating(seed_tree) == ["CLAUDE.md"], f"a user edit to a recorded file warns by name ({seed_tree['reason']}: {_deviating(seed_tree)})")
    _check(_accepted(seed_tree) == [path for path in _CREATE_EDITS if path != "CLAUDE.md"], "the other four stay accepted")
    _check(checkout["reason"] == "working_tree_dirty" and checkout["dirty_paths"] == ["CLAUDE.md"], f"release identity: dirty with CLAUDE.md ({checkout['reason']}: {checkout['dirty_paths']})")


def _leg_mode_change_after_create(root: Path) -> None:
    case = Case(root / "chmod-after")
    _create(case)
    os.chmod(case.target / "CLAUDE.md", 0o755)
    seed_tree, checkout = _doctor(case)
    _check(seed_tree["reason"] == "tracked_tree_deviation" and _deviating(seed_tree) == ["CLAUDE.md"], f"chmod +x on a recorded file warns by name ({seed_tree['reason']}: {_deviating(seed_tree)})")
    _check("CLAUDE.md" not in _accepted(seed_tree), "a mode-changed recorded file is not accepted")
    _check(checkout["reason"] == "working_tree_dirty" and checkout["dirty_paths"] == ["CLAUDE.md"], f"release identity: dirty with CLAUDE.md ({checkout['reason']}: {checkout['dirty_paths']})")


def _leg_mode_change_before_apply(root: Path) -> None:
    case = Case(root / "chmod-before")
    os.chmod(case.target / "AGENTS.md", 0o755)
    _create(case)
    seed_tree, checkout = _doctor(case)
    _check(_deviating(seed_tree) == ["AGENTS.md"] and "AGENTS.md" not in _accepted(seed_tree), f"a chmod made before the apply is not absorbed by it ({_deviating(seed_tree)})")
    _check(checkout["dirty_paths"] == ["AGENTS.md"], f"release identity: dirty with AGENTS.md ({checkout['dirty_paths']})")


def _leg_staged_edit_behind_manager_bytes(root: Path) -> None:
    case = Case(root / "staged")
    _create(case)
    path = case.target / "AGENTS.md"
    manager_bytes = path.read_bytes()
    path.write_bytes(manager_bytes + b"Staged by the user.\n")
    _run(case.target, "add", "AGENTS.md")
    path.write_bytes(manager_bytes)
    seed_tree, checkout = _doctor(case)
    staged = [item["paths"] for item in seed_tree["deviations"] if item["location"] == "index"]
    _check(seed_tree["reason"] == "tracked_tree_deviation" and staged == [["AGENTS.md"]], f"a staged edit behind the Manager's bytes warns ({seed_tree['deviations']})")
    _check("AGENTS.md" in checkout["dirty_paths"], f"release identity: the staged path is dirty ({checkout['dirty_paths']})")


def _leg_recreate_drops_old_rows(root: Path) -> None:
    from solet_manager.create_applied_edits import applied_edits_path, load_applied_edits, record_applied_edits  # noqa: PLC0415 -- the other legs run red against a Manager without it

    case = Case(root / "recreate")
    _create(case)
    ledger_path = applied_edits_path(case.paths, _NAME)
    before = load_applied_edits(ledger_path)
    _check(before is not None and [item.path for item in before.edits] == list(_CREATE_EDITS), "control: the first create recorded its five edits")
    digest = "sha256:" + "0" * 64
    record_applied_edits(case.paths, name=_NAME, target=str(case.target), create_operation_id="op_new_create", operation_id="run_genesis", before={}, after={"README.md": digest})
    after = load_applied_edits(ledger_path)
    _check(after is not None and after.create_operation_id == "op_new_create" and [(item.path, item.sha256) for item in after.edits] == [("README.md", digest)], "a re-create under the same name keeps none of the old create's rows")


def _leg_user_edits_other_file(root: Path) -> None:
    case = Case(root / "other")
    _create(case)
    _append(case.target, "README.md", "Edited by hand.\n")
    seed_tree, checkout = _doctor(case)
    _check(seed_tree["reason"] == "tracked_tree_deviation" and _deviating(seed_tree) == ["README.md"], f"an edit no apply made warns ({seed_tree['reason']}: {_deviating(seed_tree)})")
    _check(checkout["reason"] == "working_tree_dirty" and checkout["dirty_paths"] == ["README.md"], "release identity: dirty with README.md")


def _leg_edit_before_apply(root: Path) -> None:
    case = Case(root / "before")
    _append(case.target, "README.md", "Edited between stages.\n")
    _create(case)
    seed_tree, _checkout = _doctor(case)
    _check(_deviating(seed_tree) == ["README.md"] and _accepted(seed_tree) == list(_CREATE_EDITS), f"an edit made before an apply is not attributed to it ({_deviating(seed_tree)})")


def _leg_failed_apply(root: Path) -> None:
    case = Case(root / "failed")
    _apply(case, "run_genesis", _genesis_writes, status=CheckpointStatus.FAILED)
    _enroll(case)
    seed_tree, _checkout = _doctor(case)
    _check(_deviating(seed_tree) == sorted(_GENESIS_EDITS) and _accepted(seed_tree) == [], f"a failed apply's writes are not recorded ({_deviating(seed_tree)})")


def _leg_other_create_transaction(root: Path) -> None:
    from solet_manager.create_applied_edits import accepted_edit_paths  # noqa: PLC0415 -- the other legs run red against a Manager without it

    case = Case(root / "rebound")
    _create(case)
    transaction = load_transaction(case.paths.transaction_path(_NAME))
    if transaction is None:
        raise AssertionError("the create transaction vanished")
    accepted = accepted_edit_paths(case.paths, name=_NAME, target=case.target, create_operation_id=transaction.operation_id)
    _check(sorted(accepted) == list(_CREATE_EDITS), "control: the ledger accepts the five edits for its own create")
    other = accepted_edit_paths(case.paths, name=_NAME, target=case.target, create_operation_id="op_another_create")
    _check(other == frozenset(), "a ledger bound to another create transaction accepts nothing")


def main() -> int:
    failures: list[str] = []
    legs: tuple[tuple[str, Callable[[Path], None]], ...] = (
        ("fresh create: the create's own five edits do not warn in either check", _leg_fresh_create),
        ("user edit to a recorded file: CLAUDE.md warns again", _leg_user_edits_recorded_file),
        ("mode change after create: chmod +x CLAUDE.md warns", _leg_mode_change_after_create),
        ("mode change before an apply: not absorbed by the apply", _leg_mode_change_before_apply),
        ("staged user edit behind the Manager's bytes: still warns", _leg_staged_edit_behind_manager_bytes),
        ("re-create under the same name: the old create's rows are dropped", _leg_recreate_drops_old_rows),
        ("user edit to another tracked file: README.md warns", _leg_user_edits_other_file),
        ("edit before an apply: not attributed to the apply", _leg_edit_before_apply),
        ("failed apply: nothing recorded, its writes warn", _leg_failed_apply),
        ("another create transaction: the ledger accepts nothing", _leg_other_create_transaction),
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for label, leg in legs:
            try:
                leg(root)
                print(f"  PASS  {label}")
            except Exception as exc:  # noqa: BLE001 -- every leg's verdict is reported, not only the first
                failures.append(f"{label}: {type(exc).__name__}: {str(exc)[:400]}")
                print(f"  FAIL  {failures[-1]}")
    if failures:
        print(f"create_applied_edits_doctor_smoke FAILED: {len(failures)} leg(s)")
        return 1
    print(f"create_applied_edits_doctor_smoke OK: {_checks} checks, {len(legs)} legs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
