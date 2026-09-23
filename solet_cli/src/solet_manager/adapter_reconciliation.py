"""Snapshot-only reconciliation of a target's frozen code surface.

The production cutover half is deliberately not present here.  Until a
sanctioned manager-to-target blue-green interface exists, callers may use this
manager only from the explicit snapshot-replay CLI mode.  That makes a useful
repair primitive testable without misrepresenting a disk refresh as a live
deployment.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import stat
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import ProbeDriftError, StateConflictError, StateError, VenvIncompatibleError
from .models import CommandResult, ExitCode, InstanceRecord, JsonValue
from .paths import ManagerPaths
from .reconciliation_ceremony import run_reconciliation_ceremony
from .registry import InstanceRegistry
from .release_lock import SeedLock, load_seed_lock
from .source_acquisition import materialize_locked_seed
from .state_io import atomic_remove, atomic_replace_bytes, atomic_write_json

_RECEIPT_KEYS = frozenset({"schema_version", "name", "target", "seed", "preview_fingerprint", "state", "files"})
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
_VENV_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class _Replacement:
    path: Path
    mode: int
    before: bytes | None
    after: bytes | None


@dataclass(frozen=True)
class _PreparedReconciliation:
    record: InstanceRecord
    seed: SeedLock
    files: tuple[_Replacement, ...]
    fingerprint: str


type PayloadMaterializer = Callable[[SeedLock, Path, ManagerPaths], Path]
type CompatibilityCheck = Callable[[Path, tuple[Path, ...]], None]


class AdapterReconciliationManager:
    """Refresh only frozen code, never contracts, journals, or live processes."""

    def __init__(
        self,
        *,
        paths: ManagerPaths,
        seed_lock_path: Path,
        payload_materializer: PayloadMaterializer | None = None,
        compatibility_check: CompatibilityCheck | None = None,
    ) -> None:
        self._paths = paths
        self._seed_lock_path = seed_lock_path
        self._payload_materializer = payload_materializer or _materialize_payload
        self._compatibility_check = compatibility_check or _verify_venv_compatibility
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
            recover=lambda: recover_adapter_reconciliation(self._paths, name),
            prepare=lambda: self._prepare(name),
            preview_result=lambda prepared, recovered: _preview_result(prepared, recovered=recovered),
            approval_required=_approval_required,
            drift_result=_drift_result,
            apply=self._apply,
            fingerprint=lambda prepared: prepared.fingerprint,
        )

    def _prepare(self, name: str) -> _PreparedReconciliation:
        record = self._registry.require(name)
        target = Path(record.target)
        _require_real_directory(target, "managed target")
        seed = load_seed_lock(self._seed_lock_path)
        payload = self._payload_materializer(seed, target, self._paths)
        _require_real_directory(payload, "locked-seed payload")
        source_files = _surface_files(payload)
        target_files = _surface_files(target)
        replacements = _replacements(target, source_files, target_files)
        self._compatibility_check(target, tuple(source_files.values()))
        fingerprint = _canonical_sha256(
            {
                "name": record.name,
                "target": str(target),
                "seed": seed.identity_dict(),
                "files": [_replacement_data(item) for item in replacements],
            }
        )
        return _PreparedReconciliation(record, seed, replacements, fingerprint)

    def _apply(self, prepared: _PreparedReconciliation) -> CommandResult:
        fresh = self._prepare(prepared.record.name)
        if fresh.fingerprint != prepared.fingerprint:
            raise ProbeDriftError(
                "adapter reconciliation state changed after preview approval; no mutation was performed",
                repair="Rerun reconcile-adapter --dry-run and review the new fingerprint.",
            )
        receipt_path = _receipt_path(self._paths, fresh.record.name)
        receipt = _receipt(fresh)
        atomic_write_json(receipt_path, receipt)
        for replacement in fresh.files:
            _apply_replacement(replacement)
        _verify_after_files(fresh.files)
        applied = dict(receipt)
        applied["state"] = "applied"
        atomic_write_json(receipt_path, applied)
        return CommandResult(
            kind="adapter_reconciliation",
            status="reconciled_not_yet_invokable",
            message=(f"Refreshed frozen code for snapshot {fresh.record.name!r}. No live cutover was attempted; this verb is not yet invocable for live targets."),
            exit_code=ExitCode.OK,
            data={
                "name": fresh.record.name,
                "target": fresh.record.target,
                "changed_files": sum(item.before != item.after for item in fresh.files),
                "approval_fingerprint_cleared": True,
                "live_cutover": "not_available_pending_iss_dd6c87e6",
            },
        )


def recover_adapter_reconciliation(paths: ManagerPaths, name: str) -> bool:
    """Recover an interrupted code refresh to authenticated before-images."""

    receipt_path = _receipt_path(paths, name)
    if not receipt_path.exists():
        return False
    raw = _load_receipt(receipt_path, name)
    if raw["state"] in {"applied", "recovered"}:
        return False
    files = _parse_receipt_files(raw)
    current = tuple(_current_sha256(item.path) for item in files)
    before = tuple(_hash(item.before) for item in files)
    after = tuple(_hash(item.after) for item in files)
    if current == after:
        terminal = dict(raw)
        terminal["state"] = "applied"
        atomic_write_json(receipt_path, terminal)
        return True
    if any(value not in {old, new} for value, old, new in zip(current, before, after, strict=True)):
        raise StateConflictError("adapter reconciliation recovery found bytes outside its authenticated before/after set")
    for replacement in files:
        _apply_replacement(_Replacement(replacement.path, replacement.mode, None, replacement.before))
    terminal = dict(raw)
    terminal["state"] = "recovered"
    atomic_write_json(receipt_path, terminal)
    return True


def _materialize_payload(seed: SeedLock, target: Path, paths: ManagerPaths) -> Path:
    del target
    root = paths.acquisition_dir / "adapter-reconciliations" / uuid.uuid4().hex
    payload = root / "payload"
    return materialize_locked_seed(seed, payload, cache_dir=paths.acquisition_dir)


def _surface_files(root: Path) -> dict[Path, Path]:
    roots = _surface_roots(root)
    files: dict[Path, Path] = {}
    for surface_root in roots:
        for path in _regular_files(surface_root):
            relative = path.relative_to(root)
            if relative in files:
                raise StateConflictError(f"duplicate frozen code path: {relative}")
            files[relative] = path
    return files


def _surface_roots(root: Path) -> tuple[Path, ...]:
    plugin_root = root / "plugins"
    roots: list[Path] = []
    if plugin_root.exists():
        _require_real_directory(plugin_root, "plugins root")
        for plugin in sorted(plugin_root.iterdir(), key=lambda item: item.name):
            if plugin.is_symlink() or not plugin.is_dir():
                raise StateConflictError(f"plugin root contains a non-directory entry: {plugin}")
            source = plugin / "src"
            if source.exists():
                _require_real_directory(source, "plugin source root")
                roots.append(source)
    contracts = root / "solet_setup_contracts" / "src"
    if contracts.exists():
        _require_real_directory(contracts, "solet_setup_contracts source root")
        roots.append(contracts)
    if not roots:
        raise StateConflictError("target has no frozen plugin or solet_setup_contracts code surface")
    return tuple(roots)


def _regular_files(root: Path) -> Iterable[Path]:
    for directory, directories, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for child in directories:
            candidate = current / child
            if candidate.is_symlink():
                raise StateConflictError(f"frozen code surface contains a symlink: {candidate}")
        for filename in filenames:
            candidate = current / filename
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise StateConflictError(f"frozen code surface contains a non-regular file: {candidate}")
            yield candidate


def _replacements(
    target: Path,
    source_files: dict[Path, Path],
    target_files: dict[Path, Path],
) -> tuple[_Replacement, ...]:
    replacements: list[_Replacement] = []
    for relative in sorted(source_files.keys() | target_files.keys()):
        source = source_files.get(relative)
        existing = target_files.get(relative)
        before = None if existing is None else existing.read_bytes()
        after = None if source is None else source.read_bytes()
        mode_path = source or existing
        if mode_path is None:
            raise AssertionError("replacement path disappeared from both surfaces")
        mode = stat.S_IMODE(mode_path.stat().st_mode)
        replacements.append(_Replacement(target / relative, mode, before, after))
    return tuple(replacements)


def _verify_venv_compatibility(target: Path, source_files: tuple[Path, ...]) -> None:
    interpreter = _target_interpreter(target)
    roots = sorted(_external_import_roots(source_files))
    script = "import importlib.util, json, sys\nmissing = [name for name in json.loads(sys.argv[1]) if importlib.util.find_spec(name) is None]\nprint(json.dumps(missing))\n"
    try:
        completed = subprocess.run(
            [str(interpreter), "-c", script, json.dumps(roots)],
            capture_output=True,
            check=False,
            text=True,
            timeout=_VENV_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateConflictError(f"target venv compatibility probe could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-500:]
        raise StateConflictError(f"target venv compatibility probe failed: {detail}")
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise StateConflictError("target venv compatibility probe returned malformed JSON") from exc
    if not isinstance(parsed, list):
        raise StateConflictError("target venv compatibility probe returned an invalid result")
    missing: list[str] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, str):
            raise StateConflictError("target venv compatibility probe returned an invalid result")
        missing.append(item)
    if missing:
        raise VenvIncompatibleError(
            "target venv lacks imports required by the locked-seed code: " + ", ".join(missing),
            repair=("Use the bounded 1.35 dependency-closure remedy; do not install packages or modify the target venv manually."),
        )


def _target_interpreter(target: Path) -> Path:
    for candidate in (target / ".venv" / "bin" / "python3", target / "venv" / "bin" / "python3"):
        if candidate.is_file():
            return candidate
    raise StateConflictError("target has no real .venv/bin/python3 or venv/bin/python3 interpreter")


def _external_import_roots(source_files: tuple[Path, ...]) -> set[str]:
    internal = _internal_import_roots(source_files)
    roots: set[str] = set()
    for source in source_files:
        if source.suffix != ".py":
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise StateConflictError(f"locked-seed Python source cannot be parsed: {source}: {exc}") from exc
        for node in ast.walk(tree):
            roots.update(name for name in _import_roots(node) if name not in internal)
    return roots


def _internal_import_roots(source_files: tuple[Path, ...]) -> set[str]:
    roots: set[str] = set()
    for path in source_files:
        parents = path.parents
        for parent in parents:
            if parent.name == "src" and parent.parent.name in {"plugins", "solet_setup_contracts"}:
                relative = path.relative_to(parent)
                if relative.parts:
                    roots.add(relative.parts[0])
                break
    return roots


def _import_roots(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name.partition(".")[0] for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
        return (node.module.partition(".")[0],)
    return ()


def _apply_replacement(replacement: _Replacement) -> None:
    if replacement.after is None:
        atomic_remove(replacement.path)
        return
    _ensure_parent(replacement.path.parent)
    atomic_replace_bytes(replacement.path, replacement.after, mode=replacement.mode)


def _ensure_parent(path: Path) -> None:
    pending: list[Path] = []
    cursor = path
    while not cursor.exists():
        pending.append(cursor)
        cursor = cursor.parent
    _require_real_directory(cursor, "target code parent")
    for directory in reversed(pending):
        directory.mkdir(mode=0o755)
        _require_real_directory(directory, "created target code parent")


def _verify_after_files(files: tuple[_Replacement, ...]) -> None:
    for replacement in files:
        if _current_sha256(replacement.path) != _hash(replacement.after):
            raise StateError(f"adapter reconciliation postcondition failed: {replacement.path}")


def _receipt(prepared: _PreparedReconciliation) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "name": prepared.record.name,
        "target": prepared.record.target,
        "seed": prepared.seed.identity_dict(),
        "preview_fingerprint": prepared.fingerprint,
        "state": "prepared",
        "files": [_receipt_file(item) for item in prepared.files],
    }


def _receipt_file(replacement: _Replacement) -> dict[str, JsonValue]:
    return {
        "path": str(replacement.path),
        "mode": replacement.mode,
        "before_sha256": _hash(replacement.before),
        "before_base64": _encode(replacement.before),
        "after_sha256": _hash(replacement.after),
        "after_base64": _encode(replacement.after),
    }


def _load_receipt(path: Path, name: str) -> dict[str, JsonValue]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(f"adapter reconciliation receipt is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise StateError("adapter reconciliation receipt does not match the closed v1 schema")
    receipt = cast(dict[str, JsonValue], raw)
    if frozenset(receipt) != _RECEIPT_KEYS:
        raise StateError("adapter reconciliation receipt does not match the closed v1 schema")
    if receipt.get("schema_version") != 1 or receipt.get("name") != name:
        raise StateError("adapter reconciliation receipt identity is invalid")
    if not isinstance(receipt.get("target"), str) or receipt.get("state") not in _RECEIPT_STATES:
        raise StateError("adapter reconciliation receipt state is invalid")
    if not isinstance(receipt.get("seed"), dict) or not isinstance(receipt.get("files"), list):
        raise StateError("adapter reconciliation receipt payload is invalid")
    return receipt


def _parse_receipt_files(raw: dict[str, JsonValue]) -> tuple[_Replacement, ...]:
    values = raw["files"]
    if not isinstance(values, list) or not values:
        raise StateError("adapter reconciliation receipt files are invalid")
    files = tuple(_parse_receipt_file(value) for value in values)
    if len({item.path for item in files}) != len(files):
        raise StateError("adapter reconciliation receipt has duplicate file paths")
    return files


def _parse_receipt_file(value: JsonValue) -> _Replacement:
    if not isinstance(value, dict) or frozenset(value) != _RECEIPT_FILE_KEYS:
        raise StateError("adapter reconciliation receipt file does not match the closed v1 schema")
    path = value.get("path")
    mode = value.get("mode")
    if not isinstance(path, str) or not Path(path).is_absolute() or isinstance(mode, bool) or not isinstance(mode, int):
        raise StateError("adapter reconciliation receipt file identity is invalid")
    before = _decode(value.get("before_base64"))
    after = _decode(value.get("after_base64"))
    if value.get("before_sha256") != _hash(before) or value.get("after_sha256") != _hash(after):
        raise StateError("adapter reconciliation receipt hashes are invalid")
    return _Replacement(Path(path), mode, before, after)


def _preview_result(prepared: _PreparedReconciliation, *, recovered: bool) -> CommandResult:
    changed = [item for item in prepared.files if item.before != item.after]
    return CommandResult(
        kind="adapter_reconciliation_preview",
        status="preview_ready",
        message=(f"Preview frozen-code reconciliation for snapshot {prepared.record.name!r}. A live cutover is unavailable and will not be attempted."),
        exit_code=ExitCode.OK,
        data={
            "name": prepared.record.name,
            "target": prepared.record.target,
            "seed": prepared.seed.identity_dict(),
            "changed_files": [str(item.path) for item in changed],
            "approval_fingerprint": prepared.fingerprint,
            "recovery_performed": recovered,
            "dry_run_writes": 0,
            "live_cutover": "not_available_pending_iss_dd6c87e6",
        },
    )


def _approval_required(preview: CommandResult) -> CommandResult:
    return CommandResult(
        kind="adapter_reconciliation_preview",
        status="awaiting_user",
        message="Exact code-refresh preview rendered; pass --yes with its fingerprint to apply it.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="approval_required",
        repair="Review this snapshot-only result, then rerun with --yes and --approval-fingerprint.",
        data={key: value for key, value in preview.data.items() if key != "approval_fingerprint"},
    )


def _drift_result(preview: CommandResult) -> CommandResult:
    return CommandResult(
        kind="adapter_reconciliation_preview",
        status="awaiting_user",
        message="The supplied approval fingerprint does not match the fresh code-refresh preview.",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="probe_drift",
        repair="Rerun --dry-run, review the changed reconciliation, and use its new fingerprint.",
        data={key: value for key, value in preview.data.items() if key != "approval_fingerprint"},
    )


def _replacement_data(replacement: _Replacement) -> dict[str, JsonValue]:
    return {
        "path": str(replacement.path),
        "before_sha256": _hash(replacement.before),
        "after_sha256": _hash(replacement.after),
    }


def _receipt_path(paths: ManagerPaths, name: str) -> Path:
    return paths.adapter_reconciliations_dir / f"{name}.json"


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise StateConflictError(f"{label} is not a real directory: {path}")


def _current_sha256(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StateConflictError(f"reconciliation target is not a regular file: {path}")
    return _hash(path.read_bytes())


def _hash(value: bytes | None) -> str | None:
    return None if value is None else hashlib.sha256(value).hexdigest()


def _encode(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode("ascii")


def _decode(value: JsonValue) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError("adapter reconciliation receipt bytes are invalid")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise StateError("adapter reconciliation receipt bytes are malformed") from exc


def _canonical_sha256(value: JsonValue) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
