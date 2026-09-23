#!/usr/bin/env python3
"""Pure host-side contracts for a stage-resume Tart run.

The executable guest driver is deliberately separate.  This module has no
Tart side effect: it validates the one host-to-guest URL rewrite and constructs
the closed directory-share invocation that a caller may execute only after VM
resource custody has been granted.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

ADAPTERS = ("homebrew", "lm_studio", "postgresql", "launchd")
_METADATA_KEYS = frozenset(
    {
        "formula_revision",
        "install_mode",
        "manager_url",
        "manager_source_repository",
        "manager_source_ref",
        "manager_source_commit",
        "manager_source_tree_hash",
        "release_archive_sha256",
        "seed_repository",
        "seed_release_tag",
        "seed_commit",
        "seed_tree_hash",
        "seed_profile",
        "seed_channel_id",
        "seed_provenance",
        "existing_install_contract",
        "allowed_repository_migrations",
    }
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_VM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "pair_id",
        "stage_mount_tag",
        "stage_guest_root",
        "stage_read_only",
        "receipt_mount_tag",
        "receipt_guest_root",
        "origin_metadata_sha256",
        "guest_metadata_sha256",
        "payload_relative_path",
        "payload_sha256",
        "formula_relative_path",
        "formula_sha256",
        "formula_manager_url",
        "manager_source_commit",
    }
)


class ContractError(ValueError):
    """Raised when a transport or receipt boundary is not provable."""


def selected_adapters(selector: str) -> tuple[str, ...]:
    """Return the closed adapter selection used by one independent run."""

    if selector == "all":
        return ADAPTERS
    if selector in ADAPTERS:
        return (selector,)
    raise ContractError(f"--adapter must be one of all, {', '.join(ADAPTERS)}")


def guest_metadata(origin: dict[str, object], asset_name: str) -> dict[str, object]:
    """Derive the guest-visible metadata with exactly one permitted delta."""

    manager_url = _origin_manager_url(origin)
    _validate_asset_name(manager_url, asset_name)
    derived = dict(origin)
    derived["manager_url"] = f"file:///Volumes/solet-stage/origin/payload/{asset_name}"
    _validate_guest_metadata_delta(origin, derived)
    return derived


def _origin_manager_url(origin: dict[str, object]) -> str:
    if frozenset(origin) != _METADATA_KEYS:
        raise ContractError("origin release metadata has an unexpected key set")
    if origin.get("install_mode") != "dev":
        raise ContractError("stage-resume transport requires Item A dev-mode metadata")
    manager_url = origin.get("manager_url")
    if not isinstance(manager_url, str):
        raise ContractError("origin manager_url must be a string")
    parsed = urlsplit(manager_url)
    if parsed.scheme != "file" or parsed.netloc or not parsed.path.startswith("/"):
        raise ContractError("origin manager_url must be an absolute host-local file URL")
    return manager_url


def _validate_asset_name(manager_url: str, asset_name: str) -> None:
    parsed = urlsplit(manager_url)
    if Path(parsed.path).name != asset_name or "/" in asset_name or not asset_name:
        raise ContractError("asset name must exactly match the origin manager_url basename")


def _validate_guest_metadata_delta(
    origin: dict[str, object], derived: dict[str, object]
) -> None:
    changed = {key for key in origin if origin[key] != derived[key]}
    if changed != {"manager_url"}:
        raise AssertionError("guest metadata derivation changed an unauthorized field")


def write_guest_metadata(path: Path, metadata: dict[str, object]) -> None:
    """Write canonical transport metadata after the caller validated its source."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    """Return the prefixed SHA-256 digest of an existing regular file."""

    if not path.is_file():
        raise ContractError(f"expected a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def verify_artifact_digest(path: Path, expected: str) -> None:
    """Refuse receipt finalization when a captured artifact has drifted."""

    if _SHA256.fullmatch(expected) is None:
        raise ContractError("artifact digest must be sha256 followed by 64 lowercase hex digits")
    if sha256(path) != expected:
        raise ContractError(f"captured artifact digest differs from its receipt: {path.name}")


def transport_manifest(
    *,
    stage_root: Path,
    origin_metadata: Path,
    guest_metadata_path: Path,
    payload: Path,
    formula: Path,
    manager_source_commit: str,
) -> dict[str, object]:
    """Bind all guest-visible bytes before the stage share becomes read-only."""

    if not stage_root.is_absolute():
        raise ContractError("stage root must be absolute")
    if _GIT_COMMIT.fullmatch(manager_source_commit) is None:
        raise ContractError("manager source commit must be 40 lowercase hexadecimal characters")
    payload_relative = _stage_relative(stage_root, payload)
    formula_relative = _stage_relative(stage_root, formula)
    origin_relative = _stage_relative(stage_root, origin_metadata)
    guest_relative = _stage_relative(stage_root, guest_metadata_path)
    if not payload_relative.startswith("origin/payload/"):
        raise ContractError("payload must remain below stage/origin/payload")
    if formula_relative != "guest/Formula/solet.rb":
        raise ContractError("rendered Formula must be stage/guest/Formula/solet.rb")
    if origin_relative != "origin/release_metadata.json":
        raise ContractError("origin metadata must have its canonical stage location")
    if guest_relative != "guest/release_metadata.json":
        raise ContractError("guest metadata must have its canonical stage location")
    guest = _load_json_object(guest_metadata_path)
    manager_url = guest.get("manager_url")
    if not isinstance(manager_url, str) or not manager_url.endswith(f"/{payload.name}"):
        raise ContractError("guest metadata manager_url does not name the staged payload")
    if not formula.read_text(encoding="utf-8").find(manager_url) >= 0:
        raise ContractError("rendered Formula does not contain the guest-visible manager_url")
    return {
        "schema_version": 1,
        "pair_id": "stage-resume-vm-transport-20260914",
        "stage_mount_tag": "solet-stage",
        "stage_guest_root": "/Volumes/solet-stage",
        "stage_read_only": True,
        "receipt_mount_tag": "solet-receipts",
        "receipt_guest_root": "/Volumes/solet-receipts",
        "origin_metadata_sha256": sha256(origin_metadata),
        "guest_metadata_sha256": sha256(guest_metadata_path),
        "payload_relative_path": payload_relative,
        "payload_sha256": sha256(payload),
        "formula_relative_path": formula_relative,
        "formula_sha256": sha256(formula),
        "formula_manager_url": manager_url,
        "manager_source_commit": manager_source_commit,
    }


def write_transport_manifest(path: Path, manifest: dict[str, object]) -> None:
    """Persist only the exact closed v1 transport-manifest shape."""

    if frozenset(manifest) != _MANIFEST_KEYS:
        raise ContractError("transport manifest has an unexpected key set")
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _stage_relative(stage_root: Path, path: Path) -> str:
    try:
        relative = path.resolve(strict=True).relative_to(stage_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ContractError(f"staged artifact is not below the stage root: {path}") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ContractError("staged artifact path is not normalized")
    return relative.as_posix()


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"guest metadata cannot be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("guest metadata must be a JSON object")
    return value


def tart_run_argv(run_root: Path, vm_name: str) -> list[str]:
    """Return the only valid read-only-stage/writable-receipts Tart invocation."""

    if not run_root.is_absolute():
        raise ContractError("run root must be absolute")
    if _VM_NAME.fullmatch(vm_name) is None:
        raise ContractError("VM name has an invalid Tart identifier shape")
    stage = run_root / "stage"
    receipts = run_root / "receipts"
    if not stage.is_dir() or not receipts.is_dir():
        raise ContractError("run root must contain prepared stage/ and receipts/ directories")
    return [
        "tart",
        "run",
        "--no-graphics",
        "--suspendable",
        f"--dir={stage}:ro,tag=solet-stage",
        f"--dir={receipts}:tag=solet-receipts",
        vm_name,
    ]
