"""Manifest-gated recovery-safe reconciliation of one persisted contract bundle."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from .answer_validation import validate_normalized_answers
from .contract_reconciliation_chain import resolve_reconciliation_chain
from .contract_reconciliation_receipts import (
    per_probe_migration_data,
    validate_per_probe_migrations,
)
from .contracts import (
    ContractBundle,
    ContractReconciliation,
    contract_filenames,
    load_contract_reconciliations,
    load_reconciliation_destination_bundle,
    target_contract_directory,
)
from .errors import ContractError, ManagerError, ProbeDriftError, StateConflictError, StateError
from .models import CommandResult, ExitCode, InstanceRecord, JsonValue
from .paths import ManagerPaths
from .reconciliation_ceremony import run_reconciliation_ceremony
from .registry import InstanceRegistry
from .stage_activation import reconcile_contract_stage_probe_state
from .state_io import (
    atomic_replace_bytes,
    atomic_write_json,
    ensure_private_directory,
    load_json_object,
)
from .transaction import (
    Transaction,
    load_transaction,
    target_install_state_projection,
)

_RECEIPT_V1_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "target",
        "migration_id",
        "preview_fingerprint",
        "state",
        "files",
        "attempt_identity_mappings",
    }
)
_RECEIPT_V2_KEYS = _RECEIPT_V1_KEYS | {"per_probe_migrations"}
_RECEIPT_FILE_KEYS = frozenset(
    {"path", "mode", "before_sha256", "before_base64", "after_sha256", "after_base64"}
)


@dataclass(frozen=True)
class _PreparedReconciliation:
    record: InstanceRecord
    original_transaction: Transaction
    transaction: Transaction
    reconciliation: ContractReconciliation
    destination_bundle: ContractBundle
    files: tuple[tuple[Path, int, bytes, bytes], ...]
    fingerprint: str


class ContractReconciliationManager:
    """Reconcile only release-declared incompatible persisted contract pins."""

    def __init__(
        self,
        *,
        paths: ManagerPaths,
        contract_directory: Path | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self._paths = paths
        self._contract_directory = contract_directory
        self._manifest_path = manifest_path
        self._registry = InstanceRegistry(paths.registry_path)

    def run(
        self,
        name: str,
        *,
        dry_run: bool,
        approved_fingerprint: str | None,
    ) -> CommandResult:
        return run_reconciliation_ceremony(
            paths=self._paths,
            name=name,
            dry_run=dry_run,
            approved_fingerprint=approved_fingerprint,
            recover=lambda: recover_contract_reconciliation(self._paths, name),
            prepare=lambda: self._prepare(name),
            preview_result=lambda prepared, recovered: _preview_result(
                prepared, recovered=recovered
            ),
            approval_required=_approval_required,
            drift_result=_drift_result,
            apply=self._apply,
            fingerprint=lambda prepared: prepared.fingerprint,
        )

    def _prepare(self, name: str) -> _PreparedReconciliation:
        record = self._registry.require(name)
        if record.lifecycle_state != "setup_incomplete":
            raise StateConflictError("contract reconciliation requires a setup-incomplete instance")
        transaction = load_transaction(self._paths.transaction_path(name))
        _require(
            transaction is not None,
            StateConflictError(f"managed instance {name!r} lacks its transaction journal"),
        )
        assert transaction is not None
        target = Path(record.target)
        _validate_pinned_identity(record, transaction, target)
        _validate_projection(transaction, target)
        source_bundle = ContractBundle.load(
            source_revision=transaction.flow_source_revision,
            directory=target_contract_directory(target),
            expected_digest=transaction.flow_contract_digest,
            resume_compatibility=True,
        )
        destination_bundle = load_reconciliation_destination_bundle(
            source_revision=transaction.flow_source_revision,
            development_directory=self._contract_directory,
        )
        reconciliation = _select_reconciliation(
            transaction,
            source_bundle,
            destination_bundle,
            self._manifest_path,
        )
        transaction = _migrate_answer_values(transaction, reconciliation)
        _validate_destination_answers(transaction, destination_bundle)
        reconciled_transaction = reconcile_contract_stage_probe_state(
            destination_bundle,
            transaction,
            reconciliation,
        )
        updated_record = self._registry.reconciled_contract_record(
            record,
            flow_contract_digest=reconciled_transaction.flow_contract_digest,
            updated_at=reconciled_transaction.updated_at,
        )
        files = _replacement_files(
            paths=self._paths,
            record=record,
            transaction=transaction,
            updated_record=updated_record,
            reconciled_transaction=reconciled_transaction,
            destination_bundle=destination_bundle,
        )
        fingerprint = _canonical_sha256(
            {
                "name": name,
                "target": str(target),
                "migration_id": reconciliation.migration_id,
                "source": _identity_data(transaction),
                "destination": _identity_data(reconciled_transaction),
                "destination_contract_resolution": destination_bundle.resolution.to_identity_dict(),
                "files": [
                    {
                        "path": str(path),
                        "before_sha256": _sha256(before),
                        "after_sha256": _sha256(after),
                    }
                    for path, _, before, after in files
                ],
                "attempt_identity_mappings": _attempt_mapping_data(
                    transaction, reconciled_transaction
                ),
            }
        )
        return _PreparedReconciliation(
            record=record,
            original_transaction=transaction,
            transaction=reconciled_transaction,
            reconciliation=reconciliation,
            destination_bundle=destination_bundle,
            files=files,
            fingerprint=fingerprint,
        )

    def _apply(self, prepared: _PreparedReconciliation) -> CommandResult:
        # Recompute every action-driving input while the same lock is held.
        fresh = self._prepare(prepared.record.name)
        if fresh.fingerprint != prepared.fingerprint:
            raise ProbeDriftError(
                "reconciliation state changed after preview approval; no mutation was performed",
                repair="Rerun reconcile-contract --dry-run and review the new fingerprint.",
            )
        _stage_destination_bundle(
            fresh.destination_bundle,
            self._paths.cache_dir / "contract-reconciliation-staging",
        )
        receipt_path = _receipt_path(self._paths, fresh.record.name)
        receipt = _receipt(fresh)
        atomic_write_json(receipt_path, receipt)
        for path, mode, before, after in fresh.files:
            if _sha256(path.read_bytes()) != _sha256(before):
                raise StateConflictError(f"reconciliation target changed before promotion: {path}")
            atomic_replace_bytes(path, after, mode=mode)
        _verify_after_files(fresh.files)
        applied = dict(receipt)
        applied["state"] = "applied"
        atomic_write_json(receipt_path, applied)
        return CommandResult(
            kind="contract_reconciliation",
            status="reconciled",
            message=(
                f"Reconciled persisted setup contract for {fresh.record.name!r}; run a fresh `solet create NAME --dry-run` before resuming."
            ),
            exit_code=ExitCode.OK,
            data={
                "name": fresh.record.name,
                "migration_id": fresh.reconciliation.migration_id,
                "flow_source_revision": fresh.transaction.flow_source_revision,
                "flow_contract_digest": fresh.transaction.flow_contract_digest,
                "approval_fingerprint_cleared": True,
                "next_action": f"solet create {fresh.record.name} --dry-run",
            },
        )


def recover_contract_reconciliation(paths: ManagerPaths, name: str) -> bool:
    """Recover a nonterminal receipt to authenticated before-images or finish it."""

    receipt_path = _receipt_path(paths, name)
    raw = load_json_object(receipt_path, missing_ok=True)
    if raw is None:
        return False
    files, state = _parse_receipt(raw, name)
    if state in {"applied", "recovered"}:
        return False
    current = tuple(_sha256(path.read_bytes()) for path, _, _, _ in files)
    before = tuple(_sha256(value) for _, _, value, _ in files)
    after = tuple(_sha256(value) for _, _, _, value in files)
    if current == after:
        terminal = dict(raw)
        terminal["state"] = "applied"
        atomic_write_json(receipt_path, terminal)
        return True
    _require(
        all(
            value in {expected_before, expected_after}
            for value, expected_before, expected_after in zip(current, before, after, strict=True)
        ),
        StateConflictError(
            "reconciliation recovery found bytes outside its authenticated before/after set"
        ),
    )
    for path, mode, before_bytes, _ in files:
        atomic_replace_bytes(path, before_bytes, mode=mode)
    terminal = dict(raw)
    terminal["state"] = "recovered"
    atomic_write_json(receipt_path, terminal)
    return True


def reconciliation_receipt_before_flow_source_revision(
    paths: ManagerPaths, name: str
) -> str | None:
    """Read the pre-reconciliation journal revision from one terminal receipt."""

    raw = load_json_object(_receipt_path(paths, name), missing_ok=True)
    if raw is None:
        return None
    _validate_terminal_receipt_identity(raw, name)
    transaction_before = _receipt_transaction_before_image(raw, paths.transaction_path(name))
    return _flow_source_revision(transaction_before)


def _validate_terminal_receipt_identity(raw: dict[str, JsonValue], name: str) -> None:
    version = raw.get("schema_version")
    expected_keys = _RECEIPT_V1_KEYS if version == 1 else _RECEIPT_V2_KEYS
    _require(
        version in {1, 2} and frozenset(raw) == expected_keys,
        StateError("contract reconciliation receipt does not match its closed schema"),
    )
    _require(
        raw.get("name") == name and isinstance(raw.get("target"), str),
        StateError("contract reconciliation receipt identity is invalid"),
    )
    _require(
        raw.get("state") in {"applied", "recovered"},
        StateConflictError("contract reconciliation receipt is not terminal"),
    )


def _receipt_transaction_before_image(raw: dict[str, JsonValue], transaction_path: Path) -> bytes:
    entries = raw.get("files")
    _require(
        isinstance(entries, list) and bool(entries),
        StateError("contract reconciliation receipt files are invalid"),
    )
    assert isinstance(entries, list)
    files = tuple(_parse_receipt_file(entry) for entry in entries)
    matches = [before for path, _, before, _ in files if path == transaction_path]
    _require(
        len(matches) == 1,
        StateConflictError("contract reconciliation receipt lacks its transaction before-image"),
    )
    return matches[0]


def _flow_source_revision(transaction_before: bytes) -> str:
    try:
        before = json.loads(transaction_before)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError("contract reconciliation transaction before-image is invalid") from exc
    _require(
        isinstance(before, dict) and isinstance(before.get("flow_source_revision"), str),
        StateError("contract reconciliation transaction before-image lacks flow_source_revision"),
    )
    assert isinstance(before, dict)
    return before["flow_source_revision"]


def reconciliation_recovery_pending(paths: ManagerPaths, name: str) -> bool:
    """Return whether this instance has a receipt that requires recovery."""

    raw = load_json_object(_receipt_path(paths, name), missing_ok=True)
    if raw is None:
        return False
    _, state = _parse_receipt(raw, name)
    return state == "prepared"


def _select_reconciliation(
    transaction: Transaction,
    source_bundle: ContractBundle,
    destination_bundle: ContractBundle,
    manifest_path: Path | None,
) -> ContractReconciliation:
    if source_bundle.flow_id != transaction.flow_id:
        raise StateConflictError("target contract flow_id differs from transaction")
    selected = resolve_reconciliation_chain(
        transaction,
        destination_bundle.contract_digest,
        load_contract_reconciliations(manifest_path=manifest_path),
    )
    _validate_selected_flow_ids(selected, transaction, source_bundle, destination_bundle)
    return selected


def _validate_selected_flow_ids(
    selected: ContractReconciliation,
    transaction: Transaction,
    source_bundle: ContractBundle,
    destination_bundle: ContractBundle,
) -> None:
    _require(
        selected.flow_id == transaction.flow_id,
        StateConflictError("reconciliation flow_id differs from transaction"),
    )
    _require(
        selected.flow_id == source_bundle.flow_id,
        StateConflictError("reconciliation source flow_id differs from target contract"),
    )
    _require(
        selected.flow_id == destination_bundle.flow_id,
        StateConflictError("reconciliation destination flow_id differs from destination contract"),
    )


def _validate_pinned_identity(
    record: InstanceRecord, transaction: Transaction, target: Path
) -> None:
    _require(
        target.is_dir() and not target.is_symlink(),
        StateConflictError(f"managed target is not a real directory: {target}"),
    )
    _require(
        _identities_match(record, transaction),
        StateConflictError("registry and transaction pinned identities differ"),
    )


def _identities_match(record: InstanceRecord, transaction: Transaction) -> bool:
    return (
        transaction.name == record.name
        and transaction.target == record.target
        and transaction.flow_id == record.flow_id
        and transaction.flow_source_revision == record.flow_source_revision
        and transaction.flow_contract_digest == record.flow_contract_digest
    )


def _validate_projection(transaction: Transaction, target: Path) -> None:
    projection_path = target / ".solet" / "install-state.json"
    projection = load_json_object(projection_path)
    _require(
        projection == target_install_state_projection(transaction),
        StateConflictError("target install-state projection differs from the pinned transaction"),
    )


def _validate_destination_answers(
    transaction: Transaction,
    destination_bundle: ContractBundle,
) -> None:
    """Refuse an answer/contract mismatch before its reconciliation is approved."""

    try:
        _validate_destination_answer_schema_requirements(destination_bundle, transaction.answers)
        validate_normalized_answers(destination_bundle, transaction.answers)
    except (ContractError, StateError) as exc:
        raise StateConflictError(
            f"persisted normalized answers are incompatible with the destination contract: journal contract digest {transaction.flow_contract_digest}, destination contract digest {destination_bundle.contract_digest}; the destination contract moved while the journal stayed pinned: {exc}",
            repair=(
                "Declare an answer migration or a new decision for the destination contract; do not treat the journal as corrupt."
            ),
        ) from exc


def _migrate_answer_values(
    transaction: Transaction,
    reconciliation: ContractReconciliation,
) -> Transaction:
    """Apply only the answer rewrites declared by the selected bridge."""

    if not reconciliation.answer_value_migrations:
        return transaction
    answers = deepcopy(transaction.answers)
    decisions = answers.get("decisions")
    if not isinstance(decisions, dict):
        return transaction
    changed = False
    for migration in reconciliation.answer_value_migrations:
        selected = decisions.get(migration.decision_id)
        if isinstance(selected, str):
            if selected == migration.from_value:
                decisions[migration.decision_id] = migration.to_value
                changed = True
            continue
        if not isinstance(selected, list):
            continue
        migrated = [
            migration.to_value if item == migration.from_value else item
            for item in selected
        ]
        if migrated != selected:
            decisions[migration.decision_id] = migrated
            changed = True
    if not changed:
        return transaction
    migrated = transaction.with_answers(answers)
    # The approval fingerprint includes replacement bytes; as with the other
    # reconciliation transitions, a lock-time re-preview must be stable.
    return replace(migrated, updated_at=transaction.updated_at)


def _validate_destination_answer_schema_requirements(
    destination_bundle: ContractBundle,
    answers: dict[str, JsonValue],
) -> None:
    """Enforce the destination schema's closed top-level required-property contract."""

    required = destination_bundle.answers_schema.get("required")
    _require(
        isinstance(required, list) and all(isinstance(item, str) for item in required),
        StateError("destination setup answers schema required list is invalid"),
    )
    assert isinstance(required, list)
    required_names = {item for item in required if isinstance(item, str)}
    missing = sorted(required_names - set(answers))
    if missing:
        raise StateError(f"destination setup answers missing required properties: {missing}")


