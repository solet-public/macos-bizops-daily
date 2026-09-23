"""Closed validation helpers for public adapter-envelope values."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from typing import cast

from .errors import AdapterProtocolError
from .models import JsonValue

STREAM_LIMIT_BYTES = 8192
FAILURE_STDERR_DIAGNOSTIC_LIMIT = 1024
FORMULA_MARKER = "/Cellar/solet/"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
DIGEST_PATTERN = re.compile(r"^(sha256:[0-9a-f]{64}|none)$")
FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
OPERATION_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]*::[a-z][a-z0-9_.]*$")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(password|secret|token|authorization|oauth[_ -]?code|private[_ -]?key)\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+"),
)


def bounded_redacted(value: str) -> str:
    """Redact secret-shaped text and bound its public byte representation."""

    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) <= STREAM_LIMIT_BYTES:
        return redacted
    digest = hashlib.sha256(encoded).hexdigest()
    prefix = encoded[:STREAM_LIMIT_BYTES].decode("utf-8", errors="replace")
    return f"{prefix}\n[TRUNCATED bytes={len(encoded)} sha256={digest}]"


def public_string(value: JsonValue, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise TypeError(
            f"{label} must be a non-empty string of at most {maximum} characters"
        )
    if bounded_redacted(value) != value:
        raise ValueError(f"{label} contains secret-like material")
    if FORMULA_MARKER in value:
        raise ValueError(f"{label} contains a formula-keg path")
    return value


def identifier(value: JsonValue, label: str) -> str:
    string = public_string(value, label, maximum=128)
    if ID_PATTERN.fullmatch(string) is None:
        raise ValueError(f"{label} does not match the closed id grammar")
    return string


def optional_identifier(value: JsonValue, label: str) -> str | None:
    return None if value is None else identifier(value, label)


def optional_public_string(
    value: JsonValue,
    label: str,
    *,
    maximum: int,
) -> str | None:
    return None if value is None else public_string(value, label, maximum=maximum)


def stream(value: JsonValue, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return bounded_redacted(value)


def public_value(value: JsonValue, label: str) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError(f"{label} must be a finite number")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return public_string(value, label, maximum=4096)
    if isinstance(value, list):
        return _public_string_array(value, label)
    raise TypeError(f"{label} is not a public scalar or unique string array")


def _public_string_array(value: list[JsonValue], label: str) -> JsonValue:
    if not all(isinstance(item, str) for item in value):
        raise TypeError(f"{label} is not a public scalar or unique string array")
    strings = [public_string(item, label, maximum=4096) for item in value]
    if len(strings) != len(set(strings)):
        raise ValueError(f"{label} string array must be unique")
    return cast(JsonValue, strings)


def boolean(value: JsonValue, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be boolean")
    return value


def optional_int(
    value: JsonValue,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return None
    return bounded_integer(value, label, minimum=minimum, maximum=maximum)


def nonnegative_integer(value: JsonValue, label: str) -> int:
    return bounded_integer(value, label, minimum=0)


def bounded_integer(
    value: JsonValue,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    parsed = _integer(value, label)
    if minimum is not None and parsed < minimum:
        raise TypeError(f"{label} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise TypeError(f"{label} must be at most {maximum}")
    return parsed


def _integer(value: JsonValue, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be integer")
    return value


def validate_evidence(raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    required = {
        "id",
        "kind",
        "status",
        "summary",
        "observed",
        "expected",
        "source",
        "digest",
        "captured_at",
        "sensitivity",
    }
    if set(raw) != required:
        raise AdapterProtocolError("adapter evidence does not match the closed v1 shape")
    try:
        return _validated_evidence(raw)
    except (TypeError, ValueError) as exc:
        raise AdapterProtocolError(f"adapter evidence fields are invalid: {exc}") from exc


def _validated_evidence(raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    digest = public_string(raw["digest"], "evidence digest", maximum=71)
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError("evidence digest is invalid")
    captured_at = public_string(
        raw["captured_at"], "evidence captured_at", maximum=64
    )
    datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    sensitivity = raw["sensitivity"]
    if sensitivity not in {"public", "redacted"}:
        raise ValueError("evidence sensitivity is invalid")
    return {
        "id": identifier(raw["id"], "evidence id"),
        "kind": identifier(raw["kind"], "evidence kind"),
        "status": public_string(raw["status"], "evidence status", maximum=512),
        "summary": public_string(raw["summary"], "evidence summary", maximum=512),
        "observed": public_value(raw["observed"], "evidence observed"),
        "expected": public_value(raw["expected"], "evidence expected"),
        "source": public_string(raw["source"], "evidence source", maximum=512),
        "digest": digest,
        "captured_at": captured_at,
        "sensitivity": str(sensitivity),
    }


def validate_reason(value: JsonValue) -> dict[str, JsonValue] | None:
    """Validate the closed, stream-free command-failure diagnostic."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("reason must be an object or null")
    legacy_required = {
        "outcome_class",
        "exit_code",
        "duration_ms",
        "timed_out",
        "stdout_bytes",
        "stderr_bytes",
        "stdout_truncated",
        "stderr_truncated",
    }
    diagnostic_required = legacy_required | {
        "stderr_diagnostic",
        "stderr_diagnostic_truncated",
    }
    fields = set(value)
    if fields != legacy_required and fields != diagnostic_required:
        raise ValueError("reason does not match the closed diagnostic shape")
    outcome_class = value["outcome_class"]
    if outcome_class not in {"executable_missing", "launch_error", "timeout", "nonzero_exit"}:
        raise ValueError("reason outcome_class is invalid")
    validated: dict[str, JsonValue] = {
        "outcome_class": outcome_class,
        "exit_code": optional_int(value["exit_code"], "reason exit_code", minimum=0, maximum=255),
        "duration_ms": bounded_integer(value["duration_ms"], "reason duration_ms", minimum=0),
        "timed_out": boolean(value["timed_out"], "reason timed_out"),
        "stdout_bytes": bounded_integer(value["stdout_bytes"], "reason stdout_bytes", minimum=0),
        "stderr_bytes": bounded_integer(value["stderr_bytes"], "reason stderr_bytes", minimum=0),
        "stdout_truncated": boolean(value["stdout_truncated"], "reason stdout_truncated"),
        "stderr_truncated": boolean(value["stderr_truncated"], "reason stderr_truncated"),
    }
    if fields == diagnostic_required:
        validated["stderr_diagnostic"] = public_string(
            value["stderr_diagnostic"],
            "reason stderr_diagnostic",
            maximum=FAILURE_STDERR_DIAGNOSTIC_LIMIT,
        )
        validated["stderr_diagnostic_truncated"] = boolean(
            value["stderr_diagnostic_truncated"],
            "reason stderr_diagnostic_truncated",
        )
    return validated
