#!/usr/bin/env python3
"""Verify that a coordination hook belongs to this installation.

This module is deliberately stdlib-only because it runs from an installed
Claude plugin, before the hook is allowed to create a marker or call the
bridge.  A receipt is an integrity record, not an authority grant: every
dynamic value remains bound to the launch environment and recorded bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

_RECEIPT_ENV = "AGENT_COORDINATION_RECEIPT_PATH"
_GAUGE_ENV = "AGENT_CONTEXT_GAUGE_REPORTER_PATH"
_SCHEMA = "solet_coordination_installation_v1"
_RECEIPT_RELATIVE = Path("data/coordination-hooks/claude/installation.v1.json")
_HASH = re.compile(r"[0-9a-f]{64}$")
_CAPABILITIES = {
    "heartbeat": "heartbeat_report_alive.py",
    "context_watch": "rotation_due_watch.py",
    "wake": "wake_waiter.py",
}
_REQUIRED_FILES = frozenset({"coordination_owner.py", *_CAPABILITIES.values()})


@dataclass(frozen=True)
class OwnershipResult:
    eligible: bool
    managed: bool
    diagnostic: str | None = None
    marker_root: Path | None = None


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value!r}")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_receipt(path: Path) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("receipt root is not an object")
    return value


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def receipt_sha256(receipt: Mapping[str, object]) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _absolute_file(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field} is not absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{field} is not a regular file")
    return resolved


def _absolute_directory(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field} is not absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{field} is not a directory")
    return resolved


def _digest_matches(path: Path, expected: object, field: str) -> None:
    if not isinstance(expected, str) or _HASH.fullmatch(expected) is None:
        raise ValueError(f"{field} is not a lowercase SHA-256")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"{field} drifted")


def _managed(environ: Mapping[str, str]) -> bool:
    # A partial launch identity is never authority to invoke a coordination
    # bridge, watcher, or marker.  Only a completely unarmed process may take
    # the inert path; every supplied coordination hint must prove ownership.
    return any(environ.get(name, "").strip() for name in (
        "AGENT_INSTANCE_ID",
        "AGENT_SESSION_ID",
        "AGENT_WAKE_CLI",
        _RECEIPT_ENV,
        "SOLET_NAME",
        _GAUGE_ENV,
    ))


def _verify_receipt_path(environ: Mapping[str, str]) -> tuple[dict[str, object], Path, Path]:
    raw_path = environ.get(_RECEIPT_ENV, "").strip()
    if not raw_path:
        raise ValueError(f"{_RECEIPT_ENV} is missing")
    receipt_path = _absolute_file(raw_path, _RECEIPT_ENV)
    receipt = _load_receipt(receipt_path)
    app_home = _absolute_directory(receipt.get("app_home"), "app_home")
    expected = (app_home / _RECEIPT_RELATIVE).resolve(strict=False)
    if receipt_path != expected:
        raise ValueError("receipt path is not beneath declared app_home at the fixed location")
    return receipt, receipt_path, app_home


def _validate_surface_files(surface: dict[str, object], root: Path) -> None:
    files = surface.get("files")
    if not isinstance(files, dict):
        raise ValueError("receipt surface files are not an object")
    file_names = {key for key in files if isinstance(key, str)}
    if len(file_names) != len(files) or file_names.symmetric_difference(_REQUIRED_FILES):
        raise ValueError("receipt surface file set is incomplete")
    for filename in _REQUIRED_FILES:
        hook = _contained_file(root, root / filename, f"surface.files.{filename}")
        _digest_matches(hook, files.get(filename), f"surface.files.{filename}")


def _contained_file(root: Path, candidate: Path, field: str) -> Path:
    resolved = _absolute_file(str(candidate), field)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field} escapes receipt root") from exc
    return resolved


def _validate_surface(surface: object, selected_root: Path) -> None:
    if not isinstance(surface, dict):
        raise ValueError("receipt surface is not an object")
    if surface.get("kind") not in {"plugin_cache", "checkout", "release"}:
        raise ValueError("receipt surface kind is unsupported")
    root = _absolute_directory(surface.get("hook_root"), "surface.hook_root")
    if root != selected_root:
        raise ValueError("selected root is not the receipt surface root")
    manifest = _absolute_file(surface.get("manifest_path"), "surface.manifest_path")
    _absolute_file(surface.get("interpreter"), "surface.interpreter")
    _digest_matches(manifest, surface.get("manifest_sha256"), "surface.manifest_sha256")
    generation = surface.get("reporter_generation")
    if generation != 5:
        raise ValueError("receipt reporter_generation is unsupported")
    _validate_surface_files(surface, root)


def _validate_owner_receipt(receipt: dict[str, object], environ: Mapping[str, str]) -> None:
    solet = environ.get("SOLET_NAME", "").strip()
    if receipt.get("schema") != _SCHEMA:
        raise ValueError("unsupported receipt schema")
    if not solet or receipt.get("solet_name") != solet:
        raise ValueError("SOLET_NAME does not match receipt owner")
    if receipt.get("plugin_selector") != f"coordination-hooks@{solet.replace('_', '-')}":
        raise ValueError("receipt plugin selector does not match owner")
    recorded_hash = receipt.get("receipt_sha256")
    if not isinstance(recorded_hash, str) or _HASH.fullmatch(recorded_hash) is None:
        raise ValueError("receipt checksum is malformed")
    if recorded_hash != receipt_sha256(receipt):
        raise ValueError("receipt checksum is invalid")


def _selected_root(receipt: dict[str, object], environ: Mapping[str, str]) -> Path:
    designated = environ.get(_GAUGE_ENV, "").strip()
    if not designated:
        return _absolute_directory(receipt.get("default_hook_root"), "default_hook_root")
    reporter = _absolute_file(designated, _GAUGE_ENV)
    if reporter.name != _CAPABILITIES["context_watch"]:
        raise ValueError("designated reporter basename is invalid")
    return reporter.parent


def _matching_surface(receipt: dict[str, object], selected_root: Path) -> dict[str, object]:
    surfaces = receipt.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        raise ValueError("receipt has no surfaces")
    roots: set[Path] = set()
    matching: dict[str, object] | None = None
    for surface in surfaces:
        if not isinstance(surface, dict):
            raise ValueError("receipt surface is not an object")
        root = _absolute_directory(surface.get("hook_root"), "surface.hook_root")
        if root in roots:
            raise ValueError("receipt has duplicate surface roots")
        roots.add(root)
        if root == selected_root:
            matching = surface
    if matching is None:
        raise ValueError("selected root is not recorded in receipt")
    return matching


def _verify_execution(capability: str, executing_file: str, selected_root: Path) -> None:
    expected = (selected_root / _CAPABILITIES[capability]).resolve(strict=True)
    if Path(executing_file).resolve(strict=True) != expected:
        raise ValueError("executing hook is not the selected owner-qualified capability")


def verify(capability: str, executing_file: str, environ: Mapping[str, str] | None = None) -> OwnershipResult:
    """Return eligibility without writing a marker or invoking a bridge."""
    env = os.environ if environ is None else environ
    if capability not in _CAPABILITIES:
        return OwnershipResult(False, True, "coordination ownership: unknown capability")
    if not _managed(env):
        return OwnershipResult(False, False)
    if not env.get("AGENT_INSTANCE_ID", "").strip() or not env.get("AGENT_SESSION_ID", "").strip():
        return OwnershipResult(False, True, "coordination ownership: managed hook lacks instance/session identity")
    try:
        receipt, _receipt_path, app_home = _verify_receipt_path(env)
        _validate_owner_receipt(receipt, env)
        selected_root = _selected_root(receipt, env)
        _validate_surface(_matching_surface(receipt, selected_root), selected_root)
        _verify_execution(capability, executing_file, selected_root)
        return OwnershipResult(True, True, marker_root=app_home / "data/coordination-hooks/claude/runtime")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return OwnershipResult(False, True, f"coordination ownership refusal: {exc}")


def runtime_identity_directory(result: OwnershipResult, environ: Mapping[str, str] | None = None) -> Path | None:
    """Return collision-free state root after successful ownership verification."""
    if not result.eligible or result.marker_root is None:
        return None
    env = os.environ if environ is None else environ
    parts = (env.get("SOLET_NAME", ""), env.get("AGENT_INSTANCE_ID", ""), env.get("AGENT_SESSION_ID", ""))
    if not all(parts):
        return None
    return result.marker_root.joinpath(*(hashlib.sha256(part.encode("utf-8")).hexdigest() for part in parts))


def report_refusal(result: OwnershipResult, *, stream: TextIO = sys.stderr) -> None:
    if result.managed and result.diagnostic:
        print(f"[coordination-owner] {result.diagnostic}", file=stream)
