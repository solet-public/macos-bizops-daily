"""Closed, pure validation for the v1 ``PROVENANCE.json`` contract."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROOT = frozenset({"schema_version", "seed_id", "origin_id", "source_commit", "manifest_sha256", "bundle", "source_date", "lineage", "ancestry", "signature"})
_SUMMARY = frozenset({"seed_id", "origin_id", "source_commit", "manifest_sha256", "source_date"})
_BUNDLE = frozenset({"name", "platform"})
_REQUIRED_TRAILERS = frozenset({"Seed-Id", "Origin-Id", "Manifest-SHA256", "Assembled-Ref", "License-Policy", "Minted-At"})
_SEAL_SUBJECT = "Seed bundle (factory-sealed)"
_SEAL_POLICIES = frozenset({"public_apache", "internal_private"})


class ProvenanceV1Error(ValueError):
    """A v1 provenance or seal-trailer contract was malformed."""


@dataclass(frozen=True, slots=True)
class ProvenanceSummaryV1:
    seed_id: str
    origin_id: str
    source_commit: str
    manifest_sha256: str
    source_date: str


@dataclass(frozen=True, slots=True)
class ProvenanceV1:
    seed_id: str
    origin_id: str
    source_commit: str
    manifest_sha256: str
    bundle_name: str
    platform: str
    source_date: str
    lineage: tuple[ProvenanceSummaryV1, ...]
    ancestry: tuple[ProvenanceSummaryV1, ...]

    def summary(self) -> ProvenanceSummaryV1:
        return ProvenanceSummaryV1(self.seed_id, self.origin_id, self.source_commit, self.manifest_sha256, self.source_date)


def canonical_provenance_sha256(payload: bytes) -> str:
    """Return the digest only for canonical, strictly-valid v1 bytes."""
    parse_provenance_v1(payload)
    return hashlib.sha256(payload).hexdigest()


def parse_provenance_v1(payload: bytes) -> ProvenanceV1:
    """Parse the complete closed v1 wire format without performing I/O."""
    value = _canonical_object(payload)
    bundle_name, platform = _bundle_identity(value)
    stamp = _stamp(value, bundle_name, platform)
    _verify_seed_identity(stamp)
    return stamp


def _canonical_object(payload: bytes) -> Mapping[str, object]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceV1Error("provenance must be UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicate_object)
    except (json.JSONDecodeError, ProvenanceV1Error) as exc:
        raise ProvenanceV1Error(f"provenance is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvenanceV1Error("provenance must be one JSON object")
    canonical = json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
    if text != canonical:
        raise ProvenanceV1Error("provenance JSON must use the canonical v1 encoding")
    _exact_keys(value, _ROOT, "provenance")
    if value["schema_version"] != 1:
        raise ProvenanceV1Error("schema_version must be exactly 1")
    if value["signature"] is not None:
        raise ProvenanceV1Error("v1 signature must be explicitly null")
    return value


def _bundle_identity(value: Mapping[str, object]) -> tuple[str, str]:
    bundle = _mapping(value["bundle"], "bundle")
    _exact_keys(bundle, _BUNDLE, "bundle")
    bundle_name = _name(bundle["name"], "bundle.name")
    platform = _string(bundle["platform"], "bundle.platform")
    if platform != "local":
        raise ProvenanceV1Error("bundle.platform must be 'local'")
    return bundle_name, platform


def _stamp(value: Mapping[str, object], bundle_name: str, platform: str) -> ProvenanceV1:
    lineage = _summaries(value["lineage"], "lineage")
    ancestry = _summaries(value["ancestry"], "ancestry")
    return ProvenanceV1(
        seed_id=_uuid(value["seed_id"], "seed_id"),
        origin_id=_uuid(value["origin_id"], "origin_id"),
        source_commit=_pattern(value["source_commit"], _COMMIT, "source_commit"),
        manifest_sha256=_pattern(value["manifest_sha256"], _SHA256, "manifest_sha256"),
        bundle_name=bundle_name,
        platform=platform,
        source_date=_timestamp(value["source_date"], "source_date"),
        lineage=lineage,
        ancestry=ancestry,
    )


def _verify_seed_identity(stamp: ProvenanceV1) -> None:
    name = f"{stamp.source_commit}:{stamp.manifest_sha256}:{stamp.lineage[-1].seed_id if stamp.lineage else ''}:{','.join(item.seed_id for item in stamp.ancestry)}"
    if stamp.seed_id != str(uuid.uuid5(uuid.UUID(stamp.origin_id), name)):
        raise ProvenanceV1Error("seed_id does not match the v1 deterministic identity")


def verify_seal_trailers(stamp: ProvenanceV1, trailer_values: Mapping[str, Sequence[str] | str]) -> None:
    """Verify a complete, closed set of parsed seal trailers; no Git access."""
    _verify_trailer_shape(trailer_values)
    _verify_trailer_bindings(stamp, trailer_values)
    _verify_seal_metadata(trailer_values)
    _verify_lineage_parent(stamp, trailer_values)


def _verify_trailer_shape(trailer_values: Mapping[str, Sequence[str] | str]) -> None:
    unexpected = set(trailer_values) - (_REQUIRED_TRAILERS | {"Lineage-Parent", "Subject"})
    if unexpected:
        raise ProvenanceV1Error(f"unexpected seal trailers: {sorted(unexpected)}")
    for key in _REQUIRED_TRAILERS:
        values = _values(trailer_values, key)
        if len(values) != 1:
            raise ProvenanceV1Error(f"{key} must occur exactly once")


def _verify_trailer_bindings(stamp: ProvenanceV1, trailer_values: Mapping[str, Sequence[str] | str]) -> None:
    expected = {"Seed-Id": stamp.seed_id, "Origin-Id": stamp.origin_id, "Manifest-SHA256": stamp.manifest_sha256, "Assembled-Ref": stamp.source_commit}
    for key, expected_value in expected.items():
        if _values(trailer_values, key)[0] != expected_value:
            raise ProvenanceV1Error(f"{key} does not bind the strict provenance stamp")


def _verify_seal_metadata(trailer_values: Mapping[str, Sequence[str] | str]) -> None:
    if _values(trailer_values, "License-Policy")[0] not in _SEAL_POLICIES:
        raise ProvenanceV1Error("License-Policy is not a sealed policy")
    _timestamp(_values(trailer_values, "Minted-At")[0], "Minted-At")
    if "Subject" in trailer_values and _values(trailer_values, "Subject") != [_SEAL_SUBJECT]:
        raise ProvenanceV1Error("sealed subject is not the reviewed factory subject")


def _verify_lineage_parent(stamp: ProvenanceV1, trailer_values: Mapping[str, Sequence[str] | str]) -> None:
    parent = _values(trailer_values, "Lineage-Parent") if "Lineage-Parent" in trailer_values else []
    if stamp.lineage:
        if parent != [stamp.lineage[-1].seed_id]:
            raise ProvenanceV1Error("Lineage-Parent must bind the lineage tail exactly once")
    elif parent:
        raise ProvenanceV1Error("Lineage-Parent is forbidden for root provenance")


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceV1Error(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _exact_keys(value: Mapping[str, object], keys: frozenset[str], label: str) -> None:
    if frozenset(value) != keys:
        raise ProvenanceV1Error(f"{label} keys are not closed; missing={sorted(keys - set(value))}, extra={sorted(set(value) - keys)}")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProvenanceV1Error(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProvenanceV1Error(f"{label} must be a non-empty string")
    return value


def _name(value: object, label: str) -> str:
    text = _string(value, label)
    if _NAME.fullmatch(text) is None:
        raise ProvenanceV1Error(f"{label} has invalid characters")
    return text


def _pattern(value: object, pattern: re.Pattern[str], label: str) -> str:
    text = _string(value, label)
    if pattern.fullmatch(text) is None:
        raise ProvenanceV1Error(f"{label} has invalid canonical shape")
    return text


def _uuid(value: object, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ProvenanceV1Error(f"{label} must be a UUID") from exc
    if str(parsed) != text:
        raise ProvenanceV1Error(f"{label} must be a canonical lowercase UUID")
    return text


def _timestamp(value: object, label: str) -> str:
    text = _string(value, label)
    if not (text.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", text)):
        raise ProvenanceV1Error(f"{label} must have an explicit offset")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProvenanceV1Error(f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ProvenanceV1Error(f"{label} must have an explicit offset")
    return text


def _summaries(value: object, label: str) -> tuple[ProvenanceSummaryV1, ...]:
    if not isinstance(value, list):
        raise ProvenanceV1Error(f"{label} must be an explicit array")
    result: list[ProvenanceSummaryV1] = []
    for index, item in enumerate(value):
        row = _mapping(item, f"{label}[{index}]")
        _exact_keys(row, _SUMMARY, f"{label}[{index}]")
        result.append(
            ProvenanceSummaryV1(
                _uuid(row["seed_id"], f"{label}[{index}].seed_id"),
                _uuid(row["origin_id"], f"{label}[{index}].origin_id"),
                _pattern(row["source_commit"], _COMMIT, f"{label}[{index}].source_commit"),
                _pattern(row["manifest_sha256"], _SHA256, f"{label}[{index}].manifest_sha256"),
                _timestamp(row["source_date"], f"{label}[{index}].source_date"),
            )
        )
    return tuple(result)


def _values(mapping: Mapping[str, Sequence[str] | str], key: str) -> list[str]:
    value = mapping.get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return list(value)
    raise ProvenanceV1Error(f"{key} must be supplied as trailer text")
