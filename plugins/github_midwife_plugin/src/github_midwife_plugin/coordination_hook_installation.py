"""Atomic ownership-receipt construction for installed Claude hooks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

RECEIPT_SCHEMA: Final = "solet_coordination_installation_v1"
RECEIPT_RELATIVE_PATH: Final = Path("data/coordination-hooks/claude/installation.v1.json")
HOOK_FILENAMES: Final = (
    "coordination_owner.py",
    "heartbeat_report_alive.py",
    "rotation_due_watch.py",
    "wake_waiter.py",
)
_SHA256 = re.compile(r"[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReceiptSurface:
    kind: str
    hook_root: Path
    interpreter: Path
    manifest_path: Path
    reporter_generation: int = 5


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _strict_object(raw: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    payload = json.loads(
        raw,
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
    if not isinstance(payload, dict):
        raise ValueError("receipt root is not an object")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _regular_absolute(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file")
    return resolved


def _root(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{label} must be a directory")
    return resolved


def surface_payload(surface: ReceiptSurface) -> dict[str, object]:
    if surface.kind not in {"plugin_cache", "checkout", "release"}:
        raise ValueError("unsupported receipt surface kind")
    root = _root(surface.hook_root, "hook_root")
    interpreter = _regular_absolute(surface.interpreter, "interpreter")
    manifest = _regular_absolute(surface.manifest_path, "manifest_path")
    files = {filename: _sha256(_root_file(root, filename)) for filename in HOOK_FILENAMES}
    return {
        "kind": surface.kind,
        "hook_root": str(root),
        "interpreter": str(interpreter),
        "manifest_path": str(manifest),
        "manifest_sha256": _sha256(manifest),
        "reporter_generation": surface.reporter_generation,
        "files": files,
    }


def build_receipt(
    *, solet_name: str, app_home: Path, plugin_selector: str, default_hook_root: Path,
    surfaces: tuple[ReceiptSurface, ...], installation_id: str | None = None,
) -> dict[str, object]:
    if not solet_name or plugin_selector != f"coordination-hooks@{solet_name.replace('_', '-')}":
        raise ValueError("receipt selector does not name the selected solet")
    canonical_home = _root(app_home, "app_home")
    payloads = [surface_payload(surface) for surface in surfaces]
    if not payloads:
        raise ValueError("receipt requires at least one surface")
    roots = [str(payload["hook_root"]) for payload in payloads]
    if len(roots) != len(set(roots)):
        raise ValueError("receipt surface roots must be unique")
    default = _root(default_hook_root, "default_hook_root")
    if str(default) not in roots:
        raise ValueError("default hook root is not a receipt surface")
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "solet_name": solet_name,
        "app_home": str(canonical_home),
        "plugin_selector": plugin_selector,
        "installation_id": installation_id or str(uuid.uuid4()),
        "verified_at": datetime.now(UTC).isoformat(),
        "default_hook_root": str(default),
        "surfaces": payloads,
    }
    receipt["receipt_sha256"] = hashlib.sha256(_canonical_json(receipt)).hexdigest()
    return receipt


def receipt_path(app_home: Path) -> Path:
    return app_home / RECEIPT_RELATIVE_PATH


def publish_receipt(receipt: Mapping[str, object]) -> Path:
    app_home_value = receipt.get("app_home")
    if not isinstance(app_home_value, str):
        raise ValueError("receipt has no app_home")
    app_home = _root(Path(app_home_value), "app_home")
    path = receipt_path(app_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.with_name(path.name + ".previous")
    if path.exists():
        previous.write_bytes(path.read_bytes())
        with previous.open("rb") as handle:
            os.fsync(handle.fileno())
    encoded = _canonical_json(receipt)
    fd, temporary_name = tempfile.mkstemp(prefix=".installation.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        candidate = Path(temporary_name)
        parsed = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or parsed != receipt:
            raise ValueError("receipt temporary write did not round-trip")
        if not isinstance(parsed.get("receipt_sha256"), str) or parsed["receipt_sha256"] != hashlib.sha256(_canonical_json({key: value for key, value in parsed.items() if key != "receipt_sha256"})).hexdigest():
            raise ValueError("receipt temporary checksum did not round-trip")
        os.replace(candidate, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return path


def receipt_is_well_formed(path: Path) -> bool:
    try:
        payload = _strict_object(path.read_text(encoding="utf-8"))
        if payload.get("schema") != RECEIPT_SCHEMA:
            return False
        digest = payload.get("receipt_sha256")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            return False
        unsigned = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        return digest == hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _surface_matches_hook_root(surface: object, hook_root: Path) -> bool:
    if not isinstance(surface, dict) or surface.get("hook_root") != str(hook_root):
        return False
    manifest = _regular_absolute(hook_root / "hooks.json", "hooks.json")
    if surface.get("manifest_sha256") != _sha256(manifest):
        return False
    files = surface.get("files")
    return isinstance(files, dict) and all(
        files.get(filename) == _sha256(_regular_absolute(hook_root / filename, filename))
        for filename in HOOK_FILENAMES
    )


def hook_root_matches_expected(actual_root: Path, expected_root: Path) -> bool:
    """Require cache hooks and their manifest to be exact intended source bytes."""
    try:
        actual = _root(actual_root, "actual hook_root")
        expected = _root(expected_root, "expected hook_root")
        return all(
            _sha256(_root_file(actual, name)) == _sha256(_root_file(expected, name))
            for name in (*HOOK_FILENAMES, "hooks.json")
        )
    except (OSError, ValueError):
        return False


def _root_file(root: Path, name: str) -> Path:
    candidate = _regular_absolute(root / name, name)
    candidate.relative_to(root)
    return candidate


def receipt_matches_hook_root(
    path: Path,
    hook_root: Path,
    *,
    solet_name: str,
    app_home: Path,
    plugin_selector: str,
    interpreter: Path,
) -> bool:
    """Check receipt ownership, selected bytes, and interpreter binding."""
    if not receipt_is_well_formed(path):
        return False
    try:
        payload = _strict_object(path.read_text(encoding="utf-8"))
        expected_home = _root(app_home, "app_home")
        if not _receipt_owner_matches(
            payload, path, solet_name, expected_home, plugin_selector,
        ):
            return False
        surfaces = payload.get("surfaces")
        if not isinstance(surfaces, list) or not _full_receipt_contract(payload, surfaces):
            return False
        selected = _root(hook_root, "hook_root")
        return _selected_surface_matches(surfaces, selected, interpreter)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _full_receipt_contract(payload: Mapping[str, object], surfaces: list[object]) -> bool:
    roots: set[Path] = set()
    for surface in surfaces:
        if not _surface_contract_valid(surface, roots):
            return False
    default = payload.get("default_hook_root")
    return isinstance(default, str) and any(str(root) == default for root in roots)


def _surface_contract_valid(surface: object, roots: set[Path]) -> bool:
    if not isinstance(surface, dict) or surface.get("kind") not in {"plugin_cache", "checkout", "release"}:
        return False
    if surface.get("reporter_generation") != 5:
        return False
    root = _root(Path(str(surface.get("hook_root", ""))), "surface hook_root")
    if root in roots:
        return False
    roots.add(root)
    manifest = _regular_absolute(Path(str(surface.get("manifest_path", ""))), "surface manifest")
    interpreter = _regular_absolute(Path(str(surface.get("interpreter", ""))), "surface interpreter")
    if surface.get("manifest_sha256") != _sha256(manifest) or not interpreter.is_file():
        return False
    files = surface.get("files")
    return isinstance(files, dict) and set(files) == set(HOOK_FILENAMES) and all(
        files.get(name) == _sha256(_root_file(root, name)) for name in HOOK_FILENAMES
    )


def _receipt_owner_matches(
    payload: Mapping[str, object],
    path: Path,
    solet_name: str,
    app_home: Path,
    plugin_selector: str,
) -> bool:
    values = (
        payload.get("schema") == RECEIPT_SCHEMA,
        payload.get("solet_name") == solet_name,
        payload.get("app_home") == str(app_home),
        payload.get("plugin_selector") == plugin_selector,
        path.resolve(strict=True) == receipt_path(app_home).resolve(strict=False),
    )
    return all(values)


def _selected_surface_matches(
    surfaces: list[object],
    selected: Path,
    interpreter: Path,
) -> bool:
    expected_interpreter = _regular_absolute(interpreter, "interpreter")
    return any(
        _surface_matches_hook_root(surface, selected)
        and isinstance(surface, dict)
        and surface.get("interpreter") == str(expected_interpreter)
        for surface in surfaces
    )