def _replacement_files(
    *,
    paths: ManagerPaths,
    record: InstanceRecord,
    transaction: Transaction,
    updated_record: InstanceRecord,
    reconciled_transaction: Transaction,
    destination_bundle: ContractBundle,
) -> tuple[tuple[Path, int, bytes, bytes], ...]:
    target_contracts = target_contract_directory(Path(record.target))
    destination_contracts = destination_bundle.directory
    files: list[tuple[Path, int, bytes, bytes]] = []
    for filename in contract_filenames():
        target_path = target_contracts / filename
        files.append(_replacement_file(target_path, destination_contracts / filename))
    transaction_path = paths.transaction_path(record.name)
    files.append(_replacement_json_file(transaction_path, reconciled_transaction.to_dict()))
    registry_raw = load_json_object(paths.registry_path)
    _require(
        registry_raw is not None, StateConflictError("registry disappeared during reconciliation")
    )
    assert registry_raw is not None
    registry_payload = _registry_payload(registry_raw, record, updated_record)
    files.append(_replacement_json_file(paths.registry_path, registry_payload))
    projection = Path(record.target) / ".solet" / "install-state.json"
    files.append(
        _replacement_json_file(projection, target_install_state_projection(reconciled_transaction))
    )
    return tuple(files)


def _replacement_file(target: Path, source: Path) -> tuple[Path, int, bytes, bytes]:
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
        before = target.read_bytes()
        after = source.read_bytes()
    except OSError as exc:
        raise StateConflictError(
            f"required reconciliation file is unavailable: {target}: {exc}"
        ) from exc
    return target, mode, before, after


