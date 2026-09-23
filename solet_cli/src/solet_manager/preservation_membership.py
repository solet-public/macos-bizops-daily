"""One-shot authorization for the exact service-bindings repair."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final, NoReturn, cast

AUTHORITY_KIND: Final = "caller_owned_manager_subject_v2"
EFFECT_RELATIVE_PATH: Final = Path("profile/config/service_bindings.json")
REQUIRED_LOGICAL_IDENTITIES: Final = (
    "artifact:profile/config/service_bindings.json",
    "service:local_self_deployment_service",
    "provider:macos_self_deployment_plugin",
)
RuntimeResolver = Callable[[str, Path], tuple[str, ...]]


class ProtectionRefusedError(RuntimeError):
    def __init__(self, guard: str, reason_code: str, message: str, evidence: object) -> None:
        super().__init__(f"repair refused: {message}")
        self.guard = guard
        self.reason_code = reason_code
        self.authority_kind = AUTHORITY_KIND
        self.evidence = str(evidence)


@dataclass(frozen=True)
class RepairEffectSet:
    paths: tuple[Path, ...]
    logical_identities: tuple[str, ...]
    expected_before_sha256: str
    expected_after_sha256: str
    replacement_bytes: bytes


@dataclass(frozen=True)
class ManagerLocations:
    config_dir: Path
    state_dir: Path


@dataclass(frozen=True)
class OwnershipPlan:
    authority_kind: str
    plan_id: str
    acknowledgement_token: str
    target_name: str
    target_root: str
    operation: str
    effect_path: str
    expected_before_sha256: str
    expected_after_sha256: str
    logical_identities: tuple[str, ...]
    runtime_identities: tuple[str, ...]
    root_manifest_sha256: str
    manager_transaction_path: str
    manager_transaction_sha256: str
    preserved_roots_config_path: str
    preserved_roots_config_sha256: str
    preserved_roots: tuple[str, ...]
    plan_digest: str


def build_preservation_snapshot(
    *,
    environ: Mapping[str, str],
    target_name: str,
    target_root: Path,
    effects: RepairEffectSet,
    runtime_resolver: RuntimeResolver,
    operation: str,
) -> OwnershipPlan:
    locations = _manager_locations(environ)
    token = secrets.token_urlsafe(32)
    plan_id = _sha(token.encode())
    plan = _observe(
        environ, target_name, target_root, effects, runtime_resolver, operation,
        plan_id, token,
    )
    _persist(locations, plan, effects.replacement_bytes)
    return plan


def revalidate_preservation_snapshot(
    planned: object,
    *,
    acknowledgement: str,
    environ: Mapping[str, str],
    target_name: str,
    target_root: Path,
    effects: RepairEffectSet,
    runtime_resolver: RuntimeResolver,
    operation: str,
) -> OwnershipPlan:
    if not isinstance(planned, OwnershipPlan):
        _refuse("plan", "plan_invalid", "act requires an ownership plan", planned)
    if not hmac.compare_digest(acknowledgement, planned.acknowledgement_token):
        _refuse("acknowledgement", "acknowledgement_mismatch", "acknowledgement must equal the exact plan token", acknowledgement)
    locations = _manager_locations(environ)
    stored, replacement, available = _load(locations, acknowledgement)
    if stored != planned:
        _refuse("plan", "plan_payload_mismatch", "request plan differs from persisted plan", planned.plan_id)
    _consume(available, locations, stored.plan_id)
    current = _observe(
        environ, target_name, target_root, effects, runtime_resolver, operation,
        stored.plan_id, acknowledgement,
    )
    if current != stored:
        _refuse("plan", "plan_stale", "authority or target changed after planning", current.plan_digest)
    root = Path(stored.target_root)
    target = Path(stored.effect_path)
    before = _census(root, target)
    _cas_write(target, stored.expected_before_sha256, replacement)
    if _sha_file(target) != stored.expected_after_sha256:
        _refuse("postcondition", "effect_postcondition_failed", "postimage differs", target)
    if _census(root, target) != before:
        _refuse("postcondition", "collateral_changed", "objects outside the declared artifact changed", root)
    return stored


def _observe(
    environ: Mapping[str, str], name: str, root_input: Path,
    effects: RepairEffectSet, resolver: RuntimeResolver, operation: str,
    plan_id: str, token: str,
) -> OwnershipPlan:
    root = _running_target(environ, name, root_input)
    locations = _manager_locations(environ)
    transaction, transaction_sha = _provenance(locations, name, root)
    config, config_sha, preserved = _preserved(locations, root)
    effect = _effect(root, effects)
    runtime = _identities(resolver(name, root), "runtime_identity_unresolved")
    draft = OwnershipPlan(
        AUTHORITY_KIND, plan_id, token, name, str(root), operation, str(effect),
        effects.expected_before_sha256, effects.expected_after_sha256,
        effects.logical_identities, runtime, _sha_file(root / "root_manifest.yaml"),
        str(transaction), transaction_sha, str(config), config_sha,
        tuple(str(path) for path in preserved), "",
    )
    return replace(draft, plan_digest=_digest(draft))


def _persist(locations: ManagerLocations, plan: OwnershipPlan, replacement: bytes) -> None:
    available = locations.state_dir / "repair_plans/available"
    consumed = locations.state_dir / "repair_plans/consumed"
    _private_dir(available)
    _private_dir(consumed)
    path = available / f"{plan.plan_id}.json"
    payload = {
        "plan": asdict(plan),
        "replacement_b64": base64.b64encode(replacement).decode("ascii"),
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(payload, target, sort_keys=True, separators=(",", ":"))
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
    except OSError as exc:
        _refuse("plan_store", "plan_persist_failed", "could not persist one-shot plan", exc)


def _load(locations: ManagerLocations, token: str) -> tuple[OwnershipPlan, bytes, Path]:
    _validate_token(token)
    plan_id = _sha(token.encode())
    path = locations.state_dir / "repair_plans/available" / f"{plan_id}.json"
    payload = _json(path, "plan_store", "plan_token_unavailable")
    plan, replacement = _decode_record(payload, path)
    if plan.plan_id != plan_id or plan.acknowledgement_token != token:
        _refuse("plan_store", "plan_record_malformed", "token does not bind record", path)
    if _digest(plan) != plan.plan_digest or _sha(replacement) != plan.expected_after_sha256:
        _refuse("plan_store", "plan_record_malformed", "record digest or replacement differs", path)
    return plan, replacement, path


def _validate_token(token: str) -> None:
    if not 40 <= len(token) <= 64 or any(character not in _TOKEN_CHARS for character in token):
        _refuse("plan_store", "plan_token_invalid", "token syntax is invalid", token)


def _decode_record(payload: dict[str, Any], path: Path) -> tuple[OwnershipPlan, bytes]:
    raw_plan = payload.get("plan")
    encoded = payload.get("replacement_b64")
    if not isinstance(raw_plan, dict) or not isinstance(encoded, str):
        _refuse("plan_store", "plan_record_malformed", "plan record shape is invalid", path)
    try:
        plan = _plan_from_dict(raw_plan)
        replacement = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        _refuse("plan_store", "plan_record_malformed", "plan record cannot be decoded", exc)
    return plan, replacement


def _consume(path: Path, locations: ManagerLocations, plan_id: str) -> None:
    destination = locations.state_dir / "repair_plans/consumed" / f"{plan_id}.json"
    try:
        os.replace(path, destination)
    except OSError as exc:
        _refuse("plan_store", "plan_token_unavailable", "plan token is consumed or unavailable", exc)


def _plan_from_dict(value: dict[str, Any]) -> OwnershipPlan:
    copied = dict(value)
    for name in ("logical_identities", "runtime_identities", "preserved_roots"):
        raw = copied[name]
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise TypeError(name)
        copied[name] = tuple(cast(list[str], raw))
    return OwnershipPlan(**copied)


def _digest(plan: OwnershipPlan) -> str:
    return _sha(_canonical({**asdict(plan), "plan_digest": ""}))


def _effect(root: Path, effects: RepairEffectSet) -> Path:
    expected = root / EFFECT_RELATIVE_PATH
    if effects.paths != (expected,) or effects.logical_identities != REQUIRED_LOGICAL_IDENTITIES:
        _refuse("effects", "effect_target_mismatch", "effect is not the pinned service-bindings identity", effects)
    if not _valid_sha(effects.expected_before_sha256) or not _valid_sha(effects.expected_after_sha256):
        _refuse("effects", "effect_set_invalid", "effect digests are invalid", effects)
    if _sha(effects.replacement_bytes) != effects.expected_after_sha256:
        _refuse("effects", "effect_set_invalid", "replacement bytes do not match postimage", expected)
    path = _regular(expected, "effects", "effect_target_mismatch")
    if _sha_file(path) != effects.expected_before_sha256:
        _refuse("plan", "plan_stale", "service-bindings preimage changed", path)
    return path


def _cas_write(path: Path, before_sha: str, replacement: bytes) -> None:
    if _sha_file(path) != before_sha:
        _refuse("write", "plan_stale", "pre-write CAS failed", path)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temp)
    try:
        os.fchmod(descriptor, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(descriptor, "wb") as target:
            target.write(replacement)
            target.flush()
            os.fsync(target.fileno())
        if _sha_file(path) != before_sha:
            _refuse("write", "plan_stale", "post-write CAS failed", path)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _census(root: Path, excluded: Path) -> dict[str, tuple[object, ...]]:
    result: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        if path == excluded:
            continue
        info = path.lstat()
        relative = str(path.relative_to(root))
        if stat.S_ISDIR(info.st_mode):
            result[relative] = ("d", stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
        elif stat.S_ISREG(info.st_mode):
            result[relative] = ("f", stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, info.st_size, _sha_file(path))
        else:
            _refuse("census", "collateral_unsupported", "target contains unsupported object", path)
    return result


def _running_target(environ: Mapping[str, str], name: str, root_input: Path) -> Path:
    env_name = environ.get("SOLET_NAME", "").strip()
    app_raw = environ.get("APP_HOME", "").strip()
    if env_name != name or not app_raw:
        _refuse("running_identity", "running_identity_mismatch", "environment does not identify target", env_name)
    root = _directory(root_input, "running_identity", "running_identity_mismatch")
    app_home = _directory(Path(app_raw), "running_identity", "running_identity_mismatch")
    if app_home != root / "profile" or _manifest_name(root / "root_manifest.yaml") != name:
        _refuse("running_identity", "running_identity_mismatch", "manifest or APP_HOME differs", root)
    return root


def _manager_locations(environ: Mapping[str, str]) -> ManagerLocations:
    raw = environ.get("SOLET_HOME", "").strip()
    if raw:
        base = _directory(Path(raw), "manager_provenance", "manager_provenance_unavailable")
        return ManagerLocations(base / "config", base / "state")
    home_raw = environ.get("HOME", "").strip()
    if not home_raw:
        _refuse("manager_provenance", "manager_provenance_unavailable", "SOLET_HOME or HOME is required", "missing")
    home = _directory(Path(home_raw), "manager_provenance", "manager_provenance_unavailable")
    return ManagerLocations(home / ".config/solet", home / ".local/state/solet")


def _provenance(locations: ManagerLocations, name: str, root: Path) -> tuple[Path, str]:
    path = locations.state_dir / "transactions" / f"{name}.json"
    payload = _json(path, "manager_provenance", "manager_provenance_unavailable")
    target = payload.get("target")
    if payload.get("name") != name or not isinstance(target, str) or not payload.get("operation_id"):
        _refuse("manager_provenance", "manager_provenance_mismatch", "transaction identity is incomplete", path)
    if _directory(Path(target), "manager_provenance", "manager_provenance_mismatch") != root:
        _refuse("manager_provenance", "manager_provenance_mismatch", "transaction target differs", path)
    return path, _sha_file(path)


def _preserved(locations: ManagerLocations, target: Path) -> tuple[Path, str, tuple[Path, ...]]:
    config = locations.config_dir / "preserved_roots.json"
    if not config.exists():
        _refuse("preserved_roots", "preserved_roots_config_missing", "explicit config is required", config)
    payload = _json(config, "preserved_roots", "preserved_roots_config_malformed")
    if set(payload) != {"schema_version", "preserved_roots"} or payload.get("schema_version") != 1:
        _refuse("preserved_roots", "preserved_roots_config_malformed", "config schema is not exactly v1", config)
    roots = _roots(payload.get("preserved_roots"), config)
    _root_relations(roots, target)
    return config, _sha_file(config), roots


def _roots(value: object, config: Path) -> tuple[Path, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        _refuse("preserved_roots", "preserved_roots_config_malformed", "roots must be explicit strings", config)
    roots = tuple(_directory(Path(cast(str, item)), "preserved_roots", "preserved_roots_config_malformed") for item in value)
    if len(set(roots)) != len(roots):
        _refuse("preserved_roots", "preserved_roots_relation_ambiguous", "roots contain duplicates", config)
    return roots


def _root_relations(roots: tuple[Path, ...], target: Path) -> None:
    for index, left in enumerate(roots):
        if any(left in right.parents or right in left.parents for right in roots[index + 1:]):
            _refuse("preserved_roots", "preserved_roots_relation_ambiguous", "roots overlap", left)
        if left == target or left in target.parents:
            _refuse("preserved_roots", "subject_preserved", "target is preserved", left)
        if target in left.parents:
            _refuse("preserved_roots", "preserved_roots_relation_ambiguous", "target contains preserved root", left)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.geteuid():
        _refuse("plan_store", "plan_store_unsafe", "plan store has unsafe identity", path)
    path.chmod(0o700)


def _manifest_name(path: Path) -> str:
    lines = _regular(path, "running_identity", "running_identity_mismatch").read_text(encoding="utf-8").splitlines()
    names = [line.split(":", 1)[1].strip().strip("'\"") for line in lines if line.startswith("solet_name:")]
    if len(names) != 1 or not names[0]:
        _refuse("running_identity", "running_identity_mismatch", "manifest has no unique name", path)
    return names[0]


def _json(path: Path, guard: str, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(_regular(path, guard, reason).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _refuse(guard, reason, "expected readable JSON object", exc)
    if not isinstance(value, dict):
        _refuse(guard, reason, "expected JSON object", path)
    return value


def _directory(path: Path, guard: str, reason: str) -> Path:
    return _safe(path, guard, reason, True)


def _regular(path: Path, guard: str, reason: str) -> Path:
    return _safe(path, guard, reason, False)


def _safe(path: Path, guard: str, reason: str, directory: bool) -> Path:
    if not path.is_absolute() or path == Path("/"):
        _refuse(guard, reason, "path is unbounded", path)
    try:
        info = path.lstat()
    except OSError as exc:
        _refuse(guard, reason, "path is unavailable", exc)
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        _refuse(guard, reason, "path has unsafe type or owner", path)
    return path.resolve(strict=True)


def _identities(values: tuple[str, ...], reason: str) -> tuple[str, ...]:
    if not values or any(not item for item in values) or len(set(values)) != len(values):
        _refuse("identity", reason, "identities must be nonempty and unique", values)
    return tuple(values)


_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _valid_sha(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _sha_file(path: Path) -> str:
    return _sha(path.read_bytes())


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _refuse(guard: str, reason: str, message: str, evidence: object) -> NoReturn:
    raise ProtectionRefusedError(guard, reason, message, evidence)
