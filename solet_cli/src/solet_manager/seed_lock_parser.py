"""Closed seed-lock syntax and immutable-identity validation."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import SourceError

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TREE_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_CANONICAL_REPOSITORY = "https://github.com/solet-public/macos-bizops.git"
_CANONICAL_PROFILE = "macos-bizops"
_V1_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "repository",
        "release_tag",
        "commit",
        "tree_hash",
        "profile",
    }
)
_V2_REQUIRED_KEYS = _V1_REQUIRED_KEYS - {"release_tag"}
_OPTIONAL_KEYS = frozenset({"archive_sha256"})
_CHANNEL = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_V3_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "channel_id",
        "repository",
        "release_tag",
        "commit",
        "tree_hash",
        "archive_sha256",
        "profile",
        "provenance",
        "existing_install_contract",
        "allowed_repository_migrations",
    }
)
_PROVENANCE_KEYS = frozenset(
    {
        "schema_version",
        "provenance_sha256",
        "seed_id",
        "origin_id",
        "manifest_sha256",
        "bundle_name",
        "platform",
        "source_commit",
        "source_date",
    }
)
_CONTRACT_KEYS = frozenset({"flow_id", "flow_schema_version", "bundle_digest"})
_MIGRATION_KEYS = frozenset({"from_repository", "to_repository"})


@dataclass(frozen=True)
class SeedLockFields:
    repository: str
    release_tag: str | None
    commit: str
    tree_hash: str
    archive_sha256: str | None
    profile: str
    channel_id: str | None = None
    provenance: dict[str, object] | None = None
    existing_install_contract: dict[str, object] | None = None
    allowed_repository_migrations: tuple[dict[str, str], ...] = ()


def parse_seed_lock(path: Path) -> SeedLockFields:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise SourceError(f"seed lock is unreadable or invalid at {path}: {exc}") from exc
    return parse_seed_lock_bytes(raw_bytes)


def parse_seed_lock_bytes(raw_bytes: bytes) -> SeedLockFields:
    """Parse one closed seed-lock document already obtained by a caller.

    This is deliberately the byte-only half of :func:`parse_seed_lock`: callers
    which need tracked descriptor reads can retain ownership of that read rather
    than re-opening a path through the compatibility API.
    """
    raw = _parse_lock_bytes(raw_bytes)
    schema_version = raw.get("schema_version")
    if schema_version == 3:
        _validate_key_set(raw, _V3_REQUIRED_KEYS, 3)
        release_tag = _release_tag(raw)
        channel_id = _pattern_value(raw, "channel_id", _CHANNEL, "a valid channel identifier")
        provenance = _provenance(raw["provenance"])
        contract = _existing_install_contract(raw["existing_install_contract"])
        migrations = _migrations(raw["allowed_repository_migrations"], _repository(raw))
    elif schema_version == 2:
        _validate_key_set(raw, _V2_REQUIRED_KEYS, 2)
        release_tag = None
        channel_id = None
        provenance = None
        contract = None
        migrations = ()
    else:
        _validate_key_set(raw, _V1_REQUIRED_KEYS, 1)
        if schema_version != 1:
            raise SourceError("seed lock schema_version must be exactly 1 or 2")
        release_tag = _release_tag(raw)
        channel_id = None
        provenance = None
        contract = None
        migrations = ()
    repository = _repository(raw)
    commit = _pattern_value(raw, "commit", _COMMIT_PATTERN, "40 lowercase hexadecimal characters")
    tree_hash = _pattern_value(
        raw, "tree_hash", _TREE_PATTERN, "40 lowercase hexadecimal characters"
    )
    archive_sha256 = _optional_pattern_value(
        raw,
        "archive_sha256",
        _SHA256_PATTERN,
        "64 lowercase hexadecimal characters",
    )
    profile = _pattern_value(raw, "profile", _PROFILE_PATTERN, "a valid identifier")
    return SeedLockFields(
        repository,
        release_tag,
        commit,
        tree_hash,
        archive_sha256,
        profile,
        channel_id,
        provenance,
        contract,
        migrations,
    )


def _parse_lock_bytes(raw_bytes: bytes) -> dict[str, object]:
    try:
        raw: object = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"seed lock is unreadable or invalid: {exc}") from exc
    if not isinstance(raw, dict):
        raise SourceError("seed lock must contain one JSON object")
    mapping = cast(dict[object, object], raw)
    if not all(isinstance(key, str) for key in mapping):
        raise SourceError("seed lock must contain one JSON object")
    return cast(dict[str, object], mapping)


def _validate_key_set(
    raw: dict[str, object],
    required_keys: frozenset[str],
    schema_version: int,
) -> None:
    keys = frozenset(raw)
    allowed = required_keys | (_OPTIONAL_KEYS if schema_version in {1, 2} else frozenset())
    if required_keys <= keys <= allowed:
        return
    missing = sorted(required_keys - keys)
    unknown = sorted(
        keys - (required_keys | (_OPTIONAL_KEYS if schema_version in {1, 2} else frozenset()))
    )
    raise SourceError(
        f"seed lock fields differ from v{schema_version}; missing={missing}, unknown={unknown}"
    )


def _repository(raw: dict[str, object]) -> str:
    value = _string(raw, "repository")
    if not value.startswith("https://github.com/") or not value.endswith(".git"):
        raise SourceError("seed repository must be an explicit GitHub HTTPS .git URL")
    return value


def _release_tag(raw: dict[str, object]) -> str:
    value = _string(raw, "release_tag")
    if not _TAG_PATTERN.fullmatch(value) or value in {"main", "master", "HEAD"}:
        raise SourceError("release_tag must be an exact immutable tag name, not a moving ref")
    return value


def _pattern_value(
    raw: dict[str, object],
    key: str,
    pattern: re.Pattern[str],
    requirement: str,
) -> str:
    value = _string(raw, key)
    if not pattern.fullmatch(value):
        raise SourceError(f"seed {key} must be exactly {requirement}")
    return value


def _optional_pattern_value(
    raw: dict[str, object],
    key: str,
    pattern: re.Pattern[str],
    requirement: str,
) -> str | None:
    if key not in raw:
        return None
    return _pattern_value(raw, key, pattern, requirement)


def _string(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise SourceError(f"seed lock {key} must be a non-empty string")
    return value


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SourceError(f"seed lock {label} must be an object")
    return cast(dict[str, object], value)


def _provenance(value: object) -> dict[str, object]:
    raw = _object(value, "provenance")
    _closed(raw, _PROVENANCE_KEYS, "provenance")
    if raw["schema_version"] != 1 or isinstance(raw["schema_version"], bool):
        raise SourceError("seed lock provenance.schema_version must be exactly 1")
    for key in ("provenance_sha256", "manifest_sha256"):
        _pattern_value(raw, key, _SHA256_PATTERN, "64 lowercase hexadecimal characters")
    for key in ("seed_id", "origin_id"):
        _canonical_uuid(_string(raw, key), f"provenance.{key}")
    _pattern_value(raw, "source_commit", _COMMIT_PATTERN, "40 lowercase hexadecimal characters")
    if _string(raw, "platform") != "local":
        raise SourceError("seed lock provenance.platform must be 'local'")
    if not _TAG_PATTERN.fullmatch(_string(raw, "bundle_name")):
        raise SourceError("seed lock provenance.bundle_name has invalid characters")
    _rfc3339(_string(raw, "source_date"), "provenance.source_date")
    return raw


def _existing_install_contract(value: object) -> dict[str, object]:
    raw = _object(value, "existing_install_contract")
    _closed(raw, _CONTRACT_KEYS, "existing_install_contract")
    if not _TAG_PATTERN.fullmatch(_string(raw, "flow_id")):
        raise SourceError("seed lock existing_install_contract.flow_id has invalid characters")
    version = raw["flow_schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise SourceError(
            "seed lock existing_install_contract.flow_schema_version must be positive"
        )
    digest = _string(raw, "bundle_digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SourceError(
            "seed lock existing_install_contract.bundle_digest must be a canonical sha256 digest"
        )
    return raw


def _migrations(value: object, repository: str) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        raise SourceError("seed lock allowed_repository_migrations must be an array")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        row = _object(item, f"allowed_repository_migrations[{index}]")
        _closed(row, _MIGRATION_KEYS, f"allowed_repository_migrations[{index}]")
        source = _repository({"repository": row["from_repository"]})
        target = _repository({"repository": row["to_repository"]})
        if target != repository or source == repository or source in seen:
            raise SourceError("seed lock has invalid or duplicate repository migration")
        seen.add(source)
        result.append({"from_repository": source, "to_repository": target})
    return tuple(result)


def _closed(raw: dict[str, object], expected: frozenset[str], label: str) -> None:
    if frozenset(raw) != expected:
        missing = sorted(expected - set(raw))
        unknown = sorted(set(raw) - expected)
        raise SourceError(f"seed lock {label} fields differ; missing={missing}, unknown={unknown}")


def _canonical_uuid(value: str, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise SourceError(f"seed lock {label} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise SourceError(f"seed lock {label} must be a canonical UUID")


def _rfc3339(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError(f"seed lock {label} must be RFC3339") from exc
    if parsed.tzinfo is None or not (value.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", value)):
        raise SourceError(f"seed lock {label} must have an explicit offset")
