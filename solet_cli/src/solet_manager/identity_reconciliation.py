"""Receipt-bound in-field migration of a reconciled setup identity."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .contract_reconciliation import reconciliation_receipt_before_flow_source_revision
from .errors import ProbeDriftError, StateConflictError, StateError
from .models import CommandResult, ExitCode, InstanceRecord, JsonValue
from .paths import ManagerPaths
from .reconciliation_ceremony import run_reconciliation_ceremony
from .registry import InstanceRegistry
from .state_io import atomic_replace_bytes, atomic_write_json, load_json_object
from .transaction import Transaction, load_transaction, target_install_state_projection

_RECEIPT_KEYS = frozenset(
    {"schema_version", "name", "target", "seed", "preview_fingerprint", "state", "files"}
)
_RECEIPT_FILE_KEYS = frozenset(
    {
        "path",
        "mode",
        "before_sha256",
        "before_base64",
        "after_sha256",
        "after_base64",
    }
)
_RECEIPT_STATES = frozenset({"prepared", "applied", "recovered"})
_GIT_REVISION = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class TargetRevision:
    """The target checkout identities that must continue to name its seed."""

    head: str
    main: str


@dataclass(frozen=True)
class _Replacement:
    path: Path
    mode: int
    before: bytes
    after: bytes


@dataclass(frozen=True)
class _PreparedIdentityReconciliation:
    record: InstanceRecord
    transaction: Transaction
    revised_transaction: Transaction
    revised_record: InstanceRecord
    old_revision: str
    new_revision: str
    files: tuple[_Replacement, ...]
    fingerprint: str | None


type TargetRevisionProbe = Callable[[Path], TargetRevision]


class IdentityReconciliationManager:
    """Restore a receipt-proven historical flow revision to its seed commit."""

    def __init__(
        self,
        *,
        paths: ManagerPaths,
        target_revision_probe: TargetRevisionProbe | None = None,
    ) -> None:
        self._paths = paths
        self._registry = InstanceRegistry(paths.registry_path)
        self._target_revision_probe = target_revision_probe or _target_revisions

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
            recover=lambda: recover_identity_reconciliation(self._paths, name),
            prepare=lambda: self._prepare(name),
            preview_result=_preview_result,
            approval_required=_approval_required,
            drift_result=_drift_result,
            apply=self._apply,
            fingerprint=_required_fingerprint,
        )

    def _prepare(self, name: str) -> _PreparedIdentityReconciliation:
        record = self._registry.require(name)
        transaction = load_transaction(self._paths.transaction_path(name))
        if transaction is None:
            raise StateConflictError(f"managed instance {name!r} lacks its transaction journal")
        _validate_stored_agreement(record, transaction)
        old_revision = transaction.flow_source_revision
        new_revision = transaction.seed.commit
        if old_revision == new_revision:
            return _PreparedIdentityReconciliation(
                record,
                transaction,
                transaction,
                record,
                old_revision,
                new_revision,
                (),
                None,
            )
        receipt_before = reconciliation_receipt_before_flow_source_revision(self._paths, name)
        if receipt_before is None:
            raise StateConflictError(
                "identity reconciliation requires an existing terminal contract reconciliation receipt"
            )
        if receipt_before != new_revision:
            raise StateConflictError(
                "identity reconciliation receipt before-image does not agree with the seed commit"
            )
        revisions = self._target_revision_probe(Path(record.target))
        if revisions.head != new_revision or revisions.main != new_revision:
            raise StateConflictError(
                "identity reconciliation requires target HEAD and main to agree with the seed commit"
            )
        revised_transaction = transaction.reconciled_identity(flow_source_revision=new_revision)
        revised_record = self._registry.reconciled_identity_record(
            record,
            flow_source_revision=new_revision,
        )
        files = _replacement_files(
            paths=self._paths,
            record=record,
            transaction=revised_transaction,
            revised_record=revised_record,
        )
        fingerprint = _canonical_sha256(
            {
                "name": name,
                "target": record.target,
                "old_revision": old_revision,
                "new_revision": new_revision,
                "seed_commit": transaction.seed.commit,
                "target_head": revisions.head,
                "target_main": revisions.main,
                "receipt_before_revision": receipt_before,
                "files": [
                    {
                        "path": str(item.path),
                        "before_sha256": _sha256(item.before),
                        "after_sha256": _sha256(item.after),
                    }
                    for item in files
                ],
            }
        )
        return _PreparedIdentityReconciliation(
            record,
            transaction,
            revised_transaction,
            revised_record,
            old_revision,
            new_revision,
            files,
            fingerprint,
        )

    def _apply(self, prepared: _PreparedIdentityReconciliation) -> CommandResult:
        if prepared.fingerprint is None:
            return _current_result(prepared)
        fresh = self._prepare(prepared.record.name)
        if fresh.fingerprint != prepared.fingerprint:
            raise ProbeDriftError(
                "identity reconciliation state changed after preview approval; no mutation was performed",
                repair="Rerun reconcile-identity --dry-run and review the new fingerprint.",
            )
        receipt_path = _receipt_path(self._paths, fresh.record.name)
        receipt = _receipt(fresh)
        atomic_write_json(receipt_path, receipt)
        for replacement in fresh.files:
            if replacement.path.read_bytes() != replacement.before:
                raise StateConflictError(
                    f"identity reconciliation target changed before promotion: {replacement.path}"
                )
            atomic_replace_bytes(replacement.path, replacement.after, mode=replacement.mode)
        _verify_after_files(fresh.files)
        applied = dict(receipt)
        applied["state"] = "applied"
        atomic_write_json(receipt_path, applied)
        return CommandResult(
            kind="identity_reconciliation",
            status="reconciled",
            message=f"Reconciled stored identity for {fresh.record.name!r} to its seed commit.",
            exit_code=ExitCode.OK,
            data={
                "name": fresh.record.name,
                "old_revision": fresh.old_revision,
                "new_revision": fresh.new_revision,
                "seed_commit": fresh.transaction.seed.commit,
                "approval_fingerprint_cleared": True,
                "changed_files": [str(item.path) for item in fresh.files],
            },
        )


def recover_identity_reconciliation(paths: ManagerPaths, name: str) -> bool:
    """Recover an interrupted identity migration to authenticated before-images."""

    receipt_path = _receipt_path(paths, name)
    raw = load_json_object(receipt_path, missing_ok=True)
    if raw is None:
        return False
    files, state = _parse_receipt(raw, name)
    if state in {"applied", "recovered"}:
        return False
    current = tuple(_sha256(item.path.read_bytes()) for item in files)
    before = tuple(_sha256(item.before) for item in files)
    after = tuple(_sha256(item.after) for item in files)
    if current == after:
        terminal = dict(raw)
        terminal["state"] = "applied"
        atomic_write_json(receipt_path, terminal)
        return True
    if any(value not in {old, new} for value, old, new in zip(current, before, after, strict=True)):
        raise StateConflictError(
            "identity reconciliation recovery found bytes outside its authenticated before/after set"
        )
    for replacement in files:
        atomic_replace_bytes(replacement.path, replacement.before, mode=replacement.mode)
    terminal = dict(raw)
    terminal["state"] = "recovered"
    atomic_write_json(receipt_path, terminal)
    return True


def _validate_stored_agreement(record: InstanceRecord, transaction: Transaction) -> None:
    answers_revision = transaction.answers.get("flow_source_revision")
    if not isinstance(answers_revision, str):
        raise StateConflictError("identity reconciliation requires an answers flow_source_revision")
    if (
        record.target != transaction.target
        or record.flow_id != transaction.flow_id
        or record.flow_source_revision != transaction.flow_source_revision
        or answers_revision != transaction.flow_source_revision
        or record.seed_commit != transaction.seed.commit
    ):
        raise StateConflictError(
            "identity reconciliation requires registry, journal, answers, and seed identities to agree"
        )


def _replacement_files(
    *,
    paths: ManagerPaths,
    record: InstanceRecord,
    transaction: Transaction,
    revised_record: InstanceRecord,
) -> tuple[_Replacement, ...]:
    registry_raw = load_json_object(paths.registry_path)
    if registry_raw is None:
        raise StateConflictError("registry disappeared during identity reconciliation")
    registry_payload = _registry_payload(registry_raw, record, revised_record)
    return (
        _replacement_json_file(paths.transaction_path(record.name), transaction.to_dict()),
        _replacement_json_file(paths.registry_path, registry_payload),
        _replacement_json_file(
            Path(record.target) / ".solet" / "install-state.json",
            target_install_state_projection(transaction),
        ),
    )


def _registry_payload(
    raw: dict[str, JsonValue], old: InstanceRecord, updated: InstanceRecord
) -> dict[str, JsonValue]:
    instances = raw.get("instances")
    if not isinstance(instances, dict) or instances.get(old.name) != old.to_dict():
        raise StateConflictError("registry changed before identity reconciliation promotion")
    return {**raw, "instances": {**instances, old.name: updated.to_dict()}}


def _replacement_json_file(path: Path, value: dict[str, JsonValue]) -> _Replacement:
    try:
        return _Replacement(
            path=path,
            mode=stat.S_IMODE(path.stat().st_mode),
            before=path.read_bytes(),
            after=_json_bytes(value),
        )
    except OSError as exc:
        raise StateConflictError(
            f"required identity state file is unavailable: {path}: {exc}"
        ) from exc


def _target_revisions(target: Path) -> TargetRevision:
    completed = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD", "main"],
        check=False,
        capture_output=True,
        text=True,
    )
    values = completed.stdout.splitlines()
    if (
        completed.returncode != 0
        or len(values) != 2
        or not all(_GIT_REVISION.fullmatch(value) for value in values)
    ):
        raise StateConflictError("identity reconciliation could not establish target HEAD and main")
    return TargetRevision(head=values[0], main=values[1])


def _receipt_path(paths: ManagerPaths, name: str) -> Path:
    return paths.identity_reconciliations_dir / f"{name}.json"


def _receipt(prepared: _PreparedIdentityReconciliation) -> dict[str, JsonValue]:
    assert prepared.fingerprint is not None
    return {
        "schema_version": 1,
        "name": prepared.record.name,
        "target": prepared.record.target,
        "seed": prepared.transaction.seed.identity_dict(),
        "preview_fingerprint": prepared.fingerprint,
        "state": "prepared",
        "files": [_receipt_file(item) for item in prepared.files],
    }


def _receipt_file(replacement: _Replacement) -> dict[str, JsonValue]:
    return {
        "path": str(replacement.path),
        "mode": replacement.mode,
        "before_sha256": _sha256(replacement.before),
        "before_base64": base64.b64encode(replacement.before).decode("ascii"),
        "after_sha256": _sha256(replacement.after),
        "after_base64": base64.b64encode(replacement.after).decode("ascii"),
    }


def _parse_receipt(raw: dict[str, JsonValue], name: str) -> tuple[tuple[_Replacement, ...], str]:
    state = _receipt_state(raw, name)
    files = _receipt_files(raw)
    if len({item.path for item in files}) != len(files):
        raise StateError("identity reconciliation receipt has duplicate file paths")
    return files, state


def _receipt_state(raw: dict[str, JsonValue], name: str) -> str:
    if frozenset(raw) != _RECEIPT_KEYS or raw.get("schema_version") != 1:
        raise StateError("identity reconciliation receipt does not match the closed v1 schema")
    if raw.get("name") != name or not isinstance(raw.get("target"), str):
        raise StateError("identity reconciliation receipt identity is invalid")
    state = raw.get("state")
    if state not in _RECEIPT_STATES:
        raise StateError("identity reconciliation receipt state is invalid")
    return state


def _receipt_files(raw: dict[str, JsonValue]) -> tuple[_Replacement, ...]:
    values = raw.get("files")
    if not isinstance(values, list) or not values:
        raise StateError("identity reconciliation receipt files are invalid")
    return tuple(_parse_receipt_file(value) for value in values)


def _parse_receipt_file(value: JsonValue) -> _Replacement:
    if not isinstance(value, dict) or frozenset(value) != _RECEIPT_FILE_KEYS:
        raise StateError("identity reconciliation receipt file does not match the closed v1 schema")
    path = value.get("path")
    mode = value.get("mode")
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or isinstance(mode, bool)
        or not isinstance(mode, int)
    ):
        raise StateError("identity reconciliation receipt file identity is invalid")
    before = _decode(value.get("before_base64"))
    after = _decode(value.get("after_base64"))
    if value.get("before_sha256") != _sha256(before) or value.get("after_sha256") != _sha256(after):
        raise StateError("identity reconciliation receipt hashes are invalid")
    return _Replacement(Path(path), mode, before, after)


def _decode(value: JsonValue) -> bytes:
    if not isinstance(value, str):
        raise StateError("identity reconciliation receipt bytes are invalid")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise StateError("identity reconciliation receipt bytes are malformed") from exc


def _preview_result(prepared: _PreparedIdentityReconciliation, recovered: bool) -> CommandResult:
    if prepared.fingerprint is None:
        return _current_result(prepared)
    return CommandResult(
        kind="identity_reconciliation_preview",
        status="preview_ready",
        message=f"Preview identity reconciliation for {prepared.record.name!r}.",
        exit_code=ExitCode.OK,
        data={
            "name": prepared.record.name,
            "target": prepared.record.target,
            "old_revision": prepared.old_revision,
            "new_revision": prepared.new_revision,
            "seed_commit": prepared.transaction.seed.commit,
            "changed_files": [str(item.path) for item in prepared.files],
            "approval_fingerprint": prepared.fingerprint,
            "recovery_performed": recovered,
            "dry_run_writes": 0,
        },
    )


def _current_result(prepared: _PreparedIdentityReconciliation) -> CommandResult:
    return CommandResult(
        kind="identity_reconciliation",
        status="identity_current",
        message=f"Stored identity for {prepared.record.name!r} already equals its seed commit.",
        exit_code=ExitCode.OK,
        data={
            "name": prepared.record.name,
            "old_revision": prepared.old_revision,
            "new_revision": prepared.new_revision,
            "seed_commit": prepared.transaction.seed.commit,
            "changed_files": [],
        },
    )


def _approval_required(preview: CommandResult) -> CommandResult:
    if preview.status == "identity_current":
        return preview
    return CommandResult(
        kind="identity_reconciliation_preview",
        status="awaiting_user",
        message="Exact identity preview rendered; pass --yes with its fingerprint to apply it.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="approval_required",
        repair="Review this result, then rerun with --yes and --approval-fingerprint.",
        data={key: value for key, value in preview.data.items() if key != "approval_fingerprint"},
    )


def _drift_result(preview: CommandResult) -> CommandResult:
    if preview.status == "identity_current":
        return preview
    return CommandResult(
        kind="identity_reconciliation_preview",
        status="awaiting_user",
        message="The supplied approval fingerprint does not match the fresh identity preview.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="probe_drift",
        repair="Rerun reconcile-identity --dry-run and review the new fingerprint.",
        data={key: value for key, value in preview.data.items() if key != "approval_fingerprint"},
    )


def _required_fingerprint(prepared: _PreparedIdentityReconciliation) -> str:
    if prepared.fingerprint is None:
        return ""
    return prepared.fingerprint


def _verify_after_files(files: tuple[_Replacement, ...]) -> None:
    for replacement in files:
        if replacement.path.read_bytes() != replacement.after:
            raise StateError(f"identity reconciliation postcondition failed: {replacement.path}")


def _json_bytes(value: dict[str, JsonValue]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: JsonValue) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{_sha256(encoded)}"