def _replacement_json_file(
    path: Path, value: dict[str, JsonValue]
) -> tuple[Path, int, bytes, bytes]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        before = path.read_bytes()
    except OSError as exc:
        raise StateConflictError(
            f"required reconciliation state file is unavailable: {path}: {exc}"
        ) from exc
    return path, mode, before, _json_bytes(value)


def _registry_payload(
    raw: dict[str, JsonValue],
    old: InstanceRecord,
    new: InstanceRecord,
) -> dict[str, JsonValue]:
    instances = raw.get("instances")
    _require(
        isinstance(instances, dict) and instances.get(old.name) == old.to_dict(),
        StateConflictError("registry changed before contract reconciliation"),
    )
    assert isinstance(instances, dict)
    updated_instances = dict(instances)
    updated_instances[old.name] = new.to_dict()
    return {"schema_version": raw["schema_version"], "instances": updated_instances}


def _stage_destination_bundle(bundle: ContractBundle, staging_root: Path) -> None:
    ensure_private_directory(staging_root)
    stage = staging_root / f"contract-reconciliation-{uuid.uuid4().hex}"
    ensure_private_directory(stage)
    for filename in contract_filenames():
        source = bundle.directory / filename
        atomic_replace_bytes(stage / filename, source.read_bytes(), mode=0o600)
    ContractBundle.load(
        source_revision=bundle.source_revision,
        directory=stage,
        expected_digest=bundle.contract_digest,
    )


