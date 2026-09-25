"""Pinned LM Studio ls/ps JSON parsing and path-bound row selection."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from .lm_studio_deadline import ServedDeadline
from .lm_studio_models import ModelArtifact, verified_file_identity
from .setup_adapter_runtime import CommandOutcome

INDEX_RELATIVE_PATH = "solet-verified/Qwen3-14B/Qwen3-14B-Q4_K_M.gguf"
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@-]{0,255}$")
type CliReceipt = tuple[tuple[str, ...], CommandOutcome]


@dataclass(frozen=True, slots=True)
class IndexReadback:
    status: Literal["indexed", "missing", "invalid"]
    error_kind: str | None = None
    model_key: str | None = None
    destination: Path | None = None
    command: CommandOutcome | None = None
    argv: tuple[str, ...] | None = None
    file_identity: tuple[int, int, int, int, int] | None = None
    receipts: tuple[CliReceipt, ...] = ()


@dataclass(frozen=True, slots=True)
class LoadedReadback:
    status: Literal["served", "absent", "visibility_lag", "transport_unknown", "identifier_conflict", "invalid"]
    error_kind: str | None = None
    wrong_identifier: str | None = None
    command: CommandOutcome | None = None


def alias_path(home: Path) -> Path:
    return home / ".lmstudio/models" / INDEX_RELATIVE_PATH


def select_index_rows(rows: list[dict[str, object]], home: Path, model: ModelArtifact, source_path: Path, before: os.stat_result, deadline: ServedDeadline | None = None) -> IndexReadback:
    seen_keys: set[str] = set()
    seen_paths: set[str] = set()
    matches: list[IndexReadback] = []
    for row in rows:
        checked = _checked_index_row(row, home)
        if checked is None:
            return IndexReadback("invalid", "lm_studio_index_malformed")
        key, relative, path, size = checked
        if key in seen_keys or relative in seen_paths:
            return IndexReadback("invalid", "lm_studio_index_ambiguous")
        seen_keys.add(key)
        seen_paths.add(relative)
        candidate = _index_candidate(home, model, source_path, before, key, path, size, deadline)
        if candidate is None:
            continue
        if candidate.status == "invalid":
            return candidate
        matches.append(candidate)
    if len(matches) > 1:
        return IndexReadback("invalid", "lm_studio_index_ambiguous")
    if not matches:
        return IndexReadback("missing")
    return matches[0]


def _checked_index_row(row: dict[str, object], home: Path) -> tuple[str, str, Path, int] | None:
    if row["type"] != "llm":
        return None
    key, relative, size = row["modelKey"], row["path"], row["sizeBytes"]
    path = _indexed_path(home, relative)
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        return None
    if path is None or not isinstance(relative, str) or type(size) is not int or size <= 0:
        return None
    return key, relative, path, size


def _index_candidate(home: Path, model: ModelArtifact, source_path: Path, before: os.stat_result, key: str, path: Path, size: int, deadline: ServedDeadline | None = None) -> IndexReadback | None:
    current = _identity(path)
    same_inode = current is not None and current[:2] == _identity_from_stat(before)[:2]
    conflict = _index_path_conflict(home, model, source_path, key, path, current, same_inode)
    if conflict is not None:
        return IndexReadback("invalid", conflict)
    if not same_inode:
        return None
    if size != model.size_bytes or verified_file_identity(model, path, home, deadline) is None:
        return IndexReadback("invalid", "lm_studio_index_destination_invalid")
    if path == alias_path(home) and not key.startswith("solet-verified/"):
        return IndexReadback("invalid", "lm_studio_index_key_conflict")
    return IndexReadback("indexed", model_key=key, destination=path, file_identity=current)


def _index_path_conflict(home: Path, model: ModelArtifact, source_path: Path, key: str, path: Path, current: tuple[int, int, int, int, int] | None, same_inode: bool) -> str | None:
    if key == model.api_identifier and path != source_path:
        return "lm_studio_index_key_conflict"
    if path == alias_path(home) and current is not None and not same_inode:
        return "lm_studio_index_destination_conflict"
    return None


def select_loaded_rows(rows: list[dict[str, object]], home: Path, model: ModelArtifact, index: IndexReadback, deadline: ServedDeadline | None = None) -> LoadedReadback:
    seen_ids: set[str] = set()
    matching: list[str] = []
    for row in rows:
        checked = _checked_loaded_row(row, home)
        row_type = row["type"]
        if checked is None or not isinstance(row_type, str) or not row_type:
            return LoadedReadback("invalid", "lm_studio_loaded_index_malformed")
        key, path, size, identifier = checked
        if identifier in seen_ids:
            return LoadedReadback("invalid", "lm_studio_loaded_index_ambiguous")
        seen_ids.add(identifier)
        assessed = _assess_loaded_row(row_type, home, model, index, key, path, size, identifier, deadline)
        if assessed is None:
            continue
        if assessed.status != "served":
            return assessed
        matching.append(identifier)
    return _loaded_matches_result(matching, model)


def _loaded_matches_result(matching: list[str], model: ModelArtifact) -> LoadedReadback:
    if len(matching) > 1:
        return LoadedReadback("identifier_conflict", "identifier_conflict", matching[0])
    if matching and matching[0] != model.api_identifier:
        return LoadedReadback("identifier_conflict", "identifier_conflict", matching[0])
    return LoadedReadback("served" if matching else "absent")


def _assess_loaded_row(row_type: str, home: Path, model: ModelArtifact, index: IndexReadback, key: str, path: Path, size: int, identifier: str, deadline: ServedDeadline | None = None) -> LoadedReadback | None:
    if row_type != "llm":
        if key == index.model_key or identifier == model.api_identifier:
            return LoadedReadback("invalid", "lm_studio_loaded_index_mismatch")
        return None
    return _loaded_candidate(home, model, index, key, path, size, identifier, deadline)


def _checked_loaded_row(row: dict[str, object], home: Path) -> tuple[str, Path, int, str] | None:
    key, relative, size, identifier = row["modelKey"], row["path"], row["sizeBytes"], row["identifier"]
    path = _indexed_path(home, relative)
    if not isinstance(key, str) or _KEY.fullmatch(key) is None or path is None:
        return None
    if type(size) is not int or not isinstance(identifier, str) or _KEY.fullmatch(identifier) is None:
        return None
    return key, path, size, identifier


def _loaded_candidate(home: Path, model: ModelArtifact, index: IndexReadback, key: str, path: Path, size: int, identifier: str, deadline: ServedDeadline | None = None) -> LoadedReadback | None:
    if identifier == model.api_identifier and key != index.model_key:
        return LoadedReadback("identifier_conflict", "identifier_conflict")
    if key != index.model_key:
        return None
    if path != index.destination or _identity(path) != index.file_identity or size != model.size_bytes or verified_file_identity(model, path, home, deadline) is None:
        return LoadedReadback("invalid", "lm_studio_loaded_index_mismatch")
    return LoadedReadback("served")


def parse_rows(text: str, *, required_fields: tuple[str, ...]) -> list[dict[str, object]] | None:
    try:
        value: object = json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, list) or len(value) > 1024:
        return None
    rows: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item) or any(field not in item for field in required_fields):
            return None
        rows.append(item)
    return rows


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite vendor JSON constant {value}")


def _indexed_path(home: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        return None
    parsed = PurePosixPath(relative)
    if parsed.is_absolute():
        return None
    return home / ".lmstudio/models" / Path(*parsed.parts)


def _identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        return _identity_from_stat(path.stat())
    except OSError:
        return None


def _identity_from_stat(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
