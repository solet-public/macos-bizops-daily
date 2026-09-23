"""Closed failure-record contract and act-time guards for rollback repair."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

from .models import JsonValue
from .paths import ManagerPaths
from .private_json import load_json_object

RECIPE_ID: Final = "restore_generated_service_bindings_json_v1"
RECIPE_VERSION: Final = 1
ARTIFACT_PATH: Final = Path("profile/config/service_bindings.json")


class RepairRefusedError(RuntimeError):
    """A closed precondition refused before target mutation."""

    def __init__(self, guard: str, reason: str, *, evidence: str) -> None:
        super().__init__(reason)
        self.guard = guard
        self.reason = reason
        self.evidence = evidence


@dataclass(frozen=True)
class ArtifactIdentity:
    path: Path
    digest: str
    mode: int
    uid: int
    gid: int
    device: int
    inode: int
    link_count: int
    size: int

    def public(self) -> dict[str, JsonValue]:
        return {
            "path": str(self.path),
            "sha256": self.digest,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "device": self.device,
            "inode": self.inode,
            "link_count": self.link_count,
            "size": self.size,
        }


@dataclass(frozen=True)
class RepairPlan:
    record_path: Path
    record_digest: str
    input_fingerprint: str
    failure_id: str
    instance_name: str
    target: Path
    manager_transaction_path: Path
    manager_operation_id: str
    transaction_digest: str
    genesis_digest: str
    artifact: ArtifactIdentity
    fault_digest: str
    restore_source: ArtifactIdentity
    expected_digest: str
    expected_mode: int
    expected_uid: int
    expected_gid: int
    touched_set: tuple[str, ...]
    collateral_before: dict[str, JsonValue]


def load_and_pin_record(path: Path, supplied_digest: str) -> tuple[dict[str, JsonValue], Path, str]:
    record_path = absolute_regular(path, "failure_record")
    record_digest = sha256_file(record_path)
    if record_digest != parse_prefixed_digest(supplied_digest):
        refuse(
            "failure_record_digest",
            "failure record bytes do not match the caller-pinned digest",
            record_path,
        )
    return load_private_object(record_path), record_path, record_digest


def preflight_refusal(
    raw: Mapping[str, Any], record_path: Path, paths: ManagerPaths
) -> tuple[str, Path]:
    name, target = record_identity(raw)
    guard_protected(name, target, record_path, paths)
    if raw.get("evidence_disposition") != "disposable_fixture":
        refuse(
            "evidence_disposition",
            "only a freshly declared disposable fixture may be repaired",
            raw.get("evidence_disposition"),
        )
    return name, target


def build_plan(
    raw: Mapping[str, Any],
    record_path: Path,
    record_digest: str,
    paths: ManagerPaths,
) -> RepairPlan:
    validate_record_shape(raw)
    name, target = preflight_refusal(raw, record_path, paths)
    (
        manager_transaction_path,
        manager_operation_id,
        transaction_digest,
        genesis_digest,
    ) = verify_subject_identity(raw, name, target, paths)
    artifact, fault_digest, expected_digest = verify_artifact(raw, target)
    source = verify_restore_source(raw, transaction_digest, expected_digest)
    mode, uid, gid = verify_metadata(raw, artifact)
    verify_diagnostic_and_postcondition(raw, artifact, source, fault_digest, expected_digest)
    guard_active_transaction(paths, name)
    required = artifact.size + source.size + 1_048_576
    if shutil.disk_usage(artifact.path.parent).free < required:
        refuse(
            "disk_space",
            "insufficient space for staging and prior generation",
            artifact.path.parent,
        )
    collateral = collateral_census(target, exclude=artifact.path)
    fingerprint = json_digest(
        {
            "record_sha256": record_digest,
            "target": str(target),
            "manager_transaction_path": str(manager_transaction_path),
            "manager_operation_id": manager_operation_id,
            "transaction_sha256": transaction_digest,
            "recipe_id": RECIPE_ID,
            "recipe_version": RECIPE_VERSION,
            "touched_set": [str(ARTIFACT_PATH)],
        }
    )
    return RepairPlan(
        record_path=record_path,
        record_digest=record_digest,
        input_fingerprint=fingerprint,
        failure_id=string(raw, "failure_id"),
        instance_name=name,
        target=target,
        manager_transaction_path=manager_transaction_path,
        manager_operation_id=manager_operation_id,
        transaction_digest=transaction_digest,
        genesis_digest=genesis_digest,
        artifact=artifact,
        fault_digest=fault_digest,
        restore_source=source,
        expected_digest=expected_digest,
        expected_mode=mode,
        expected_uid=uid,
        expected_gid=gid,
        touched_set=(str(ARTIFACT_PATH),),
        collateral_before=collateral,
    )


def recheck_at_action(plan: RepairPlan, raw: Mapping[str, Any], paths: ManagerPaths) -> None:
    validate_record_shape(raw)
    if sha256_file(plan.record_path) != plan.record_digest:
        refuse("failure_record_changed", "failure record changed after planning", plan.record_path)
    name, target = record_identity(raw)
    running_identity = guard_protected(name, target, plan.record_path, paths)
    if (name, target) != (plan.instance_name, plan.target):
        refuse(
            "subject_identity_changed", "subject identity changed after planning", plan.record_path
        )
    root_name = root_manifest_name(target / "root_manifest.yaml")
    guard_protected(
        root_name,
        target,
        plan.record_path,
        paths,
        running_identity=running_identity,
    )
    if root_name != name:
        refuse("root_manifest_changed", "root manifest identity changed before action", target)
    (
        manager_transaction_path,
        manager_operation_id,
        transaction_digest,
        genesis_digest,
    ) = verify_subject_identity(raw, name, target, paths)
    if (
        manager_transaction_path,
        manager_operation_id,
        transaction_digest,
        genesis_digest,
    ) != (
        plan.manager_transaction_path,
        plan.manager_operation_id,
        plan.transaction_digest,
        plan.genesis_digest,
    ):
        refuse(
            "transaction_changed",
            "manager or genesis transaction identity changed before action",
            manager_transaction_path,
        )
    source = artifact_identity(plan.restore_source.path)
    if source.digest != plan.expected_digest or source.link_count != 1:
        refuse(
            "restore_source_changed", "trusted restore source changed before action", source.path
        )
    artifact = artifact_identity(plan.artifact.path)
    if artifact.link_count != 1 or artifact.digest not in {
        plan.fault_digest,
        plan.expected_digest,
    }:
        refuse("artifact_changed", "artifact identity changed before action", artifact.path)
    guard_active_transaction(paths, name)


def verify_subject_identity(
    raw: Mapping[str, Any],
    name: str,
    target: Path,
    paths: ManagerPaths,
) -> tuple[Path, str, str, str]:
    genesis_path = safe_descendant(target, Path(".solet/genesis.json"))
    genesis = load_regular_object(genesis_path)
    if (
        root_manifest_name(target / "root_manifest.yaml") != name
        or genesis.get("solet_name") != name
    ):
        refuse(
            "subject_identity",
            "root manifest, genesis transaction, and failure record identities disagree",
            target,
        )
    genesis_record = mapping(raw, "genesis_transaction")
    if string(genesis_record, "relative_path") != ".solet/genesis.json":
        refuse("genesis_path", "unexpected genesis transaction path", genesis_path)
    genesis_digest = string(genesis_record, "sha256")
    if sha256_file(genesis_path) != genesis_digest:
        refuse("genesis_digest", "genesis transaction identity changed", genesis_path)

    manager_record = mapping(raw, "manager_transaction")
    canonical_path = absolute_regular(
        paths.transaction_path(name),
        "manager_transaction",
    )
    if string(manager_record, "path") != str(canonical_path):
        refuse(
            "manager_transaction_path",
            "failure record does not name the canonical manager transaction",
            canonical_path,
        )
    manager_digest = string(manager_record, "sha256")
    if sha256_file(canonical_path) != manager_digest:
        refuse(
            "manager_transaction_digest",
            "canonical manager transaction bytes do not match the failure record",
            canonical_path,
        )
    manager_transaction = load_regular_object(canonical_path)
    operation_id = string(manager_record, "operation_id")
    failure = mapping(manager_transaction, "repair_failure")
    observed = (
        manager_transaction.get("schema_version"),
        manager_transaction.get("operation_id"),
        manager_transaction.get("name"),
        manager_transaction.get("target"),
        manager_transaction.get("status"),
        failure.get("failure_id"),
        failure.get("reason_code"),
        failure.get("recipe_id"),
        failure.get("artifact_relative_path"),
    )
    expected = (
        1,
        operation_id,
        name,
        str(target),
        "failed",
        string(raw, "failure_id"),
        string(raw, "reason_code"),
        RECIPE_ID,
        str(ARTIFACT_PATH),
    )
    if observed != expected or raw.get("transaction_state") != "failed":
        refuse(
            "manager_transaction_identity",
            "canonical manager transaction does not bind this subject, failure, and recipe",
            canonical_path,
        )
    return canonical_path, operation_id, manager_digest, genesis_digest


def verify_artifact(raw: Mapping[str, Any], target: Path) -> tuple[ArtifactIdentity, str, str]:
    record = mapping(raw, "artifact")
    artifact = artifact_identity(safe_descendant(target, Path(string(record, "relative_path"))))
    fault = string(record, "fault_sha256")
    expected = string(record, "expected_sha256")
    if artifact.digest not in {fault, expected}:
        refuse(
            "changed_failure_state",
            "artifact no longer matches recorded fault or restored state",
            artifact.path,
        )
    if artifact.link_count != 1 or artifact.device != target.stat().st_dev:
        refuse("artifact_link_or_mount", "artifact link or mount identity is unsafe", artifact.path)
    return artifact, fault, expected


def verify_restore_source(
    raw: Mapping[str, Any], transaction_digest: str, expected_digest: str
) -> ArtifactIdentity:
    before = mapping(raw, "before_image")
    source = artifact_identity(absolute_regular(Path(string(before, "path")), "before_image"))
    expected = (
        expected_digest,
        string(before, "sha256"),
        string(before, "source_kind"),
        string(before, "transaction_sha256"),
    )
    if (
        source.digest,
        source.digest,
        "same_transaction_before_image",
        transaction_digest,
    ) != expected:
        refuse(
            "restore_source", "restore source is not authenticated to this transaction", source.path
        )
    if source.link_count != 1:
        refuse("hard_link", "restore source must have exactly one link", source.path)
    validate_service_bindings(source.path)
    return source


def verify_metadata(raw: Mapping[str, Any], artifact: ArtifactIdentity) -> tuple[int, int, int]:
    record = mapping(raw, "artifact")
    expected = (integer(record, "mode"), integer(record, "uid"), integer(record, "gid"))
    if (artifact.mode, artifact.uid, artifact.gid) != expected:
        refuse("artifact_metadata", "failing artifact metadata changed", artifact.path)
    return expected


def verify_diagnostic_and_postcondition(
    raw: Mapping[str, Any],
    artifact: ArtifactIdentity,
    source: ArtifactIdentity,
    fault_digest: str,
    expected_digest: str,
) -> None:
    diagnostic = mapping(raw, "diagnostic")
    observed = (
        string(diagnostic, "failed_subject_status"),
        string(diagnostic, "healthy_control_status"),
        string(diagnostic, "failed_subject_sha256"),
        string(diagnostic, "healthy_control_sha256"),
    )
    if observed != ("failed", "verified", fault_digest, source.digest):
        refuse(
            "diagnostic_discrimination", "failure record lacks a discriminating pair", artifact.path
        )
    postcondition = mapping(raw, "postcondition")
    if (
        string(postcondition, "kind") != "service_bindings_json_v1"
        or string(postcondition, "expected_sha256") != expected_digest
    ):
        refuse("postcondition_contract", "unknown or contradictory postcondition", artifact.path)


def validate_record_shape(raw: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "failure_id",
        "recipe_id",
        "reason_code",
        "instance_name",
        "target",
        "transaction_state",
        "evidence_disposition",
        "genesis_transaction",
        "manager_transaction",
        "artifact",
        "before_image",
        "diagnostic",
        "postcondition",
    }
    if set(raw) != required:
        refuse("record_schema", "failure record fields are not the closed v1 schema", "record")
    identity = (
        raw.get("schema_version"),
        raw.get("recipe_id"),
        raw.get("reason_code"),
        raw.get("transaction_state"),
        raw.get("evidence_disposition"),
    )
    if identity != (1, RECIPE_ID, "service_bindings_json_corrupt", "failed", "disposable_fixture"):
        refuse(
            "recipe_allowlist",
            "record does not select the closed failed-transaction recipe",
            identity,
        )
    artifact = mapping(raw, "artifact")
    if (
        string(artifact, "relative_path") != str(ARTIFACT_PATH)
        or string(artifact, "artifact_class") != "generated_service_bindings"
    ):
        refuse("touched_set", "record requests a non-allowlisted touched path", artifact)
    digests = (
        string(artifact, "fault_sha256"),
        string(artifact, "expected_sha256"),
        string(mapping(raw, "genesis_transaction"), "sha256"),
        string(mapping(raw, "manager_transaction"), "sha256"),
        string(mapping(raw, "before_image"), "sha256"),
    )
    for digest in digests:
        validate_digest(digest)


def record_identity(raw: Mapping[str, Any]) -> tuple[str, Path]:
    name = string(raw, "instance_name")
    if not name or "/" in name or name in {".", ".."}:
        refuse("subject_identity", "invalid subject name", name)
    raw_target = Path(string(raw, "target")).expanduser()
    if not raw_target.is_absolute():
        refuse("subject_identity", "target must be absolute", raw_target)
    return name, absolute_regular(raw_target, "target", directory=True)


def guard_protected(
    name: str,
    target: Path,
    record: Path,
    paths: ManagerPaths,
    *,
    running_identity: tuple[str, Path] | None = None,
) -> tuple[str, Path]:
    identity = running_identity or derive_running_identity()
    running_name, running_target = identity
    if name.casefold() == running_name.casefold() or target == running_target:
        refuse(
            "protected_running_identity",
            "rollback repair cannot target the running Solet identity",
            {
                "subject_name": name,
                "subject_target": str(target),
                "running_name": running_name,
                "running_target": str(running_target),
            },
        )
    for candidate in (target, record, paths.config_dir, paths.state_dir, paths.cache_dir):
        canonical = candidate.resolve(strict=False)
        if canonical == running_target or running_target in canonical.parents:
            refuse(
                "protected_running_path",
                "rollback repair state cannot be stored inside the running Solet",
                canonical,
            )
    for parent in (target, *target.parents):
        if parent.exists() and stat.S_ISLNK(parent.lstat().st_mode):
            refuse("symlink_traversal", "target ancestry contains a symlink", parent)
    return identity


def derive_running_identity() -> tuple[str, Path]:
    name = os.environ.get("SOLET_NAME", "").strip()
    raw_app_home = os.environ.get("APP_HOME", "").strip()
    if not name or "/" in name or name in {".", ".."} or not raw_app_home:
        refuse(
            "running_identity",
            "SOLET_NAME and APP_HOME must identify the running Solet",
            {"solet_name": name, "app_home": raw_app_home},
        )
    try:
        app_home = absolute_regular(Path(raw_app_home), "APP_HOME", directory=True)
        target = app_home.parent
        manifest_name = root_manifest_name(target / "root_manifest.yaml")
    except RepairRefusedError as exc:
        raise RepairRefusedError(
            "running_identity",
            "APP_HOME does not identify a readable running Solet",
            evidence=exc.evidence,
        ) from exc
    if manifest_name.casefold() != name.casefold():
        refuse(
            "running_identity",
            "SOLET_NAME and APP_HOME identify different running Solets",
            {
                "solet_name": name,
                "app_home": str(app_home),
                "root_manifest_name": manifest_name,
            },
        )
    return name, target


def guard_active_transaction(paths: ManagerPaths, name: str) -> None:
    transaction = paths.transaction_path(name)
    if not transaction.is_file():
        refuse(
            "manager_transaction",
            "canonical manager transaction is missing",
            transaction,
        )
    if load_private_object(transaction).get("status") != "failed":
        refuse(
            "active_transaction",
            "canonical manager transaction is not the recorded failed transaction",
            transaction,
        )


def artifact_identity(path: Path) -> ArtifactIdentity:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        refuse("artifact_type", "artifact must be a regular non-link file", path)
    return ArtifactIdentity(
        path=path,
        digest=sha256_file(path),
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
        device=info.st_dev,
        inode=info.st_ino,
        link_count=info.st_nlink,
        size=info.st_size,
    )


def collateral_census(root: Path, *, exclude: Path) -> dict[str, JsonValue]:
    census: dict[str, JsonValue] = {}
    for path in sorted(root.rglob("*")):
        if path == exclude:
            continue
        info = path.lstat()
        relative = str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            refuse("collateral_symlink", "subject contains an unresolved symlink", path)
        if stat.S_ISDIR(info.st_mode):
            census[relative] = {
                "type": "directory",
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
        elif stat.S_ISREG(info.st_mode):
            census[relative] = {
                "type": "file",
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "size": info.st_size,
                "sha256": sha256_file(path),
                "link_count": info.st_nlink,
            }
        else:
            refuse("collateral_type", "subject contains an unsupported object", path)
    return census


def absolute_regular(path: Path, label: str, *, directory: bool = False) -> Path:
    absolute = path.expanduser()
    if not absolute.is_absolute():
        refuse(label, f"{label} must be absolute", path)
    try:
        info = absolute.lstat()
    except OSError as exc:
        raise RepairRefusedError(label, f"{label} is unavailable", evidence=str(path)) from exc
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or stat.S_ISLNK(info.st_mode):
        refuse(label, f"{label} has an unsafe filesystem type", path)
    return absolute.resolve(strict=True)


def safe_descendant(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        refuse("touched_set", "artifact path is not a safe relative path", relative)
    resolved = absolute_regular(root / relative, "artifact_path")
    if root not in resolved.parents:
        refuse("touched_set", "artifact escapes the subject", resolved)
    return resolved


def root_manifest_name(path: Path) -> str:
    resolved = absolute_regular(path, "root_manifest")
    names = [
        line.split(":", 1)[1].strip()
        for line in resolved.read_text(encoding="utf-8").splitlines()
        if line.startswith("solet_name:")
    ]
    if len(names) != 1 or not names[0]:
        refuse("root_manifest", "root manifest has no unique solet_name", path)
    return names[0].strip("'\"")


def validate_service_bindings(path: Path) -> None:
    if not load_regular_object(path):
        refuse("restore_schema", "service bindings must be a nonempty JSON object", path)


def load_regular_object(path: Path) -> dict[str, Any]:
    artifact_identity(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepairRefusedError(
            "json_schema", "expected readable JSON", evidence=str(path)
        ) from exc
    if not isinstance(value, dict):
        refuse("json_schema", "expected a JSON object", path)
    return value


def load_private_object(path: Path) -> dict[str, JsonValue]:
    value = load_json_object(path)
    if value is None:
        refuse("json_schema", "expected a private JSON object", path)
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        while chunk := handle.read(1_048_576):
            digest.update(chunk)
    return digest.hexdigest()


def json_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def parse_prefixed_digest(value: str) -> str:
    if not value.startswith("sha256:"):
        refuse("failure_record_digest", "failure-record digest must use sha256 prefix", value)
    digest = value.removeprefix("sha256:")
    validate_digest(digest)
    return digest


def validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        refuse("digest_format", "digest is not 64 lowercase hexadecimal characters", value)


def mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    child = value.get(key)
    if not isinstance(child, Mapping):
        refuse("record_schema", f"{key} must be an object", key)
    return child


def string(value: Mapping[str, Any], key: str) -> str:
    child = value.get(key)
    if not isinstance(child, str):
        refuse("record_schema", f"{key} must be a string", key)
    return child


def integer(value: Mapping[str, Any], key: str) -> int:
    child = value.get(key)
    if not isinstance(child, int) or isinstance(child, bool):
        refuse("record_schema", f"{key} must be an integer", key)
    return child


def refuse(guard: str, reason: str, evidence: object) -> NoReturn:
    raise RepairRefusedError(guard, reason, evidence=str(evidence))
