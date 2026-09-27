#!/usr/bin/env python3
"""Render the immutable formula and seed lock from reviewed release metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import SplitResult, urlsplit

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_ID = re.compile(r"^[0-9a-f]{40}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROFILE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_GITHUB_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_RELEASE_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.(?:tar\.gz|tar\.bz2|tar\.xz|zip)$")
_VERSION_COMPONENT = re.compile(r"(?:^|[._-])v?\d+(?:\.\d+)+(?:[._-]|$)", re.IGNORECASE)
_MOVING_RELEASE_NAMES = frozenset({"head", "main", "master", "latest"})
_V3_PROVENANCE_KEYS = frozenset(
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
_V3_CONTRACT_KEYS = frozenset({"flow_id", "flow_schema_version", "bundle_digest"})
_MIGRATION_KEYS = frozenset({"from_repository", "to_repository"})
# The exact closed key set of a schema-v1 release manifest (design §7.1),
# duplicated here rather than imported: this script ships in the manager
# payload and stays dependency-free, same as its own metadata validators
# below. `seed_factory_plugin.release_manifest` and
# `solet_manager.release_identity` each hold their own copy for the same
# reason; the three must be kept in agreement by hand.
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "release_label",
        "manager_release_tag",
        "seed",
        "components",
        "bundle_verdict",
        "guest_validation",
        "manager",
        "tap",
        "surface_digests",
        "produced_by",
        "factory_signature",
    }
)
_REQUIRED = frozenset(
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


@dataclass(frozen=True)
class _RepositoryIdentity:
    owner: str
    repository: str


def main() -> int:
    args = _parser().parse_args()
    metadata = _load_metadata(args.metadata)
    manifest = _load_manifest(args.manifest)
    substitutions = {
        "INSTALL_MODE": metadata["install_mode"],
        "MANAGER_URL": metadata["manager_url"],
        "MANAGER_SHA256": metadata["release_archive_sha256"],
        "MANAGER_SOURCE_COMMIT": metadata["manager_source_commit"],
        "REVISION_LINE": _revision_line(metadata["formula_revision"]),
        "SEED_REPOSITORY": metadata["seed_repository"],
        "SEED_RELEASE_TAG": metadata["seed_release_tag"],
        "SEED_COMMIT": metadata["seed_commit"],
        "SEED_TREE_HASH": metadata["seed_tree_hash"],
        "SEED_ARCHIVE_SHA256": metadata["release_archive_sha256"],
        "SEED_PROFILE": metadata["seed_profile"],
        "SEED_CHANNEL_ID": metadata["seed_channel_id"],
        "SEED_PROVENANCE": json.dumps(
            metadata["seed_provenance"], separators=(",", ":"), sort_keys=True
        ),
        "EXISTING_INSTALL_CONTRACT": json.dumps(
            metadata["existing_install_contract"], separators=(",", ":"), sort_keys=True
        ),
        "ALLOWED_REPOSITORY_MIGRATIONS": json.dumps(
            metadata["allowed_repository_migrations"], separators=(",", ":"), sort_keys=True
        ),
        # iss_18c47206: the consumption-side pairing gate
        # (`solet_manager.release_identity_gate`) reads its manifest from the
        # KEG (`sys.prefix/share/solet/release_manifest.json`), never from a
        # release asset -- nothing previously installed one there, so the
        # stage-5 `allow_manager_seed_skew` escape hatch never reached the
        # gate. Ship the same stage-5 draft `stage_release.py` already writes
        # beside the Formula; its `seed`/`manager` sections are exactly what
        # the gate compares, and the sections a later `publish_release` stage
        # owns (`tap`, `surface_digests`, ...) are already `null` in the draft.
        "RELEASE_MANIFEST_JSON": json.dumps(manifest, separators=(",", ":"), sort_keys=True),
    }
    root = Path(__file__).resolve().parents[1]
    lock_destination = args.output_root / "solet_cli" / "homebrew" / "seed.lock.json"
    _render(root / "seed.lock.json.template", lock_destination, substitutions)
    # existing_install_inspection_seed_lock_catalog.v1.json's seed_lock_sha256
    # must equal sha256 of the lock exactly as it will actually ship (which
    # embeds this release's own payload digest), so it can only be computed
    # AFTER the lock above is rendered -- never baked into wheel package data
    # at manager-source-commit time (iss_42749563: that would need the wheel's
    # own bytes to already contain a hash of a lock that itself hashes the
    # wheel, which no release could ever satisfy).
    substitutions["EXISTING_INSTALL_SEED_LOCK_SHA256"] = hashlib.sha256(
        lock_destination.read_bytes()
    ).hexdigest()
    _render(
        root / "Formula" / "solet.rb.template",
        args.output_root / "Formula" / "solet.rb",
        substitutions,
    )
    # The Formula's ``seeds`` symlink exposes this tap catalog to
    # ``solet --seed <profile>`` and ``solet list-seeds``.  Keep the bundled
    # default lock above unchanged, but render the same reviewed lock under
    # the profile-derived directory the manager resolves.
    _render(
        root / "seed.lock.json.template",
        (
            args.output_root
            / "solet_cli"
            / "homebrew"
            / "seeds"
            / _require_string(metadata, "seed_profile")
            / "seed.lock.json"
        ),
        substitutions,
    )
    _verify_existing_install_seed_lock_catalog(args.output_root, lock_destination)
    return 0


def _verify_existing_install_seed_lock_catalog(output_root: Path, lock_destination: Path) -> None:
    """Refuse the release if the rendered Formula's catalog entry and the
    rendered lock it must match ever disagree (design ask, iss_42749563):
    re-derive both independently from what actually landed on disk rather
    than trusting the substitution dict this process already built."""
    expected = hashlib.sha256(lock_destination.read_bytes()).hexdigest()
    formula_text = (output_root / "Formula" / "solet.rb").read_text(encoding="utf-8")
    match = re.search(r'"seed_lock_sha256":\s*"([0-9a-f]{64})"', formula_text)
    if match is None:
        raise SystemExit(
            "rendered Formula is missing the existing-install seed lock catalog entry"
        )
    if match.group(1) != expected:
        raise SystemExit(
            f"rendered Formula's existing-install catalog seed_lock_sha256 {match.group(1)!r} "
            f"does not match sha256 of the rendered seed.lock.json {expected!r}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"release manifest is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("release manifest must be a JSON object")
    manifest = cast(dict[str, object], raw)
    if frozenset(manifest) != _MANIFEST_KEYS:
        raise SystemExit("release manifest must contain exactly " + ", ".join(sorted(_MANIFEST_KEYS)))
    if not isinstance(manifest.get("manager"), dict) or not isinstance(manifest.get("seed"), dict):
        raise SystemExit("release manifest must carry closed 'manager' and 'seed' sections")
    return manifest


def _load_metadata(path: Path) -> dict[str, object]:
    raw = _read_metadata(path)
    _require_exact_keys(raw)
    _require_int(raw, "formula_revision")
    _require_seed_repository(raw)
    _require_release_tag(raw)
    _require_install_source(raw)
    _require_repository(raw, "manager_source_repository")
    _require_pattern(raw, "manager_source_ref", _GIT_ID)
    _require_pattern(raw, "release_archive_sha256", _SHA256)
    _require_pattern(raw, "manager_source_commit", _GIT_ID)
    _require_pattern(raw, "manager_source_tree_hash", _GIT_ID)
    _require_pattern(raw, "seed_commit", _GIT_ID)
    _require_pattern(raw, "seed_tree_hash", _GIT_ID)
    _require_pattern(raw, "seed_profile", _PROFILE)
    _require_pattern(raw, "seed_channel_id", re.compile(r"^[a-z][a-z0-9_-]{1,63}$"))
    _require_v3_descriptor(raw)
    return raw


def _read_metadata(path: Path) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"release metadata is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("release metadata must be a JSON object")
    return cast(dict[str, object], raw)


def _require_exact_keys(raw: dict[str, object]) -> None:
    if frozenset(str(key) for key in raw) != _REQUIRED:
        raise SystemExit("release metadata must contain exactly " + ", ".join(sorted(_REQUIRED)))


def _require_seed_repository(raw: dict[str, object]) -> _RepositoryIdentity:
    return _require_repository(raw, "seed_repository")


def _require_repository(raw: dict[str, object], key: str) -> _RepositoryIdentity:
    parsed = _parse_github_url(_require_string(raw, key), key)
    parts = parsed.path.split("/")
    if len(parts) != 3 or not parts[2].endswith(".git"):
        raise SystemExit(f"{key} must be an explicit GitHub HTTPS .git URL")
    owner = parts[1]
    repository = parts[2][:-4]
    _require_repository_parts(owner, repository, key)
    return _RepositoryIdentity(owner, repository)


def _require_release_tag(raw: dict[str, object]) -> str:
    release_tag = _require_string(raw, "seed_release_tag")
    if _TAG.fullmatch(release_tag) is None or _is_moving_name(release_tag):
        raise SystemExit("seed_release_tag has an invalid immutable identity shape")
    return release_tag


def _require_install_source(raw: dict[str, object]) -> None:
    mode = _require_string(raw, "install_mode")
    url = _require_string(raw, "manager_url")
    if mode == "dev":
        _require_local_payload_url(url)
        return
    if mode != "release":
        raise SystemExit("install_mode must be exactly 'dev' or 'release'")
    _require_manager_release_url(url)


def _require_local_payload_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("dev manager_url must be an absolute, unambiguous file:// payload URL")


def _require_manager_release_url(value: str) -> None:
    parsed = _parse_github_url(value, "manager_url")
    parts = parsed.path.split("/")
    if len(parts) != 7 or parts[3:5] != ["releases", "download"]:
        raise SystemExit(
            "manager_url must be a GitHub uploaded release asset at "
            "/<owner>/<repo>/releases/download/<tag>/<versioned-asset>"
        )
    owner, repository_name, url_tag, asset = parts[1], parts[2], parts[5], parts[6]
    _require_repository_parts(owner, repository_name, "manager_url")
    if _TAG.fullmatch(url_tag) is None or _is_moving_name(url_tag):
        raise SystemExit("manager_url tag has an invalid immutable identity shape")
    if _RELEASE_ASSET.fullmatch(asset) is None or _VERSION_COMPONENT.search(asset) is None:
        raise SystemExit("manager_url asset must have a versioned archive filename")


def _parse_github_url(value: str, label: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SystemExit(f"{label} is not a valid URL: {exc}") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit(f"{label} must be an unambiguous GitHub HTTPS URL")
    return parsed


def _require_repository_parts(owner: str, repository: str, label: str) -> None:
    if _GITHUB_OWNER.fullmatch(owner) is None or _GITHUB_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit(f"{label} has an invalid GitHub owner/repository identity")


def _is_moving_name(value: str) -> bool:
    return value.casefold() in _MOVING_RELEASE_NAMES


def _require_string(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise SystemExit(f"{key} must be a non-empty string")
    return value


def _require_int(raw: dict[str, object], key: str) -> None:
    value = raw[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SystemExit(f"{key} must be a non-negative integer")


def _require_v3_descriptor(raw: dict[str, object]) -> None:
    _require_provenance_descriptor(_object(raw["seed_provenance"], "seed_provenance"))
    _require_existing_install_contract(
        _object(raw["existing_install_contract"], "existing_install_contract")
    )
    _require_repository_migrations(
        raw["allowed_repository_migrations"], _require_string(raw, "seed_repository")
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be closed")
    return value


def _require_provenance_descriptor(provenance: dict[str, object]) -> None:
    if frozenset(provenance) != _V3_PROVENANCE_KEYS:
        raise SystemExit("seed_provenance must be the closed v1 descriptor")
    if provenance.get("schema_version") != 1 or provenance.get("platform") != "local":
        raise SystemExit("seed_provenance has an invalid schema/platform")
    for key in ("provenance_sha256", "manifest_sha256"):
        _require_pattern(provenance, key, _SHA256)
    for key in ("seed_id", "origin_id"):
        _require_uuid(provenance, key)
    _require_pattern(provenance, "source_commit", _GIT_ID)
    if _TAG.fullmatch(_require_string(provenance, "bundle_name")) is None:
        raise SystemExit("seed_provenance.bundle_name has invalid identity shape")
    _require_rfc3339(_require_string(provenance, "source_date"), "seed_provenance.source_date")


def _require_uuid(raw: dict[str, object], key: str) -> None:
    value = _require_string(raw, key)
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise SystemExit(f"seed_provenance.{key} must be canonical UUID") from exc


def _require_existing_install_contract(contract: dict[str, object]) -> None:
    if frozenset(contract) != _V3_CONTRACT_KEYS:
        raise SystemExit("existing_install_contract must be closed")
    _require_string(contract, "flow_id")
    version = contract["flow_schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise SystemExit("existing_install_contract.flow_schema_version must be positive")
    digest = contract["bundle_digest"]
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise SystemExit("existing_install_contract.bundle_digest must be canonical")


def _require_repository_migrations(value: object, repository: str) -> None:
    if not isinstance(value, list):
        raise SystemExit("allowed_repository_migrations must be an array")
    seen: set[str] = set()
    for row in value:
        _require_repository_migration(row, repository, seen)


def _require_repository_migration(row: object, repository: str, seen: set[str]) -> None:
    if not isinstance(row, dict) or frozenset(row) != _MIGRATION_KEYS:
        raise SystemExit("repository migrations must be closed")
    source = _require_string(row, "from_repository")
    target = _require_string(row, "to_repository")
    _parse_github_url(source, "migration from_repository")
    _parse_github_url(target, "migration to_repository")
    if target != repository or source == target or source in seen:
        raise SystemExit("repository migration is invalid or duplicate")
    seen.add(source)


def _require_rfc3339(value: str, label: str) -> None:
    if not (value.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", value)):
        raise SystemExit(f"{label} must have an explicit offset")
    try:
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise SystemExit(f"{label} must be RFC3339") from exc


def _revision_line(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionError("formula_revision was not validated")
    return "" if value == 0 else f"  revision {value}"


def _require_pattern(raw: dict[str, object], key: str, pattern: re.Pattern[str]) -> None:
    if pattern.fullmatch(_require_string(raw, key)) is None:
        raise SystemExit(f"{key} has an invalid immutable identity shape")


def _render(template_path: Path, destination: Path, substitutions: dict[str, object]) -> None:
    template = template_path.read_text(encoding="utf-8")
    rendered = template
    for key, value in substitutions.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    if "{{" in rendered or "}}" in rendered:
        raise SystemExit(f"unresolved release marker in {template_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