def _receipt(prepared: _PreparedReconciliation) -> dict[str, JsonValue]:
    return {
        "schema_version": 2,
        "name": prepared.record.name,
        "target": prepared.record.target,
        "migration_id": prepared.reconciliation.migration_id,
        "preview_fingerprint": prepared.fingerprint,
        "state": "prepared",
        "files": [
            {
                "path": str(path),
                "mode": mode,
                "before_sha256": _sha256(before),
                "before_base64": base64.b64encode(before).decode("ascii"),
                "after_sha256": _sha256(after),
                "after_base64": base64.b64encode(after).decode("ascii"),
            }
            for path, mode, before, after in prepared.files
        ],
        "attempt_identity_mappings": _attempt_mapping_data(
            prepared.original_transaction,
            prepared.transaction,
        ),
        "per_probe_migrations": per_probe_migration_data(
            prepared.original_transaction,
            prepared.transaction,
            prepared.reconciliation,
        ),
    }


def _parse_receipt(
    raw: dict[str, JsonValue], name: str
) -> tuple[tuple[tuple[Path, int, bytes, bytes], ...], str]:
    state = _receipt_state(raw, name)
    entries = raw.get("files")
    _require(
        isinstance(entries, list) and bool(entries),
        StateError("contract reconciliation receipt files are invalid"),
    )
    assert isinstance(entries, list)
    files = tuple(_parse_receipt_file(entry) for entry in entries)
    _require(
        len({path for path, _, _, _ in files}) == len(files),
        StateError("contract reconciliation receipt has duplicate file paths"),
    )
    return files, state


