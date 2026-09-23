"""Bounded, deterministic technology evidence for foreign targets.

The fingerprint is stable mechanics, not a framework rubric.  It reads only a
fixed set of well-known files already present in :class:`TargetTree`'s
enumeration, never executes configuration, and never turns a declaration into a
source-usage claim.  Its capability rows are routing evidence only: building the
fingerprint executes no target code, adapter, network request, or sandbox.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from .targets import TargetTree

MAX_CONTENT_BYTES: Final[int] = 2 * 1024 * 1024
MAX_MATCHES_PER_PROBE: Final[int] = 128
MAX_BOUNDED_EXAMPLES: Final[int] = 8
_JSON_PROBES: Final[frozenset[str]] = frozenset(
    {
        "package_json",
        "package_lock",
        "expo_app_json",
        "expo_eas_json",
        "tsconfig_json",
        "deno_manifest",
        "deno_lock",
    }
)
_PRESENCE_PROBES: Final[frozenset[str]] = frozenset(
    {
        "supabase_config",
        "supabase_functions",
        "supabase_migrations",
        "eslint_config",
        "jest_config",
        "recognized_unmodeled_config",
    }
)
_PROBE_ROSTER: Final[tuple[str, ...]] = (
    "package_json",
    "package_lock",
    "expo_app_json",
    "expo_eas_json",
    "tsconfig_json",
    "deno_manifest",
    "deno_lock",
    "supabase_config",
    "supabase_functions",
    "supabase_migrations",
    "eslint_config",
    "jest_config",
    "recognized_unmodeled_config",
)
_PROBE_STATUSES: Final[tuple[str, ...]] = ("matched", "not_present", "unreadable")

_DEPENDENCY_RELATIONSHIPS: Final[tuple[tuple[str, str], ...]] = (
    ("dependencies", "dependency"),
    ("optionalDependencies", "optional_dependency"),
    ("peerDependencies", "peer_dependency"),
    ("devDependencies", "dev_dependency"),
)
_COMPONENT_BY_PACKAGE: Final[dict[str, str]] = {
    "expo": "expo",
    "expo-router": "expo_router",
    "react": "react",
    "react-native": "react_native",
    "react-native-web": "react_native_web",
    "@supabase/supabase-js": "supabase_js",
    "typescript": "typescript",
    "eslint": "eslint",
    "jest": "jest",
    "ts-jest": "ts_jest",
}
_FRAMEWORK_COMPONENTS: Final[frozenset[str]] = frozenset(
    {
        "expo",
        "expo_router",
        "react",
        "react_native",
        "react_native_web",
        "supabase_js",
    }
)
_RUNTIME_PERFORMANCE_COMPONENTS: Final[frozenset[str]] = frozenset(
    {"expo", "expo_router", "react", "react_native", "react_native_web"}
)
_ROUTE_SUFFIXES: Final[frozenset[str]] = frozenset({".js", ".jsx", ".ts", ".tsx"})

_ESLINT_CONFIG_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".eslintrc",
        ".eslintrc.cjs",
        ".eslintrc.js",
        ".eslintrc.json",
        ".eslintrc.mjs",
        ".eslintrc.yaml",
        ".eslintrc.yml",
        "eslint.config.cjs",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.ts",
    }
)
_JEST_CONFIG_NAMES: Final[frozenset[str]] = frozenset(
    {
        "jest.config.cjs",
        "jest.config.js",
        "jest.config.json",
        "jest.config.mjs",
        "jest.config.ts",
    }
)
_UNMODELED_CONFIG_NAMES: Final[frozenset[str]] = frozenset(
    {
        "app.config.cjs",
        "app.config.js",
        "app.config.mjs",
        "app.config.ts",
        "babel.config.cjs",
        "babel.config.js",
        "babel.config.mjs",
        "babel.config.ts",
        "eslint.config.cjs",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.ts",
        "jest.config.cjs",
        "jest.config.js",
        "jest.config.mjs",
        "jest.config.ts",
        "metro.config.cjs",
        "metro.config.js",
        "metro.config.mjs",
        "metro.config.ts",
        "vite.config.cjs",
        "vite.config.js",
        "vite.config.mjs",
        "vite.config.ts",
        "webpack.config.cjs",
        "webpack.config.js",
        "webpack.config.mjs",
        "webpack.config.ts",
    }
)


class _DuplicateJSONKeyError(ValueError):
    """A strict-JSON object repeated a key."""


class _NonStandardJSONConstantError(ValueError):
    """A JavaScript-only NaN/Infinity literal appeared in strict JSON."""


_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


@dataclass(frozen=True, slots=True)
class _ReadResult:
    path: str
    status: str
    data: Mapping[str, object] | None = None
    reason: str | None = None
    detail: str | None = None

    def observation(self) -> dict[str, object]:
        item: dict[str, object] = {"path": self.path, "status": self.status}
        if self.reason is not None:
            item["reason"] = self.reason
        if self.detail is not None:
            item["detail"] = self.detail
        return item


def scope_for_path(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "." if parent == "." else parent


def join_scope(scope: str, child: str) -> str:
    return child if scope == "." else f"{scope}/{child}"


def json_pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def evidence(path: str, pointer: str, kind: str) -> dict[str, str]:
    return {"path": path, "pointer": pointer, "kind": kind}


def _validate_enumerated_path(path: str) -> None:
    pure = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != path
    ):
        raise RuntimeError(
            f"TargetTree enumerated path is not normalized tree-relative POSIX: {path!r}"
        )


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _NonStandardJSONConstantError(value)


def _json_decode_reason(probe: str, text: str) -> str:
    without_strings = _JSON_STRING.sub("", text)
    has_comments = "//" in without_strings or "/*" in without_strings
    if probe == "tsconfig_json" and has_comments:
        return "unsupported_jsonc"
    return "invalid_strict_json"


def _metadata_gap(metadata: os.stat_result, *, symlink_detail: str) -> tuple[str, str] | None:
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink_not_followed", symlink_detail
    if not stat.S_ISREG(metadata.st_mode):
        return "not_regular_file", f"mode={metadata.st_mode:o}"
    if metadata.st_size > MAX_CONTENT_BYTES:
        return "content_too_large", f"{metadata.st_size} bytes exceeds {MAX_CONTENT_BYTES}"
    return None


def _lstat_regular(
    tree: TargetTree, path: str
) -> tuple[os.stat_result | None, str | None, str | None]:
    try:
        metadata = tree.abspath(path).lstat()
    except OSError as exc:
        return None, "read_error", exc.__class__.__name__
    gap = _metadata_gap(metadata, symlink_detail="enumerated path is a symlink")
    return (metadata, None, None) if gap is None else (None, *gap)


def _open_no_follow(tree: TargetTree, path: str) -> tuple[int | None, str | None, str | None]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(tree.abspath(path), flags), None, None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None, "symlink_not_followed", "enumerated path became a symlink"
        return None, "read_error", exc.__class__.__name__


def _read_descriptor(descriptor: int) -> tuple[bytes | None, str | None, str | None]:
    try:
        opened = os.fstat(descriptor)
        gap = _metadata_gap(
            opened,
            symlink_detail="opened descriptor unexpectedly identifies a symlink",
        )
        if gap is not None:
            return None, *gap
        chunks: list[bytes] = []
        remaining = MAX_CONTENT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_CONTENT_BYTES:
        detail = f"content changed while read and exceeded {MAX_CONTENT_BYTES} bytes"
        return None, "content_too_large", detail
    return payload, None, None


def _read_bounded_bytes(tree: TargetTree, path: str) -> tuple[bytes | None, str | None, str | None]:
    """Read one enumerated file without following symlinks and with a hard cap."""

    _validate_enumerated_path(path)
    metadata, reason, detail = _lstat_regular(tree, path)
    if metadata is None:
        return None, reason, detail
    descriptor, reason, detail = _open_no_follow(tree, path)
    if descriptor is None:
        return None, reason, detail
    return _read_descriptor(descriptor)


def _optional_string_error(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        return f"package {key} must be a string"
    return None


def _dependency_table_error(data: Mapping[str, object], table: str) -> str | None:
    raw = data.get(table)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return f"{table} must be an object"
    invalid = any(
        not isinstance(package_name, str) or not isinstance(declared_version, str)
        for package_name, declared_version in raw.items()
    )
    if invalid:
        return f"{table} entries must map string names to string versions"
    return None


def _validate_package_manifest(data: Mapping[str, object]) -> str | None:
    for key in ("name", "version"):
        error = _optional_string_error(data, key)
        if error is not None:
            return error
    for table, _relationship in _DEPENDENCY_RELATIONSHIPS:
        error = _dependency_table_error(data, table)
        if error is not None:
            return error
    return None


def _validate_package_lock(data: Mapping[str, object]) -> str | None:
    version = data.get("lockfileVersion")
    if isinstance(version, bool) or version not in {2, 3}:
        return "lockfileVersion must be npm package-lock version 2 or 3"
    packages = data.get("packages")
    if not isinstance(packages, Mapping):
        return "npm package-lock v2/v3 requires an object at /packages"
    for package_path, record in packages.items():
        if not isinstance(package_path, str) or not isinstance(record, Mapping):
            return "package-lock /packages entries must map string paths to objects"
        resolved = record.get("version")
        if resolved is not None and not isinstance(resolved, str):
            return f"package-lock version at /packages/{package_path} must be a string"
    return None


def _read_json_object(tree: TargetTree, path: str, probe: str) -> _ReadResult:
    payload, reason, detail = _read_bounded_bytes(tree, path)
    if payload is None:
        return _ReadResult(path=path, status="unreadable", reason=reason, detail=detail)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="invalid_utf8",
            detail=f"byte {exc.start}",
        )
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJSONKeyError as exc:
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="duplicate_json_key",
            detail=f"duplicate key {exc.args[0]!r}",
        )
    except json.JSONDecodeError as exc:
        return _ReadResult(
            path=path,
            status="unreadable",
            reason=_json_decode_reason(probe, text),
            detail=f"line {exc.lineno}, column {exc.colno}",
        )
    except _NonStandardJSONConstantError as exc:
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="invalid_strict_json",
            detail=f"non-standard constant {exc.args[0]!r}",
        )
    if not isinstance(parsed, Mapping):
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="invalid_json_shape",
            detail="top-level JSON value must be an object",
        )
    shape_error: str | None = None
    if probe == "package_json":
        shape_error = _validate_package_manifest(parsed)
    elif probe == "package_lock":
        shape_error = _validate_package_lock(parsed)
    if shape_error is not None:
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="invalid_json_shape",
            detail=shape_error,
        )
    return _ReadResult(path=path, status="matched", data=parsed)


def _presence_result(tree: TargetTree, path: str) -> _ReadResult:
    _validate_enumerated_path(path)
    try:
        metadata = tree.abspath(path).lstat()
    except OSError as exc:
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="read_error",
            detail=exc.__class__.__name__,
        )
    if stat.S_ISLNK(metadata.st_mode):
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="symlink_not_followed",
            detail="enumerated path is a symlink",
        )
    if not stat.S_ISREG(metadata.st_mode):
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="not_regular_file",
            detail=f"mode={metadata.st_mode:o}",
        )
    return _ReadResult(path=path, status="matched")


def _is_supabase_function(path: str) -> bool:
    parts = PurePosixPath(path).parts
    for index in range(len(parts) - 3):
        if parts[index : index + 2] == ("supabase", "functions"):
            return index + 3 == len(parts) - 1 and parts[-1] == "index.ts"
    return False


def _is_supabase_migration(path: str) -> bool:
    parts = PurePosixPath(path).parts
    for index in range(len(parts) - 2):
        if parts[index : index + 2] == ("supabase", "migrations"):
            return index + 2 == len(parts) - 1 and path.endswith(".sql")
    return False


def _name_matcher(names: frozenset[str]) -> Callable[[str], bool]:
    return lambda path: PurePosixPath(path).name in names


def _tsconfig_match(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.startswith("tsconfig") and name.endswith(".json")


def _supabase_config_match(path: str) -> bool:
    return path == "supabase/config.toml" or path.endswith("/supabase/config.toml")


_PROBE_MATCHERS: Final[dict[str, Callable[[str], bool]]] = {
    "package_json": _name_matcher(frozenset({"package.json"})),
    "package_lock": _name_matcher(frozenset({"package-lock.json"})),
    "expo_app_json": _name_matcher(frozenset({"app.json"})),
    "expo_eas_json": _name_matcher(frozenset({"eas.json"})),
    "tsconfig_json": _tsconfig_match,
    "deno_manifest": _name_matcher(frozenset({"deno.json", "deno.jsonc"})),
    "deno_lock": _name_matcher(frozenset({"deno.lock"})),
    "supabase_config": _supabase_config_match,
    "supabase_functions": _is_supabase_function,
    "supabase_migrations": _is_supabase_migration,
    "eslint_config": _name_matcher(_ESLINT_CONFIG_NAMES),
    "jest_config": _name_matcher(_JEST_CONFIG_NAMES),
    "recognized_unmodeled_config": _name_matcher(_UNMODELED_CONFIG_NAMES),
}


def _matches_probe(probe: str, path: str) -> bool:
    try:
        matcher = _PROBE_MATCHERS[probe]
    except KeyError as exc:
        raise RuntimeError(f"unknown technology-fingerprint probe: {probe}") from exc
    return matcher(path)


def _candidate_result(tree: TargetTree, probe: str, path: str) -> _ReadResult:
    if probe == "deno_manifest" and path.endswith(".jsonc"):
        return _ReadResult(
            path=path,
            status="unreadable",
            reason="unsupported_jsonc",
            detail="JSONC is recognized but intentionally not parsed",
        )
    if probe in _JSON_PROBES:
        return _read_json_object(tree, path, probe)
    if probe in _PRESENCE_PROBES:
        return _presence_result(tree, path)
    raise RuntimeError(f"probe {probe!r} has no read policy")


def _probe_status(candidates: Sequence[str], unreadable_count: int) -> str:
    if not candidates:
        return "not_present"
    return "unreadable" if unreadable_count else "matched"


def _probe_observations(
    results: Sequence[_ReadResult], omitted: Sequence[str]
) -> tuple[list[dict[str, object]], int]:
    observations = [result.observation() for result in results]
    unreadable_count = sum(result.status == "unreadable" for result in results)
    if omitted:
        observations.append(
            {
                "status": "unreadable",
                "reason": "probe_match_limit_exceeded",
                "omitted_count": len(omitted),
                "examples": list(omitted[:MAX_BOUNDED_EXAMPLES]),
            }
        )
    return observations, unreadable_count + len(omitted)


def _probe_payload(
    probe: str,
    candidates: Sequence[str],
    matched: Sequence[_ReadResult],
    unreadable_count: int,
    observations: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    reconciles = len(candidates) == len(matched) + unreadable_count
    if not reconciles:
        raise RuntimeError(f"technology probe {probe!r} failed candidate reconciliation")
    return {
        "probe": probe,
        "status": _probe_status(candidates, unreadable_count),
        "candidate_count": len(candidates),
        "matched_count": len(matched),
        "unreadable_count": unreadable_count,
        "observations": [dict(item) for item in observations],
        "reconciliation": {
            "candidates": len(candidates),
            "matched_plus_unreadable": len(matched) + unreadable_count,
            "reconciles": True,
        },
    }


def _run_probe(
    tree: TargetTree, tracked: Sequence[str], probe: str
) -> tuple[dict[str, object], dict[str, _ReadResult]]:
    candidates = [path for path in tracked if _matches_probe(probe, path)]
    selected = candidates[:MAX_MATCHES_PER_PROBE]
    omitted = candidates[MAX_MATCHES_PER_PROBE:]
    results = [_candidate_result(tree, probe, path) for path in selected]
    matched = [result for result in results if result.status == "matched"]
    observations, unreadable_count = _probe_observations(results, omitted)
    payload = _probe_payload(probe, candidates, matched, unreadable_count, observations)
    return payload, {result.path: result for result in matched}


def _validated_tracked(tree: TargetTree) -> tuple[str, ...]:
    tracked = tuple(sorted(tree.all_files()))
    if len(set(tracked)) != len(tracked):
        raise RuntimeError("TargetTree enumeration contains duplicate paths")
    for path in tracked:
        _validate_enumerated_path(path)
    return tracked


def probe_results(
    tree: TargetTree,
) -> tuple[list[dict[str, object]], dict[str, dict[str, _ReadResult]]]:
    tracked = _validated_tracked(tree)
    probes: list[dict[str, object]] = []
    successful: dict[str, dict[str, _ReadResult]] = {}
    for probe in _PROBE_ROSTER:
        payload, matched = _run_probe(tree, tracked, probe)
        probes.append(payload)
        successful[probe] = matched
    return probes, successful
