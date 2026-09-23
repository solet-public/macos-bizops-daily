"""Key/value file parser for credential ingestion.

Supports three formats:

- env  — simple ``KEY=value`` lines, ``#`` comments, blank lines ignored.
  Optional surrounding ``"..."`` or ``'...'`` are stripped.
- json — top-level JSON object; field value must be string/number/boolean.
- yaml — top-level YAML mapping; field value must be a scalar.

The parser fails fast with typed exceptions; callers translate those to
ActionResult error responses with the appropriate vault error code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

# ── Format tokens ─────────────────────────────────────────────────────────────

_FORMAT_ENV = "env"
_FORMAT_JSON = "json"
_FORMAT_YAML = "yaml"

_EXT_ENV = ".env"
_EXT_JSON = ".json"
_EXT_YAML = ".yaml"
_EXT_YML = ".yml"

_ENV_COMMENT_PREFIX = "#"
_ENV_DELIMITER = "="
_ENV_QUOTE_DOUBLE = '"'
_ENV_QUOTE_SINGLE = "'"

_SUPPORTED_FORMATS: frozenset[str] = frozenset({_FORMAT_ENV, _FORMAT_JSON, _FORMAT_YAML})

_EXTENSION_FORMAT_MAP: dict[str, str] = {
    _EXT_ENV: _FORMAT_ENV,
    _EXT_JSON: _FORMAT_JSON,
    _EXT_YAML: _FORMAT_YAML,
    _EXT_YML: _FORMAT_YAML,
}


# ── Public exception types ─────────────────────────────────────────────────────


class KVFileError(Exception):
    """Base error for KV file parsing."""


class KVFileFormatUnknownError(KVFileError):
    """Format could not be determined from extension and was not specified."""


class KVFileParseError(KVFileError):
    """File could not be parsed in the declared format."""


class KVFileFieldNotFoundError(KVFileError):
    """The named field was not present in the parsed mapping."""


class KVFileFieldNotScalarError(KVFileError):
    """The named field's value was not a scalar (list/object rejected)."""


# ── Public API ─────────────────────────────────────────────────────────────────


def resolve_format(path: Path, explicit_format: str | None) -> str:
    """Pick the parsing format. Explicit override beats extension detection."""
    if explicit_format is not None:
        if explicit_format not in _SUPPORTED_FORMATS:
            raise KVFileFormatUnknownError(
                f"Unsupported format {explicit_format!r}; "
                f"expected one of {sorted(_SUPPORTED_FORMATS)}."
            )
        return explicit_format

    suffix = path.suffix.lower()
    detected = _EXTENSION_FORMAT_MAP.get(suffix)
    if detected is None:
        raise KVFileFormatUnknownError(
            f"Cannot infer format from extension {suffix!r}; "
            f"pass an explicit 'format' argument."
        )
    return detected


def extract_field(text: str, fmt: str, field: str) -> str:
    """Parse ``text`` in ``fmt`` and return ``field``'s scalar value as a string."""
    if fmt == _FORMAT_ENV:
        return _extract_env_field(text, field)
    if fmt == _FORMAT_JSON:
        return _extract_json_field(text, field)
    if fmt == _FORMAT_YAML:
        return _extract_yaml_field(text, field)
    raise KVFileFormatUnknownError(f"Unsupported format: {fmt!r}")


# ── Private helpers ────────────────────────────────────────────────────────────


def _extract_env_field(text: str, field: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(_ENV_COMMENT_PREFIX):
            continue
        if _ENV_DELIMITER not in line:
            continue
        name, _, value = line.partition(_ENV_DELIMITER)
        if name.strip() != field:
            continue
        return _strip_env_quotes(value.strip())
    raise KVFileFieldNotFoundError(f"Field {field!r} not found in env file.")


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2:
        first, last = value[0], value[-1]
        if first == last and first in (_ENV_QUOTE_DOUBLE, _ENV_QUOTE_SINGLE):
            return value[1:-1]
    return value


def _extract_json_field(text: str, field: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KVFileParseError(f"Invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise KVFileParseError("JSON file must contain a top-level object.")
    return _coerce_mapping_field(parsed, field)


def _extract_yaml_field(text: str, field: str) -> str:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KVFileParseError(f"Invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise KVFileParseError("YAML file must contain a top-level mapping.")
    return _coerce_mapping_field(parsed, field)


def _coerce_mapping_field(mapping: dict[str, Any], field: str) -> str:
    if field not in mapping:
        raise KVFileFieldNotFoundError(f"Field {field!r} not found in file.")
    raw = mapping[field]
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, int | float):
        return str(raw)
    raise KVFileFieldNotScalarError(
        f"Field {field!r} is not a scalar (got {type(raw).__name__})."
    )