def _receipt_state(raw: dict[str, JsonValue], name: str) -> str:
    version = raw.get("schema_version")
    if version == 1:
        expected_keys = _RECEIPT_V1_KEYS
    elif version == 2:
        expected_keys = _RECEIPT_V2_KEYS
        validate_per_probe_migrations(raw.get("per_probe_migrations"))
    else:
        raise StateError("contract reconciliation receipt version is unsupported")
    _require(
        frozenset(raw) == expected_keys,
        StateError("contract reconciliation receipt does not match closed v1 schema"),
    )
    _require(
        raw.get("name") == name and isinstance(raw.get("target"), str),
        StateError("contract reconciliation receipt identity is invalid"),
    )
    state = raw.get("state")
    _require(
        state in {"prepared", "applied", "recovered"},
        StateError("contract reconciliation receipt state is invalid"),
    )
    assert isinstance(state, str)
    return state


def _parse_receipt_file(value: JsonValue) -> tuple[Path, int, bytes, bytes]:
    path, mode, before, after, before_hash, after_hash = _receipt_file_fields(value)
    before_bytes = _decode_receipt_bytes(before)
    after_bytes = _decode_receipt_bytes(after)
    _validate_receipt_hashes(before_hash, after_hash, before_bytes, after_bytes)
    return Path(path), mode, before_bytes, after_bytes


def _receipt_file_fields(value: JsonValue) -> tuple[str, int, str, str, JsonValue, JsonValue]:
    _require(
        isinstance(value, dict) and frozenset(value) == _RECEIPT_FILE_KEYS,
        StateError("contract reconciliation receipt file does not match closed v1 schema"),
    )
    assert isinstance(value, dict)
    path = value.get("path")
    mode = value.get("mode")
    before = value.get("before_base64")
    after = value.get("after_base64")
    _require(
        isinstance(path, str)
        and Path(path).is_absolute()
        and not isinstance(mode, bool)
        and isinstance(mode, int),
        StateError("contract reconciliation receipt file identity is invalid"),
    )
    _require(
        isinstance(before, str) and isinstance(after, str),
        StateError("contract reconciliation receipt bytes are invalid"),
    )
    assert (
        isinstance(path, str)
        and isinstance(mode, int)
        and isinstance(before, str)
        and isinstance(after, str)
    )
    return path, mode, before, after, value.get("before_sha256"), value.get("after_sha256")


def _decode_receipt_bytes(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise StateError("contract reconciliation receipt bytes are malformed") from exc


def _validate_receipt_hashes(
    before_hash: JsonValue,
    after_hash: JsonValue,
    before_bytes: bytes,
    after_bytes: bytes,
) -> None:
    if before_hash != _sha256(before_bytes) or after_hash != _sha256(after_bytes):
        raise StateError("contract reconciliation receipt hashes are invalid")


def _preview_result(prepared: _PreparedReconciliation, *, recovered: bool) -> CommandResult:
    return CommandResult(
        kind="contract_reconciliation_preview",
        status="preview_ready",
        message=(
            f"Preview contract reconciliation {prepared.reconciliation.migration_id!r} for {prepared.record.name!r}."
        ),
        exit_code=ExitCode.OK,
        data={
            "name": prepared.record.name,
            "target": prepared.record.target,
            "migration_id": prepared.reconciliation.migration_id,
            "source": _identity_data(prepared.record),
            "destination": _identity_data(prepared.transaction),
            "destination_contract_resolution": (
                prepared.destination_bundle.resolution.to_identity_dict()
            ),
            "contract_files": [
                str(path) for path, _, _, _ in prepared.files[: len(contract_filenames())]
            ],
            "approval_fingerprint": prepared.fingerprint,
            "recovery_performed": recovered,
            "dry_run_writes": 0,
        },
    )


def _approval_required(preview: CommandResult) -> CommandResult:
    return CommandResult(
        kind="contract_reconciliation_preview",
        status="awaiting_user",
        message=(
            "Exact reconciliation preview rendered; pass --yes with its fingerprint to apply it."
        ),
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="approval_required",
        repair="Review this result, then rerun with --yes and --approval-fingerprint.",
        data={key: value for key, value in preview.data.items() if key != "approval_fingerprint"},
    )


def _drift_result(preview: CommandResult) -> CommandResult:
    return CommandResult(
        kind="contract_reconciliation_preview",
        status="awaiting_user",
        message="The supplied approval fingerprint does not match the fresh reconciliation preview.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="probe_drift",
        repair="Rerun --dry-run, review the changed reconciliation, and use its new fingerprint.",
        data={key: value for key, value in preview.data.items() if key != "approval_fingerprint"},
    )


def _identity_data(value: Transaction | InstanceRecord) -> dict[str, JsonValue]:
    return {
        "flow_id": value.flow_id,
        "flow_source_revision": value.flow_source_revision,
        "flow_contract_digest": value.flow_contract_digest,
    }


def _attempt_mapping_data(
    before: Transaction | None,
    after: Transaction,
) -> list[JsonValue]:
    _require(
        before is not None,
        StateConflictError("transaction disappeared while preparing reconciliation receipt"),
    )
    assert before is not None
    entries: list[JsonValue] = []
    for old, new in zip(before.stage_probe_attempts, after.stage_probe_attempts, strict=True):
        old_identity = [old["stage_id"], old["boundary"], old["probe_id"]]
        new_identity = [new["stage_id"], new["boundary"], new["probe_id"]]
        if old_identity != new_identity:
            entries.append(
                {
                    "before_sha256": _canonical_sha256(old),
                    "after_sha256": _canonical_sha256(new),
                    "source": old_identity,
                    "destination": new_identity,
                }
            )
    return entries


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: JsonValue) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{_sha256(encoded)}"


def _json_bytes(value: dict[str, JsonValue]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _verify_after_files(files: tuple[tuple[Path, int, bytes, bytes], ...]) -> None:
    for path, _, _, after in files:
        if _sha256(path.read_bytes()) != _sha256(after):
            raise StateError(f"contract reconciliation postcondition failed: {path}")


def _receipt_path(paths: ManagerPaths, name: str) -> Path:
    return paths.reconciliations_dir / f"{name}.json"


def _require(condition: bool, error: ManagerError) -> None:
    """Raise the supplied domain error when a receipt invariant is false."""

    if not condition:
        raise error
